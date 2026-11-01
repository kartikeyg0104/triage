"""Repository intake caps must hold across requests and server processes sharing SQLite."""

import hashlib
import hmac
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from claimcheck.server import store as storage
from claimcheck.server import webhook
from claimcheck.server.app import create_app
from claimcheck.server.store import Store
from test_server_http import Server

SECRET = "limits-secret"
PENDING_ENV = "CLAIMCHECK_WEBHOOK_MAX_PENDING"
RATE_ENV = "CLAIMCHECK_WEBHOOK_MAX_PER_MINUTE"


@pytest.fixture(autouse=True)
def clean_limits(monkeypatch):
    monkeypatch.delenv(PENDING_ENV, raising=False)
    monkeypatch.delenv(RATE_ENV, raising=False)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "claimcheck.db"


def body(repo: str = "octo/widgets", event: str = "issues") -> bytes:
    key = webhook.PAYLOAD_KEY[event]
    return json.dumps({"action": "opened", "repository": {"full_name": repo},
                       key: {"number": 1, "title": "Claim", "body": "parse_url() exists"}}).encode()


def post(client: TestClient, delivery: str, repo: str = "octo/widgets", event: str = "issues",
         payload: bytes | None = None, headers: dict | None = None):
    raw = body(repo, event) if payload is None else payload
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/v1/github/webhook", content=raw, headers={
        "Content-Type": "application/json", "X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature, **(headers or {})})


def client_for(db, root, pending: int = 100, rate: int = 60) -> TestClient:
    return TestClient(create_app(db, root, SECRET, webhook_max_pending=pending, webhook_max_per_minute=rate))


def finish(store: Store, status: str = "completed") -> None:
    row = store.claim("worker", stale_after=60)
    assert row is not None
    assert store.finish(row.id, "worker", status)


def test_pending_cap_is_shared_by_event_and_repo_casing_and_uses_verified_body(db, tmp_path):
    client = client_for(db, tmp_path, pending=2, rate=20)
    first = post(client, "first")
    assert first.status_code == 202
    assert post(client, "second", "OCTO/Widgets", "pull_request").status_code == 202
    blocked = post(client, "third", headers={"X-GitHub-Repository": "other/widgets"})
    assert blocked.status_code == 429 and "pending" in blocked.json()["detail"]
    again = post(client, "first")
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    assert again.json()["duplicate"] is True
    assert post(client, "other-owner", "other/widgets").status_code == 202
    assert post(client, "other-name", "octo/other").status_code == 202
    assert len(Store(db).list()) == 4


@pytest.mark.parametrize("status", ["completed", "skipped", "failed"])
def test_processing_holds_pending_slot_until_worker_finishes(db, tmp_path, status):
    client = client_for(db, tmp_path, pending=1, rate=20)
    first = post(client, "first").json()
    store = Store(db)
    assert store.claim("worker", stale_after=60).id == first["id"]
    assert post(client, "next").status_code == 429
    duplicate = post(client, "first")
    assert duplicate.status_code == 202 and duplicate.json()["status"] == "processing"
    assert store.finish(first["id"], "worker", status)
    assert post(client, "next").status_code == 202
    assert len(store.list()) == 2


def test_rolling_minute_counts_finished_deliveries_and_duplicates_do_not_use_slots(db, tmp_path, monkeypatch):
    now = [1000.25]
    monkeypatch.setattr(storage.time, "time", lambda: now[0])
    client = client_for(db, tmp_path, pending=20, rate=2)
    first = post(client, "first")
    assert first.status_code == 202
    finish(Store(db), "failed")
    for _ in range(3):
        duplicate = post(client, "first")
        assert duplicate.status_code == 202 and duplicate.json()["id"] == first.json()["id"]
    now[0] += 30
    assert post(client, "second", "OCTO/Widgets", "pull_request").status_code == 202
    finish(Store(db), "skipped")
    blocked = post(client, "third")
    assert blocked.status_code == 429 and "minute" in blocked.json()["detail"]
    assert post(client, "other-owner", "other/widgets").status_code == 202
    assert post(client, "other-name", "octo/other").status_code == 202
    now[0] = 1060.249999
    assert post(client, "third").status_code == 429
    now[0] = 1060.25
    assert post(client, "third").status_code == 202
    assert post(client, "fourth").status_code == 429
    assert len(Store(db).list()) == 5


@pytest.mark.parametrize("kind, count", [("pending", 100), ("rate", 60)])
def test_default_limits(db, tmp_path, kind, count):
    client = TestClient(create_app(db, tmp_path, SECRET))
    store = Store(db)
    for n in range(count):
        if kind == "pending":
            store.create("github:issues:octo/widgets#1", "received", {"repo": "octo/widgets"},
                         delivery_id=f"seed-{n}")
        else:
            assert post(client, f"seed-{n}").status_code == 202
            finish(store)
    assert post(client, "over-limit").status_code == 429
    assert post(client, "other", "other/widgets").status_code == 202
    assert len(store.list(limit=200)) == count + 1


@pytest.mark.parametrize("env", [PENDING_ENV, RATE_ENV])
def test_limits_can_be_configured_from_environment(db, tmp_path, monkeypatch, env):
    monkeypatch.setenv(env, "1")
    client = TestClient(create_app(db, tmp_path, SECRET))
    assert post(client, "first").status_code == 202
    assert post(client, "second").status_code == 429


