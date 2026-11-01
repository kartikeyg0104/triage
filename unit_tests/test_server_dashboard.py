"""The read-only HTML dashboard: list and detail pages over the stored investigations.

Unit tests go through FastAPI's TestClient; the integration tests run the real
`python -m claimcheck.server` process, record a signed webhook and an API investigation whose text
is hostile HTML, and check the rendered pages over real HTTP."""

import base64
import hashlib
import hmac
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from claimcheck.server import dashboard
from claimcheck.server.app import create_app
from claimcheck.server.store import Store

SECRET = "s3cret"
SCRIPT = "<script>alert(1)</script>"
IMG = '"><img src=x onerror=alert(2)>'
HOSTILE = f"{SCRIPT} {IMG} RCE in parse_url_v2() and `parse_url()`"
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def issue_body(title: str = HOSTILE, body: str = IMG, number: int = 12) -> bytes:
    return json.dumps({"action": "opened", "issue": {"number": number, "title": title, "body": body},
                       "repository": {"full_name": "octo/widgets"}}).encode()


def assert_inert(page: str) -> None:
    """No hostile markup survives as markup; it is all there as escaped text."""
    assert "<script" not in page.lower()
    assert "<img" not in page.lower()
    assert IMG not in page and SCRIPT not in page  # neither payload appears unescaped
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&quot;&gt;&lt;img src=x onerror=alert(2)&gt;" in page


def assert_secure(response) -> None:
    assert response.headers["content-type"].startswith("text/html")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    csp = response.headers["content-security-policy"]
    directives = {part.split()[0]: part.split()[1:] for part in csp.split(";") if part.strip()}
    assert directives["default-src"] == ["'none'"]
    assert "script-src" not in directives  # falls back to default-src 'none': no script at all
    assert directives["frame-ancestors"] == ["'none'"]
    assert directives["form-action"] == ["'none'"]
    assert directives["base-uri"] == ["'none'"]
    assert "'unsafe-inline'" not in csp
    # The one inline <style> block is allowed by its hash, and by nothing else.
    styles = re.findall(r"<style>(.*?)</style>", response.text, re.S)
    assert len(styles) == 1
    digest = base64.b64encode(hashlib.sha256(styles[0].encode()).digest()).decode()
    assert directives["style-src"] == [f"'sha256-{digest}'"]
    assert " style=" not in response.text  # inline style attributes would be blocked anyway


@pytest.fixture
def root(tmp_path):
    repo = tmp_path / "repos" / "sample"
    repo.mkdir(parents=True)
    (repo / "urls.py").write_text("def parse_url(s):\n    return s\n")
    return tmp_path / "repos"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "claimcheck.db"


@pytest.fixture
def store(db):
    return Store(db)


@pytest.fixture
def client(root, db):
    return TestClient(create_app(db, root, SECRET))


def set_created(db: Path, investigation_id: str, created_at: str) -> None:
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE investigations SET created_at = ? WHERE id = ?", (created_at, investigation_id))


def hostile_result() -> dict:
    return {"verdict": f"refuted{SCRIPT}", "extraction_model": IMG, "revision": SCRIPT, "evidence": [
        {"kind": "symbol", "claim": f"{SCRIPT} exists", "status": "refuted", "basis": f"inferred{IMG}",
         "detail": f"detail {SCRIPT}", "source": f"src/{IMG}.py:1"},
        {"kind": "symbol", "claim": "parse_url() exists", "status": "confirmed", "basis": "observed",
         "detail": "parse_url is defined", "source": "urls.py:1"},
    ]}


# Store listing


def test_store_lists_newest_first_with_status_filter_and_paging(store, db):
    made = [store.create("api", status, {"n": i})[0] for i, status in
            enumerate(["completed", "received", "failed", "completed"])]
    for i, investigation in enumerate(made):
        set_created(db, investigation.id, f"2026-01-0{i + 1}T00:00:00+00:00")
    assert [i.id for i in store.list()] == [m.id for m in reversed(made)]
    assert [i.id for i in store.list(status="completed")] == [made[3].id, made[0].id]
    assert [i.id for i in store.list(limit=2)] == [made[3].id, made[2].id]
    assert [i.id for i in store.list(limit=2, offset=2)] == [made[1].id, made[0].id]
    assert store.list(status="nope") == []


