"""Service API and GitHub webhook, in process through FastAPI's TestClient."""

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from claimcheck import sandbox
from claimcheck.server import __main__ as serve
from claimcheck.server import webhook
from claimcheck.server.app import MAX_BODY, create_app
from claimcheck.server.store import Store

SECRET = "s3cret"
REPORT = "Crash in `parse_url()` and RCE in parse_url_v2()"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


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
def client(root, db):
    return TestClient(create_app(db, root, SECRET))


def event_body(event="issues", number=12, body=REPORT) -> bytes:
    key = "issue" if event == "issues" else "pull_request"
    return json.dumps({"action": "opened", key: {"number": number, "title": "Crash", "body": body},
                       "repository": {"full_name": "octo/widgets"}}).encode()


def post_webhook(client, body, event="issues", delivery="d-1", signature=None):
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if delivery:
        headers["X-GitHub-Delivery"] = delivery
    if signature is not False:
        headers["X-Hub-Signature-256"] = signature or sign(body)
    return client.post("/v1/github/webhook", content=body, headers=headers)


def rows(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT source, delivery_id, status FROM investigations").fetchall()


# Signature verification


def test_valid_signature_verifies():
    body = event_body()
    assert webhook.verify_signature(SECRET, body, sign(body))


@pytest.mark.parametrize("header", [
    None,
    "",
    "sha256=",
    "sha256=" + "0" * 64,
    sign(b"other body"),
    sign(event_body(), "wrong-secret"),
    sign(event_body()).removeprefix("sha256="),
    sign(event_body()).replace("sha256=", "sha1="),
    sign(event_body()).upper(),
    "sha256=é" + "0" * 63,
])
def test_bad_missing_or_wrong_prefix_signature_does_not_verify(header):
    assert not webhook.verify_signature(SECRET, event_body(), header)


def test_empty_secret_never_verifies_even_a_matching_empty_key_hmac():
    body = event_body()
    assert not webhook.verify_signature("", body, sign(body, ""))


# Webhook endpoint


def test_signed_issue_event_is_recorded_without_running_anything(client, db):
    response = post_webhook(client, event_body())
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "received" and data["duplicate"] is False
    stored = client.get(f"/v1/investigations/{data['id']}").json()
    assert stored["source"] == "github:issues:octo/widgets#12"
    assert stored["status"] == "received"
    assert stored["request"]["claims"] == ["parse_url", "parse_url_v2"]
    assert stored["result"] is None
    assert rows(db) == [("github:issues:octo/widgets#12", "d-1", "received")]


def test_signed_pull_request_event_is_recorded(client, db):
    response = post_webhook(client, event_body("pull_request", number=5), event="pull_request")
    assert response.status_code == 202
    assert rows(db) == [("github:pull_request:octo/widgets#5", "d-1", "received")]


@pytest.mark.parametrize("signature", [
    False,
    "sha256=" + "0" * 64,
    sign(event_body(), "wrong-secret"),
    sign(event_body()).replace("sha256=", "sha1="),
    "sha256=é".encode() + b"0" * 62,
])
def test_missing_or_bad_signature_is_401_and_nothing_is_stored(client, db, signature):
    assert post_webhook(client, event_body(), signature=signature).status_code == 401
    assert rows(db) == []


def test_unsigned_unsupported_event_is_still_401(client, db):
    assert post_webhook(client, b"{}", event="push", signature=False).status_code == 401


def test_empty_secret_refuses_every_webhook(root, db):
    client = TestClient(create_app(db, root, ""))
    body = event_body()
    for signature in (sign(body, ""), False):
        assert post_webhook(client, body, signature=signature).status_code == 503
    assert rows(db) == []


@pytest.mark.parametrize("event", ["push", "ping", "issue_comment", ""])
def test_signed_unsupported_event_is_ignored(client, db, event):
    response = post_webhook(client, event_body(), event=event)
    assert response.status_code == 202
    assert response.json() == {"status": "ignored", "event": event}
    assert rows(db) == []


def test_same_delivery_is_recorded_once(client, db):
    first = post_webhook(client, event_body(), delivery="d-7").json()
    again = post_webhook(client, event_body(), delivery="d-7").json()
    other = post_webhook(client, event_body(), delivery="d-8").json()
    assert again["id"] == first["id"] and again["duplicate"] is True
    assert other["id"] != first["id"]
    assert len(rows(db)) == 2


def test_missing_delivery_id_is_400(client, db):
    assert post_webhook(client, event_body(), delivery=None).status_code == 400
    assert rows(db) == []


@pytest.mark.parametrize("body", [
    b"not json",
    b"[]",
    json.dumps({"action": "opened", "repository": {"full_name": "octo/widgets"}}).encode(),
    json.dumps({"issue": {"number": "12", "title": "t"}, "repository": {"full_name": "octo/widgets"}}).encode(),
    json.dumps({"issue": {"number": 12, "title": "t"}, "repository": {}}).encode(),
])
def test_malformed_signed_payload_is_400(client, db, body):
    assert post_webhook(client, body).status_code == 400
    assert rows(db) == []


def test_webhook_never_executes_text_from_the_event(client, db, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"the webhook tried to run a process: {args}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(sandbox, "run_repro", refuse)
    monkeypatch.chdir(tmp_path)
    text = "To reproduce run `touch pwned` or $(touch pwned2); touch pwned3 in parse_url()"
    response = post_webhook(client, event_body(body=text))
    assert response.status_code == 202
    assert not list(tmp_path.rglob("pwned*"))


# Body limit


@pytest.mark.parametrize("path", ["/v1/investigations", "/v1/github/webhook"])
def test_body_over_the_limit_is_413(client, db, path):
    body = b"x" * (MAX_BODY + 1)
    headers = {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d", "X-Hub-Signature-256": sign(body)}
    assert client.post(path, content=body, headers=headers).status_code == 413
    assert rows(db) == []


def test_streamed_body_without_a_length_is_cut_off_at_the_limit(client):
    chunks = (b"x" * 65536 for _ in range(MAX_BODY // 65536 + 2))
    response = client.post("/v1/investigations", content=chunks)
    assert response.status_code == 413


# Investigations API


def test_post_investigation_runs_the_check_and_get_returns_it(client):
    response = client.post("/v1/investigations", json={"repo": "sample", "report": REPORT})
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "completed" and data["source"] == "api"
    assert data["result"]["verdict"] == "refuted"
    assert [(e["claim"], e["status"]) for e in data["result"]["evidence"]] == [
        ("parse_url() exists", "confirmed"), ("parse_url_v2() exists", "refuted")]
    assert data["result"]["extraction_model"] is None
    assert client.get(f"/v1/investigations/{data['id']}").json() == data


def test_post_investigation_with_a_diff_checks_tests(client):
    diff = "--- a/urls.py\n+++ b/urls.py\n"
    data = client.post("/v1/investigations", json={"repo": "sample", "report": REPORT, "diff": diff}).json()
    assert "pr_tests" in [e["kind"] for e in data["result"]["evidence"]]


@pytest.mark.parametrize("repo", ["..", "../..", "sample/../..", "/etc", "/", "nope", "sample/urls.py", "a\x00b"])
def test_repo_outside_root_or_missing_is_400(client, db, repo):
    assert client.post("/v1/investigations", json={"repo": repo, "report": REPORT}).status_code == 400
    assert rows(db) == []


def test_symlink_out_of_repo_root_is_400(client, root, tmp_path):
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    assert client.post("/v1/investigations", json={"repo": "escape", "report": REPORT}).status_code == 400


def test_path_that_stays_inside_root_is_allowed(client):
    assert client.post("/v1/investigations", json={"repo": "sample/../sample", "report": REPORT}).status_code == 201


def test_api_never_runs_a_repro(client, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox, "run_repro", lambda *a, **k: calls.append(a))
    monkeypatch.chdir(tmp_path)
    report = "run `touch pwned` to reproduce parse_url()"
    assert client.post("/v1/investigations", json={"repo": "sample", "report": report}).status_code == 201
    response = client.post("/v1/investigations", json={"repo": "sample", "report": report, "repro": "touch pwned"})
    assert response.status_code == 422
    assert calls == []
    assert not list(tmp_path.rglob("pwned*"))


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"repo": "sample"}', b'{"repo": 1, "report": "x"}'])
def test_invalid_investigation_request_is_422(client, body):
    response = client.post("/v1/investigations", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_unknown_investigation_is_404(client):
    assert client.get("/v1/investigations/does-not-exist").status_code == 404


# Store


def test_store_round_trip_survives_reopening(db):
    created, is_new = Store(db).create("api", "completed", {"repo": "r"}, {"verdict": "supported"})
    assert is_new
    again = Store(db).get(created.id)
    assert again == created
    assert again.request == {"repo": "r"} and again.result == {"verdict": "supported"}
    assert Store(db).get("missing") is None


def test_store_create_is_idempotent_per_delivery(db):
    store = Store(db)
    first, new_first = store.create("github:issues:o/r#1", "received", {"n": 1}, delivery_id="abc")
    second, new_second = store.create("github:issues:o/r#1", "received", {"n": 2}, delivery_id="abc")
    assert (new_first, new_second) == (True, False)
    assert second == first
    store.create("api", "completed", {}, {})
    store.create("api", "completed", {}, {})
    assert len(rows(db)) == 3


def test_store_schema(db):
    Store(db)
    with closing(sqlite3.connect(db)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(investigations)")]
    assert columns == ["id", "created_at", "source", "delivery_id", "status", "request", "result", "error", "worker",
                       "claimed_at", "attempts"]


# Entry point


def test_serve_binds_localhost_by_default_and_reads_the_secret_from_env(root, db, monkeypatch):
    seen = {}
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    monkeypatch.setenv("CLAIMCHECK_WEBHOOK_SECRET", "from-env")
    assert serve.main(["--db", str(db), "--repo-root", str(root)]) == 0
    assert seen["host"] == "127.0.0.1"
    body = event_body()
    response = post_webhook(TestClient(seen["app"]), body, signature=sign(body, "from-env"))
    assert response.status_code == 202


def test_serve_refuses_a_missing_repo_root(tmp_path, db, monkeypatch, capsys):
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: pytest.fail("should not start"))
    assert serve.main(["--db", str(db), "--repo-root", str(tmp_path / "nope")]) == 2
    assert "repo root" in capsys.readouterr().err