@pytest.mark.parametrize("env", [PENDING_ENV, RATE_ENV])
@pytest.mark.parametrize("value", ["0", "-1", "invalid", "1.5", ""])
def test_invalid_environment_limit_fails_at_app_creation(db, tmp_path, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError, match=env):
        create_app(db, tmp_path, SECRET)


@pytest.mark.parametrize("option", ["webhook_max_pending", "webhook_max_per_minute"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_explicit_limits_must_be_positive_integers(db, tmp_path, option, value):
    with pytest.raises(ValueError, match="positive integer"):
        create_app(db, tmp_path, SECRET, **{option: value})


def test_explicit_limits_override_environment(db, tmp_path, monkeypatch):
    monkeypatch.setenv(PENDING_ENV, "1")
    monkeypatch.setenv(RATE_ENV, "1")
    client = client_for(db, tmp_path, pending=2, rate=2)
    assert post(client, "first").status_code == 202
    assert post(client, "second").status_code == 202
    assert post(client, "third").status_code == 429


@pytest.mark.parametrize("repo", ["widgets", "/widgets", "octo/", "octo/widgets/extra", "octo/..",
                                   "octo/widget name", "octo/widgets#1", "octo/widgets\x00"])
def test_payload_repository_must_be_owner_repo(db, tmp_path, repo):
    client = TestClient(create_app(db, tmp_path, SECRET))
    assert post(client, "bad", repo).status_code == 400
    assert Store(db).list() == []


def test_unverified_malformed_and_ignored_events_do_not_consume_limits(db, tmp_path):
    client = client_for(db, tmp_path, pending=1, rate=1)
    assert post(client, "bad-signature", headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    assert post(client, "bad-json", payload=b"not json").status_code == 400
    assert post(client, "ignored", headers={"X-GitHub-Event": "ping"}).status_code == 202
    assert post(client, "missing-id", headers={"X-GitHub-Delivery": ""}).status_code == 400
    assert post(client, "first").status_code == 202
    assert post(client, "bad-signature", headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    assert post(client, "bad-json", payload=b"not json").status_code == 400
    assert len(Store(db).list()) == 1


@pytest.mark.parametrize("pending, rate", [(3, 30), (30, 3)])
def test_concurrent_apps_cannot_exceed_either_limit(db, tmp_path, pending, rate):
    clients = [client_for(db, tmp_path, pending, rate) for _ in range(10)]
    ready = threading.Barrier(len(clients))

    def deliver(n: int):
        ready.wait(timeout=10)
        return post(clients[n], f"delivery-{n}")

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        responses = list(pool.map(deliver, range(len(clients))))
    assert sorted(r.status_code for r in responses) == [202] * 3 + [429] * 7
    assert len(Store(db).list()) == 3


def test_concurrent_duplicates_bypass_full_limits(db, tmp_path):
    clients = [client_for(db, tmp_path, pending=1, rate=1) for _ in range(8)]
    ready = threading.Barrier(len(clients))

    def deliver(n: int):
        ready.wait(timeout=10)
        return post(clients[n], "same-delivery")

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        responses = list(pool.map(deliver, range(len(clients))))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum(not r.json()["duplicate"] for r in responses) == 1
    assert len(Store(db).list()) == 1


@pytest.mark.parametrize("pending, rate, status", [(1, 20, "received"), (20, 1, "completed")])
def test_existing_rows_count_toward_limits(db, tmp_path, monkeypatch, pending, rate, status):
    now = 1000.5
    store = Store(db)
    row, _ = store.create("github:issues:OCTO/Widgets#1", status, {"repo": "OCTO/Widgets"}, delivery_id="old")
    timestamp = datetime.fromtimestamp(1000, timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE investigations SET created_at = ? WHERE id = ?", (timestamp, row.id))
    monkeypatch.setattr(storage.time, "time", lambda: now)
    client = client_for(db, tmp_path, pending, rate)
    assert post(client, "new").status_code == 429
    assert post(client, "old").json()["id"] == row.id


@pytest.mark.parametrize("pending, rate", [(2, 20), (20, 2)])
def test_real_http_limits_survive_restart_and_leave_other_repos_unaffected(db, tmp_path, monkeypatch,
                                                                          pending, rate):
    monkeypatch.setenv(PENDING_ENV, str(pending))
    monkeypatch.setenv(RATE_ENV, str(rate))
    server = Server(db, tmp_path, tmp_path, secret=SECRET).start()
    try:
        status, first = server.webhook(body(), delivery="first")
        assert status == 202
        assert server.webhook(body("OCTO/Widgets", "pull_request"), event="pull_request", delivery="second")[0] == 202
        server.stop()
        server = Server(db, tmp_path, tmp_path, secret=SECRET).start()
        assert server.webhook(body(), delivery="third")[0] == 429
        status, duplicate = server.webhook(body(), delivery="first")
        assert status == 202 and duplicate["id"] == first["id"] and duplicate["duplicate"] is True
        assert server.webhook(body("other/widgets"), delivery="other-owner")[0] == 202
        assert server.webhook(body("octo/other"), delivery="other-name")[0] == 202
        assert len(Store(db).list()) == 4
    finally:
        server.stop()