def test_store_list_breaks_created_at_ties_by_insertion_order(store, db):
    made = [store.create("api", "completed", {"n": i})[0] for i in range(3)]
    for investigation in made:
        set_created(db, investigation.id, "2026-01-01T00:00:00+00:00")
    assert [i.id for i in store.list()] == [m.id for m in reversed(made)]


# List page


def test_root_and_dashboard_both_serve_the_list(client, store):
    store.create("api", "completed", {"repo": "sample"}, {"verdict": "refuted", "evidence": []})
    for path in ("/", "/dashboard"):
        response = client.get(path)
        assert response.status_code == 200
        assert_secure(response)
        assert "refuted" in response.text


def test_list_shows_id_source_status_verdict_and_created_newest_first(client, store, db):
    old, _ = store.create("api", "completed", {"repo": "sample"}, {"verdict": "supported", "evidence": []})
    new, _ = store.create("github:issues:octo/widgets#7", "received", {"repo": "octo/widgets"},
                          delivery_id="d-7")
    set_created(db, old.id, "2026-01-01T00:00:00+00:00")
    set_created(db, new.id, "2026-02-01T00:00:00+00:00")
    page = client.get("/dashboard").text
    for text in (old.id, new.id, "github:issues:octo/widgets#7", "supported", "received", "completed",
                 "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"):
        assert text in page
    assert page.index(new.id) < page.index(old.id)
    assert f'href="/dashboard/investigations/{new.id}"' in page


def test_list_filters_by_status(client, store):
    done, _ = store.create("api", "completed", {}, {"verdict": "refuted", "evidence": []})
    waiting, _ = store.create("github:issues:octo/widgets#1", "received", {}, delivery_id="d-1")
    page = client.get("/dashboard", params={"status": "received"}).text
    assert waiting.id in page and done.id not in page
    page = client.get("/", params={"status": "completed"}).text
    assert done.id in page and waiting.id not in page


def test_list_status_filter_value_is_escaped(client):
    response = client.get("/dashboard", params={"status": SCRIPT})
    assert response.status_code == 200
    assert_secure(response)
    assert "<script" not in response.text
    assert "&lt;script&gt;" in response.text


