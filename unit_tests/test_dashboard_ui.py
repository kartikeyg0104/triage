"""The redesigned dashboard: overview, filters, search, sort, live refresh, health, claim strips.

The security rules of test_server_dashboard.py still hold: no script, one hashed style block,
no inline style attributes, everything escaped.
"""

import re
import sqlite3
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from claimcheck.server.app import create_app
from claimcheck.server.store import Store

SECRET = "ui-secret"
SCRIPT = "<script>alert(1)</script>"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "claimcheck.db"


@pytest.fixture
def store(db):
    return Store(db)


@pytest.fixture
def client(tmp_path, db):
    (tmp_path / "repos").mkdir()
    return TestClient(create_app(db, tmp_path / "repos", SECRET))


def set_created(db, investigation_id, created_at):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE investigations SET created_at = ? WHERE id = ?", (created_at, investigation_id))


def result(verdict, *statuses):
    return {"verdict": verdict, "evidence": [
        {"kind": "symbol", "claim": f"f{i}() exists", "status": s, "basis": "observed", "detail": "d", "source": "a.py:1"}
        for i, s in enumerate(statuses)]}


def seed(store, db):
    made = {
        "ok": store.create("github:issues:octo/widgets#7", "completed", {}, result("supported", "confirmed"))[0],
        "bad": store.create("api", "completed", {}, result("refuted", "confirmed", "refuted", "unverified"))[0],
        "wait": store.create("github:pull_request:octo/widgets#9", "received", {})[0],
        "boom": store.create("api", "failed", {})[0],
    }
    for n, investigation in enumerate(made.values()):
        set_created(db, investigation.id, f"2026-11-01T0{n}:00:00+00:00")
    return made


# Store queries behind the screens


def test_summary_counts_statuses_and_verdicts(store, db):
    seed(store, db)
    summary = store.summary()
    assert summary["total"] == 4
    assert summary["status"] == {"received": 1, "processing": 0, "completed": 2, "skipped": 0, "failed": 1}
    assert summary["verdict"] == {"supported": 1, "refuted": 1, "unverified": 0}


def test_list_filters_by_verdict_searches_and_sorts(store, db):
    made = seed(store, db)
    assert [i.id for i in store.list(verdict="refuted")] == [made["bad"].id]
    assert {i.id for i in store.list(query="octo/widgets")} == {made["ok"].id, made["wait"].id}
    assert [i.id for i in store.list(query=made["boom"].id[:8])] == [made["boom"].id]
    assert [i.id for i in store.list(oldest_first=True)][0] == made["ok"].id
    assert store.list(query="%") == []  # LIKE wildcards in the query are literal


def test_activity_counts_investigations_per_hour(store, db):
    seed(store, db)
    hours = store.activity(hours=6, now="2026-11-01T05:30:00+00:00")
    assert [h for _, h in hours] == [1, 1, 1, 1, 0, 0]
    assert hours[0][0] == "2026-11-01T00"


def test_health_reports_waiting_processing_and_failures(store, db):
    made = seed(store, db)
    health = store.health()
    assert health["oldest_waiting"] == "2026-11-01T02:00:00+00:00"
    assert health["processing"] == []
    assert [i.id for i in health["failed"]] == [made["boom"].id]


# Screens


def test_layout_has_sidebar_menubar_and_counts(client, store, db):
    seed(store, db)
    page = client.get("/dashboard").text
    assert '<nav class="sidebar"' in page and '<header class="menubar"' in page
    assert 'href="/dashboard/health"' in page
    assert re.search(r'href="/dashboard\?verdict=refuted"[^>]*>.*?<b>1</b>', page, re.S)
    assert '<svg class="strip"' in page  # verdict distribution strip
    assert '<svg class="activity"' in page  # 24-hour activity chart


def test_rows_show_one_strip_cell_per_claim(client, store, db):
    made = seed(store, db)
    page = client.get("/dashboard").text
    row = re.search(rf'<tr[^>]*>(?:(?!</tr>).)*{made["bad"].id}(?:(?!</tr>).)*</tr>', page, re.S).group(0)
    assert row.count('class="cell c-') == 3
    assert "c-confirmed" in row and "c-refuted" in row and "c-unverified" in row


def test_search_verdict_filter_and_sort_work_together(client, store, db):
    made = seed(store, db)
    page = client.get("/dashboard", params={"q": "octo", "verdict": "supported"}).text
    assert made["ok"].id in page and made["wait"].id not in page and made["bad"].id not in page
    assert 'value="octo"' in page
    oldest = client.get("/dashboard", params={"sort": "oldest"}).text
    assert oldest.index(made["ok"].id) < oldest.index(made["boom"].id)
    assert client.get("/dashboard", params={"verdict": "nope"}).status_code == 400
    assert client.get("/dashboard", params={"sort": "nope"}).status_code == 400


def test_live_mode_refreshes_and_keeps_filters(client, store, db):
    seed(store, db)
    off = client.get("/dashboard", params={"status": "completed"}).text
    assert 'http-equiv="refresh"' not in off
    live = client.get("/dashboard", params={"status": "completed", "live": "1"}).text
    assert '<meta http-equiv="refresh" content="10">' in live
    assert 'href="/dashboard?status=completed"' in live  # the toggle turns it off and keeps the filter


def test_health_page(client, store, db):
    made = seed(store, db)
    page = client.get("/dashboard/health").text
    assert "Waiting" in page and "Failed" in page and made["boom"].id in page
    assert 'aria-current="page"' in page


def test_detail_has_verdict_banner_strip_and_claims(client, store, db):
    made = seed(store, db)
    page = client.get(f"/dashboard/investigations/{made['bad'].id}").text
    assert 'class="banner v-refuted"' in page
    assert page.count('class="cell c-') == 3
    assert "f1() exists" in page


def test_untrusted_text_stays_escaped_in_every_new_spot(client, store):
    store.create(f"api{SCRIPT}", "completed", {}, result(f"refuted{SCRIPT}", "refuted"))
    for path, params in (("/dashboard", {"q": SCRIPT}), ("/dashboard", {}), ("/dashboard/health", {})):
        page = client.get(path, params=params).text
        assert "<script" not in page
        assert " style=" not in page


def test_csp_allows_the_get_search_form_and_still_no_script(client):
    csp = client.get("/dashboard").headers["content-security-policy"]
    directives = {part.split()[0]: part.split()[1:] for part in csp.split(";") if part.strip()}
    assert directives["form-action"] == ["'self'"]
    assert "script-src" not in directives and directives["default-src"] == ["'none'"]
