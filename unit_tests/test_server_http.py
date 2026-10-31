"""The real service over real HTTP: `python -m claimcheck.server` (uvicorn) in a subprocess,
urllib as the client, and a SQLite file on disk that is read directly and reopened after a restart."""

import hashlib
import hmac
import http.client
import json
import os
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

SECRET = "integration-secret"
REPORT = "Crash in `parse_url()` and RCE in parse_url_v2()"


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def call(base: str, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    request = urllib.request.Request(base + path, body, headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read() or b"null")


class Server:
    """One `python -m claimcheck.server` process on a free localhost port."""

    def __init__(self, db: Path, root: Path, workdir: Path, secret: str = SECRET, token: str = ""):
        self.db, self.root, self.workdir, self.secret, self.token = db, root, workdir, secret, token
        self.process = None

    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def start(self) -> "Server":
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = {**os.environ, "CLAIMCHECK_WEBHOOK_SECRET": self.secret, "CLAIMCHECK_API_TOKEN": self.token}
        self.log = open(self.workdir / f"server-{self.port}.log", "w+")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "claimcheck.server", "--db", str(self.db), "--repo-root", str(self.root),
             "--port", str(self.port)], cwd=self.workdir, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail(f"server exited early:\n{self.output()}")
            try:
                if call(self.base, "GET", "/v1/investigations/ready-probe", headers=self.auth())[0] == 404:
                    return self
            except OSError:
                time.sleep(0.1)
        pytest.fail(f"server did not start:\n{self.output()}")

    def output(self) -> str:
        self.log.seek(0)
        return self.log.read()

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.log.close()

    def webhook(self, body: bytes, event: str = "issues", delivery: str = "d-1", signature: str | None = None):
        headers = {"Content-Type": "application/json", "X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
        if signature != "":
            headers["X-Hub-Signature-256"] = signature or sign(body, self.secret)
        return call(self.base, "POST", "/v1/github/webhook", body, headers)


@pytest.fixture
def workdir(tmp_path):
    repo = tmp_path / "repos" / "sample"
    repo.mkdir(parents=True)
    (repo / "urls.py").write_text("def parse_url(s):\n    return s\n")
    return tmp_path


@pytest.fixture
def server(workdir):
    running = Server(workdir / "claimcheck.db", workdir / "repos", workdir).start()
    yield running
    running.stop()


def rows(db: Path):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT id, source, delivery_id, status, request, result FROM investigations").fetchall()


def issue_body(body: str = REPORT) -> bytes:
    # Deliberately not json.dumps defaults: the signature must cover these exact bytes, spaces and all.
    return json.dumps({"action": "opened", "issue": {"number": 12, "title": "Crash ü", "body": body},
                       "repository": {"full_name": "octo/widgets"}}, indent=1, ensure_ascii=False).encode()


def test_investigation_is_persisted_in_sqlite_and_survives_a_restart(server, workdir):
    status, created = call(server.base, "POST", "/v1/investigations",
                           json.dumps({"repo": "sample", "report": REPORT}).encode(),
                           {"Content-Type": "application/json"})
    assert status == 201
    assert created["status"] == "completed" and created["result"]["verdict"] == "refuted"
    assert call(server.base, "GET", f"/v1/investigations/{created['id']}") == (200, created)

    [(row_id, source, delivery, row_status, request, result)] = rows(server.db)
    assert (row_id, source, delivery, row_status) == (created["id"], "api", None, "completed")
    assert json.loads(request)["repo"] == "sample"
    assert json.loads(result) == created["result"]

    server.stop()
    restarted = Server(server.db, server.root, workdir).start()
    try:
        assert call(restarted.base, "GET", f"/v1/investigations/{created['id']}") == (200, created)
    finally:
        restarted.stop()


def test_signed_webhook_is_recorded_once_per_delivery(server):
    body = issue_body()
    status, first = server.webhook(body, delivery="delivery-42")
    assert status == 202 and first["status"] == "received"
    status, again = server.webhook(body, delivery="delivery-42")
    assert status == 202 and again["id"] == first["id"] and again["duplicate"] is True

    [(row_id, source, delivery, row_status, request, result)] = rows(server.db)
    assert (row_id, source, delivery, row_status) == (first["id"], "github:issues:octo/widgets#12", "delivery-42",
                                                      "received")
    assert json.loads(request)["claims"] == ["parse_url", "parse_url_v2"]
    assert result is None
    status, stored = call(server.base, "GET", f"/v1/investigations/{first['id']}")
    assert status == 200 and stored["source"] == "github:issues:octo/widgets#12"


def test_unsigned_and_badly_signed_webhooks_are_rejected(server):
    body = issue_body()
    assert server.webhook(body, signature="")[0] == 401
    assert server.webhook(body, signature=sign(body, "not-the-secret"))[0] == 401
    assert server.webhook(body, signature=sign(body + b" "))[0] == 401
    assert rows(server.db) == []


def test_unsupported_event_is_ignored(server):
    assert server.webhook(b'{"zen": "hi"}', event="ping") == (202, {"status": "ignored", "event": "ping"})
    assert rows(server.db) == []


def test_webhook_text_is_never_executed(server, workdir):
    body = issue_body("Reproduce with `touch pwned` or $(touch pwned2); touch pwned3 && echo in parse_url()")
    assert server.webhook(body, delivery="d-exec")[0] == 202
    assert not list(workdir.rglob("pwned*"))


def test_declared_oversized_body_is_413(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.putrequest("POST", "/v1/investigations")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(5 * 1024 * 1024))
        conn.endheaders()
        assert conn.getresponse().status == 413
    finally:
        conn.close()


def test_server_without_a_secret_refuses_webhooks(workdir):
    server = Server(workdir / "nosecret.db", workdir / "repos", workdir, secret="").start()
    try:
        body = issue_body()
        assert server.webhook(body, signature=sign(body, ""))[0] == 503
        assert server.webhook(body, signature="")[0] == 503
        assert rows(server.db) == []
    finally:
        server.stop()


# Bearer token (CLAIMCHECK_API_TOKEN)

TOKEN = "http-test-token"


def fetch(base: str, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    """Status, lower-cased headers and text, for HTML and JSON alike."""
    request = urllib.request.Request(base + path, body, headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read().decode()


@pytest.fixture
def token_server(workdir):
    running = Server(workdir / "token.db", workdir / "repos", workdir, token=TOKEN).start()
    yield running
    running.stop()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
def test_api_needs_the_bearer_token(token_server, headers):
    body = json.dumps({"repo": "sample", "report": REPORT}).encode()
    status, response_headers, text = fetch(token_server.base, "POST", "/v1/investigations", body,
                                           {"Content-Type": "application/json", **headers})
    assert status == 401 and response_headers["www-authenticate"] == "Bearer"
    assert "detail" in json.loads(text)
    status, response_headers, _ = fetch(token_server.base, "GET", "/v1/investigations/anything", headers=headers)
    assert status == 401 and response_headers["www-authenticate"] == "Bearer"
    assert rows(token_server.db) == []


@pytest.mark.parametrize("path", ["/", "/dashboard", "/dashboard/investigations/x"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_dashboard_needs_the_bearer_token_and_401_page_keeps_security_headers(token_server, path, headers):
    status, response_headers, text = fetch(token_server.base, "GET", path, headers=headers)
    assert status == 401 and response_headers["www-authenticate"] == "Bearer"
    assert response_headers["content-type"].startswith("text/html") and "<h1>" in text
    assert response_headers["content-security-policy"].startswith("default-src 'none'")
    assert response_headers["x-frame-options"] == "DENY"
    assert response_headers["x-content-type-options"] == "nosniff"
    assert response_headers["referrer-policy"] == "no-referrer"
    assert response_headers["cache-control"] == "no-store"


def test_correct_token_opens_api_and_dashboard_and_webhook_needs_no_token(token_server):
    body = issue_body()
    status, hook = token_server.webhook(body, delivery="tok-1")  # HMAC only, no Authorization header
    assert status == 202 and hook["status"] == "received"
    auth = token_server.auth()
    status, created = call(token_server.base, "POST", "/v1/investigations",
                           json.dumps({"repo": "sample", "report": REPORT}).encode(),
                           {"Content-Type": "application/json", **auth})
    assert status == 201
    assert call(token_server.base, "GET", f"/v1/investigations/{hook['id']}", headers=auth)[0] == 200
    status, _, text = fetch(token_server.base, "GET", "/dashboard", headers=auth)
    assert status == 200 and created["id"] in text and hook["id"] in text
    status, _, text = fetch(token_server.base, "GET", f"/dashboard/investigations/{created['id']}", headers=auth)
    assert status == 200 and "parse_url" in text


def test_without_a_token_the_server_refuses_to_bind_all_interfaces(workdir):
    port = free_port()
    env = {**os.environ, "CLAIMCHECK_WEBHOOK_SECRET": SECRET}
    env.pop("CLAIMCHECK_API_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-m", "claimcheck.server", "--db", str(workdir / "open.db"), "--repo-root",
         str(workdir / "repos"), "--host", "0.0.0.0", "--port", str(port)],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "CLAIMCHECK_API_TOKEN" in result.stderr and "0.0.0.0" in result.stderr
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_without_a_token_a_loopback_server_stays_open(server):
    assert call(server.base, "GET", "/v1/investigations/nothing")[0] == 404
    status, _, _ = fetch(server.base, "GET", "/dashboard")
    assert status == 200