def test_list_is_paginated(client, store, db):
    made = [store.create("api", "completed", {}, {"verdict": "refuted", "evidence": []})[0]
            for _ in range(dashboard.PAGE_SIZE + 3)]
    for i, investigation in enumerate(made):
        set_created(db, investigation.id, f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00")
    first = client.get("/dashboard").text
    assert made[-1].id in first and made[0].id not in first
    assert first.count('href="/dashboard/investigations/') == dashboard.PAGE_SIZE
    assert 'href="/dashboard?page=2"' in first
    second = client.get("/dashboard", params={"page": 2}).text
    assert second.count('href="/dashboard/investigations/') == 3
    assert made[0].id in second
    assert 'href="/dashboard?page=1"' in second and "page=3" not in second


def test_pagination_links_keep_the_status_filter(client, store):
    for _ in range(dashboard.PAGE_SIZE + 1):
        store.create("api", "failed", {})
    page = client.get("/dashboard", params={"status": "failed"}).text
    assert 'href="/dashboard?status=failed&amp;page=2"' in page


@pytest.mark.parametrize("page", ["0", "-1", "abc", "1e3", "99999999999999999999"])
def test_bad_page_number_is_a_400_page(client, page):
    response = client.get("/dashboard", params={"page": page})
    assert response.status_code == 400
    assert_secure(response)


def test_empty_list_says_so(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "No investigations" in response.text


def test_list_escapes_hostile_source_status_and_verdict(client, store):
    store.create(f"github:issues:{IMG}#1", f"weird{SCRIPT}", {}, {"verdict": IMG, "evidence": []})
    response = client.get("/dashboard")
    assert_secure(response)
    page = response.text
    assert "<script" not in page and "<img" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&quot;&gt;&lt;img src=x onerror=alert(2)&gt;" in page


# Detail page


@pytest.mark.parametrize("event, route", [("issues", "issues"), ("pull_request", "pull")])
def test_detail_links_webhook_source_and_shows_short_tested_revision(client, store, event, route):
    revision = "0123456789abcdef0123456789abcdef01234567"
    source = f"github:{event}:Octo-1/widget.repo_2#12"
    request = {"event": "issues", "repo": "someone/else", "number": 99,
               "text": f'{HOSTILE} https://evil.invalid/wrong "https://github.com/someone/else/issues/99"'}
    investigation, _ = store.create(source, "completed", request, {"revision": revision, "evidence": []})
    response = client.get(f"/dashboard/investigations/{investigation.id}")
    assert response.status_code == 200
    assert_secure(response)
    assert_inert(response.text)
    assert f'<code title="{revision}">{revision[:7]}</code>' in response.text
    href = f"https://github.com/Octo-1/widget.repo_2/{route}/12"
    assert f'<a href="{href}" rel="noopener noreferrer">{source}</a>' in response.text
    assert re.findall(r'<a\b[^>]*href="([^"]*)"', response.text) == ["/dashboard", href]


def test_detail_api_without_github_source_shows_short_tested_revision(client, monkeypatch):
    revision = "fedcba9876543210fedcba9876543210fedcba98"
    monkeypatch.setattr("claimcheck.cli.git_revision", lambda repo: revision)
    response = client.post("/v1/investigations", json={
        "repo": "sample", "report": f"{HOSTILE} https://github.com/octo/widgets/issues/12"})
    assert response.status_code == 201
    created = response.json()
    assert created["source"] == "api" and created["result"]["revision"] == revision
    detail = client.get(f"/dashboard/investigations/{created['id']}")
    assert_secure(detail)
    assert_inert(detail.text)
    assert f'<code title="{revision}">{revision[:7]}</code>' in detail.text
    assert re.findall(r'<a\b[^>]*href="([^"]*)"', detail.text) == ["/dashboard"]


@pytest.mark.parametrize("source", [
    f"github:issues:octo/widgets{IMG}#12", f"github:pull_request:octo/{SCRIPT}#12",
    "github:issues:https://evil.invalid/widgets#12", "github:issues:octo/../widgets#12",
    "github:issues:octo/..#12", "github:issues:octo/.#12",
    "github:issues:octo/widgets#12?next=https://evil.invalid", "github:issues:octo/widgets#12\n",
    "prefixgithub:issues:octo/widgets#12", "github:issues:octo/widgets#12/suffix",
    "github:issues:octo/widgets#0", "github:issues:octo/widgets#-12",
    "github:issues:octo/widgets#１２", "github:issues:octo/widgets%22#12",
    "github:push:octo/widgets#12", "octo/widgets#12",
])
def test_detail_rejects_malformed_github_source_and_escapes_it(client, store, source):
    investigation, _ = store.create(source, "received", {
        "event": "issues", "repo": "octo/widgets", "number": 12,
        "text": "https://github.com/octo/widgets/issues/12"})
    response = client.get(f"/dashboard/investigations/{investigation.id}")
    assert response.status_code == 200
    assert_secure(response)
    assert f"<dt>Source</dt><dd>{dashboard.e(source)}</dd>" in response.text
    assert re.findall(r'<a\b[^>]*href="([^"]*)"', response.text) == ["/dashboard"]
    assert "<script" not in response.text and "<img" not in response.text


def test_detail_revision_tooltip_and_short_text_are_escaped(client, store):
    revision = f"{IMG}{SCRIPT}"
    investigation, _ = store.create("api", "completed", {}, {"revision": revision, "evidence": []})
    response = client.get(f"/dashboard/investigations/{investigation.id}")
    assert_secure(response)
    assert_inert(response.text)
    assert f'<code title="{dashboard.e(revision)}">{dashboard.e(revision[:7])}</code>' in response.text


@pytest.mark.parametrize("revision", [None, "", [SCRIPT], {"sha": IMG}])
def test_detail_missing_or_malformed_revision_remains_inert(client, store, revision):
    investigation, _ = store.create("api", "completed", {}, {"revision": revision, "evidence": []})
    response = client.get(f"/dashboard/investigations/{investigation.id}")
    assert response.status_code == 200
    assert_secure(response)
    assert f"<dt>Revision</dt><dd>{dashboard.block(revision)}</dd>" in response.text
    assert "<script" not in response.text and "<img" not in response.text


def test_detail_shows_each_claim_with_status_basis_detail_and_source(client, store):
    investigation, _ = store.create("api", "completed", {"repo": "sample", "report": "RCE"}, {
        "verdict": "refuted", "extraction_model": None, "revision": "abc123", "evidence": [
            {"kind": "symbol", "claim": "parse_url_v2() exists", "status": "refuted", "basis": "inferred",
             "detail": "parse_url_v2 does not appear in any of 1 source files", "source": "sample"},
            {"kind": "symbol", "claim": "parse_url() exists", "status": "confirmed", "basis": "observed",
             "detail": "parse_url is defined", "source": "urls.py:1"}]})
    response = client.get(f"/dashboard/investigations/{investigation.id}")
    assert response.status_code == 200
    assert_secure(response)
    page = response.text
    for text in (investigation.id, "api", "completed", "refuted", "abc123", "parse_url_v2() exists", "inferred",
                 "parse_url_v2 does not appear in any of 1 source files", "parse_url() exists", "confirmed",
                 "observed", "parse_url is defined", "urls.py:1", "RCE", "sample"):
        assert text in page
    assert 'href="/dashboard"' in page


def test_detail_shows_error_and_worker(client, store, db):
    investigation, _ = store.create("github:issues:octo/widgets#3", "received", {"repo": "octo/widgets"},
                                    delivery_id="d-3")
    claimed = store.claim(f"host:{SCRIPT}", stale_after=600)
    store.finish(claimed.id, f"host:{SCRIPT}", "failed", error=f"boom {IMG}")
    response = client.get(f"/dashboard/investigations/{investigation.id}")
    assert_secure(response)
    page = response.text
    assert "failed" in page and "d-3" in page
    assert "host:&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "boom &quot;&gt;&lt;img src=x onerror=alert(2)&gt;" in page
    assert "<script" not in page and "<img" not in page


def test_detail_escapes_every_report_and_evidence_field(client, store):
    request = {"repo": f"sample{IMG}", "report": HOSTILE, "diff": f"+{SCRIPT}"}
    investigation, _ = store.create(f"api{SCRIPT}", "completed", request, hostile_result())
    response = client.get(f"/dashboard/investigations/{investigation.id}")
    assert response.status_code == 200
    assert_secure(response)
    assert_inert(response.text)
    assert "detail &lt;script&gt;" in response.text
    assert "src/&quot;&gt;&lt;img src=x onerror=alert(2)&gt;.py:1" in response.text


def test_detail_survives_results_of_an_unexpected_shape(client, store):
    odd = [store.create("api", "completed", {"x": [SCRIPT]}, result)[0] for result in
           ({"evidence": "not a list"}, {"evidence": [SCRIPT, None, {"claim": 3}]}, {"verdict": {"a": SCRIPT}}, [1, 2])]
    for investigation in odd:
        response = client.get(f"/dashboard/investigations/{investigation.id}")
        assert response.status_code == 200
        assert "<script" not in response.text


def test_webhook_recorded_event_renders_inert(client):
    body = issue_body()
    response = client.post("/v1/github/webhook", content=body, headers={
        "X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-evil", "Content-Type": "application/json",
        "X-Hub-Signature-256": sign(body)})
    assert response.status_code == 202
    investigation_id = response.json()["id"]
    listing = client.get("/dashboard")
    assert investigation_id in listing.text and "<script" not in listing.text
    detail = client.get(f"/dashboard/investigations/{investigation_id}")
    assert detail.status_code == 200
    assert_secure(detail)
    assert_inert(detail.text)
    assert "parse_url_v2" in detail.text  # the claims the extractor found


def test_api_created_investigation_renders_inert(client):
    response = client.post("/v1/investigations", json={"repo": "sample", "report": HOSTILE})
    assert response.status_code == 201
    created = response.json()
    detail = client.get(f"/dashboard/investigations/{created['id']}")
    assert_secure(detail)
    assert_inert(detail.text)
    assert "parse_url is defined" in detail.text and "urls.py:1" in detail.text


@pytest.mark.parametrize("path", ["/dashboard/investigations/nope", f"/dashboard/investigations/{'0' * 32}",
                                  "/dashboard/investigations/%3Cscript%3Ealert(1)%3C%2Fscript%3E"])
def test_unknown_investigation_is_a_404_page(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert_secure(response)
    assert "Not found" in response.text
    assert "<script" not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("path", ["/", "/dashboard", "/dashboard/investigations/x"])
def test_dashboard_is_read_only(client, method, path):
    assert client.request(method, path).status_code == 405


def test_pages_have_no_forms_scripts_or_external_resources(client, store):
    investigation, _ = store.create("api", "completed", {}, hostile_result())
    for path in ("/", "/dashboard", f"/dashboard/investigations/{investigation.id}"):
        page = client.get(path).text.lower()
        assert "<form" not in page and "<script" not in page and "<link" not in page
        assert "http://" not in page and "https://" not in page


def test_json_api_is_unchanged(client, store):
    investigation, _ = store.create("api", "completed", {"repo": "sample"}, {"verdict": "refuted"})
    response = client.get(f"/v1/investigations/{investigation.id}")
    assert response.headers["content-type"] == "application/json"
    assert response.json() == investigation.to_dict()
    assert client.get("/v1/investigations/nope").json() == {"detail": "no such investigation"}


# The real server over real HTTP


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(base: str, path: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None):
    request = urllib.request.Request(base + path, body, headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, dict(error.headers), error.read().decode()


class LiveResponse:
    def __init__(self, status: int, headers: dict, text: str):
        self.status_code, self.text = status, text
        self.headers = {name.lower(): value for name, value in headers.items()}


@pytest.fixture
def live(tmp_path, root):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = open(tmp_path / "server.log", "w+")
    process = subprocess.Popen(
        [sys.executable, "-m", "claimcheck.server", "--db", str(tmp_path / "live.db"), "--repo-root", str(root),
         "--port", str(port)], cwd=tmp_path, env={**os.environ, "CLAIMCHECK_WEBHOOK_SECRET": SECRET,
                                                     "CLAIMCHECK_API_TOKEN": ""},
        stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None or time.monotonic() > deadline:
                log.seek(0)
                pytest.fail(f"server did not start:\n{log.read()}")
            try:
                if fetch(base, "/dashboard")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        yield base
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        log.close()


def test_live_webhook_with_hostile_title_renders_escaped(live):
    body = issue_body()
    status, _, text = fetch(live, "/v1/github/webhook", "POST", body, {
        "Content-Type": "application/json", "X-GitHub-Event": "issues", "X-GitHub-Delivery": "live-evil",
        "X-Hub-Signature-256": sign(body)})
    assert status == 202
    investigation_id = json.loads(text)["id"]

    listing = LiveResponse(*fetch(live, "/dashboard?status=received"))
    assert listing.status_code == 200
    assert_secure(listing)
    assert investigation_id in listing.text and "github:issues:octo/widgets#12" in listing.text

    detail = LiveResponse(*fetch(live, f"/dashboard/investigations/{investigation_id}"))
    assert detail.status_code == 200
    assert_secure(detail)
    assert_inert(detail.text)
    assert ('<a href="https://github.com/octo/widgets/issues/12" rel="noopener noreferrer">'
            'github:issues:octo/widgets#12</a>') in detail.text


def test_live_api_investigation_with_hostile_report_renders_escaped(live):
    status, _, text = fetch(live, "/v1/investigations", "POST", json.dumps({"repo": "sample", "report": HOSTILE}).encode(),
                            {"Content-Type": "application/json"})
    assert status == 201
    created = json.loads(text)
    detail = LiveResponse(*fetch(live, f"/dashboard/investigations/{created['id']}"))
    assert detail.status_code == 200
    assert_secure(detail)
    assert_inert(detail.text)
    assert "parse_url is defined" in detail.text
    assert '<dt>Revision</dt><dd><span class="muted">-</span></dd>' in detail.text
    assert re.findall(r'<a\b[^>]*href="([^"]*)"', detail.text) == ["/dashboard"]

    home = LiveResponse(*fetch(live, "/"))
    assert_secure(home)
    assert created["id"] in home.text and "refuted" in home.text
    # The JSON API still answers with the same JSON.
    status, headers, text = fetch(live, f"/v1/investigations/{created['id']}")
    assert status == 200 and json.loads(text) == created


def test_live_unknown_id_and_mutating_methods(live):
    missing = LiveResponse(*fetch(live, "/dashboard/investigations/does-not-exist"))
    assert missing.status_code == 404
    assert_secure(missing)
    assert fetch(live, "/dashboard", "POST", b"")[0] == 405
