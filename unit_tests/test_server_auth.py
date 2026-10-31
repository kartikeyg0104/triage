"""Bearer-token authentication for the investigations API and the dashboard (CLAIMCHECK_API_TOKEN).

The webhook keeps its own HMAC signature and never needs the token. Without a token the server
stays open, but `python -m claimcheck.server` then only binds a loopback host. The real-HTTP
tests are in test_server_http.py."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from claimcheck.server import __main__ as serve
from claimcheck.server import auth, dashboard
from claimcheck.server.app import create_app
from claimcheck.server.store import Store

SECRET = "hook-secret"
TOKEN = "t0ken-for-tests"
API_PATHS = ["/v1/investigations/some-id"]
DASHBOARD_PATHS = ["/", "/dashboard", "/dashboard?status=received", "/dashboard/investigations/some-id",
                   "/dashboard/unknown"]


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def bearer(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


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
    return TestClient(create_app(db, root, SECRET, api_token=TOKEN))


def post_investigation(client, headers=None):
    return client.post("/v1/investigations", content=json.dumps({"repo": "sample", "report": "parse_url() crashes"}),
                       headers={"Content-Type": "application/json", **(headers or {})})


def webhook(client, body: bytes, headers=None):
    return client.post("/v1/github/webhook", content=body, headers={
        "Content-Type": "application/json", "X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-1",
        "X-Hub-Signature-256": sign(body), **(headers or {})})


def issue_body() -> bytes:
    return json.dumps({"action": "opened", "issue": {"number": 1, "title": "t", "body": "parse_url() crashes"},
                       "repository": {"full_name": "octo/widgets"}}).encode()


BAD_HEADERS = [
    {},
    {"Authorization": "Bearer wrong"},
    {"Authorization": "Bearer "},
    {"Authorization": "Bearer"},
    {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": TOKEN},
    {"Authorization": f"Bearer {TOKEN}x"},
    {"Authorization": f"Bearer {TOKEN[:-1]}"},
]


@pytest.mark.parametrize("headers", BAD_HEADERS)
@pytest.mark.parametrize("path", API_PATHS)
def test_api_get_without_the_right_token_is_401_json(client, path, headers):
    response = client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()


@pytest.mark.parametrize("headers", BAD_HEADERS)
def test_api_post_without_the_right_token_is_401_and_runs_nothing(client, db, headers):
    response = post_investigation(client, headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert Store(db).list(None, limit=10, offset=0) == []  # nothing was checked or stored


def test_api_with_the_right_token_works(client):
    created = post_investigation(client, bearer())
    assert created.status_code == 201
    fetched = client.get(f"/v1/investigations/{created.json()['id']}", headers=bearer())
    assert fetched.status_code == 200 and fetched.json() == created.json()
    assert client.get("/v1/investigations/nope", headers=bearer()).status_code == 404


def test_bearer_scheme_is_case_insensitive(client):
    assert client.get("/v1/investigations/nope", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 404


@pytest.mark.parametrize("headers", BAD_HEADERS)
@pytest.mark.parametrize("path", DASHBOARD_PATHS)
def test_dashboard_without_the_right_token_is_a_401_html_page_with_security_headers(client, path, headers):
    response = client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"].startswith("text/html")
    for name, value in dashboard.HEADERS.items():
        assert response.headers[name] == value
    assert "<h1>" in response.text and "token" in response.text.lower()


def test_dashboard_with_the_right_token_works(client):
    assert client.get("/", headers=bearer()).status_code == 200
    assert client.get("/dashboard", headers=bearer()).status_code == 200
    assert client.get("/dashboard/investigations/nope", headers=bearer()).status_code == 404


def test_webhook_needs_only_its_signature_not_the_token(client):
    response = webhook(client, issue_body())
    assert response.status_code == 202 and response.json()["status"] == "received"
    # A wrong bearer token does not matter to the webhook either; its HMAC still decides.
    assert webhook(client, issue_body(), {"Authorization": "Bearer wrong", "X-GitHub-Delivery": "d-2"}).status_code == 202
    assert webhook(client, issue_body(), {"X-Hub-Signature-256": sign(b"x"), **bearer()}).status_code == 401


def test_token_is_compared_with_compare_digest(client, monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert client.get("/dashboard", headers=bearer("wrong")).status_code == 401
    assert client.get("/dashboard", headers=bearer()).status_code == 200
    assert len(calls) == 2
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


def test_without_a_token_the_app_stays_open(root, db):
    client = TestClient(create_app(db, root, SECRET))
    assert client.get("/dashboard").status_code == 200
    assert post_investigation(client).status_code == 201


# Entry point


def fake_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(serve.uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    return seen


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST", "127.0.0.2"])
def test_without_a_token_a_loopback_host_starts_open(root, db, monkeypatch, host):
    monkeypatch.delenv(serve.TOKEN_ENV, raising=False)
    seen = fake_run(monkeypatch)
    assert serve.main(["--db", str(db), "--repo-root", str(root), "--host", host]) == 0
    assert seen["host"] == host
    assert TestClient(seen["app"]).get("/dashboard").status_code == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com", "localhost.example.com"])
@pytest.mark.parametrize("token", [None, ""])
def test_without_a_token_a_non_loopback_host_is_refused(root, db, monkeypatch, capsys, host, token):
    if token is None:
        monkeypatch.delenv(serve.TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(serve.TOKEN_ENV, token)
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: pytest.fail("should not start"))
    assert serve.main(["--db", str(db), "--repo-root", str(root), "--host", host]) == 2
    err = capsys.readouterr().err
    assert serve.TOKEN_ENV in err and host in err and "loopback" in err


def test_with_a_token_any_host_starts_and_the_token_is_required(root, db, monkeypatch):
    monkeypatch.setenv(serve.TOKEN_ENV, TOKEN)
    seen = fake_run(monkeypatch)
    assert serve.main(["--db", str(db), "--repo-root", str(root), "--host", "0.0.0.0"]) == 0
    assert seen["host"] == "0.0.0.0"
    client = TestClient(seen["app"])
    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard", headers=bearer()).status_code == 200
