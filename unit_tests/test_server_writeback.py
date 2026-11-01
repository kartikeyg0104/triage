"""Worker write-back: after a completed investigation the worker posts (or updates) one comment,
found by a hidden marker, with the markdown evidence on the source issue or PR.

Unit tests drive `process_one` with an injected fake HTTP function. The end-to-end test runs the
real server and worker processes against a fake GitHub API (stdlib http.server on 127.0.0.1)."""

import hashlib
import hmac
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from claimcheck import report
from claimcheck.models import Claim, Evidence, Status, Verdict
from claimcheck.server import github_client, worker
from claimcheck.server.store import Store

TOKEN = "ghp_SECRETtoken0123456789abcdefABCDEF"
REPORT = "Crash in `parse_url()` and RCE in parse_url_v2()"
REPO = "octo/widgets"
MARKER = "<!-- claimcheck-service -->"


@pytest.fixture
def root(tmp_path):
    repo = tmp_path / "repos" / "widgets"
    repo.mkdir(parents=True)
    (repo / "urls.py").write_text("def parse_url(s):\n    return s\n")
    return tmp_path / "repos"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "claimcheck.db")


@pytest.fixture
def repos(root):
    return {REPO.casefold(): root / "widgets"}


def receive(store: Store, repo: str = REPO, text: str = REPORT, number: int = 7, event: str = "issues"):
    request = {"event": event, "action": "opened", "repo": repo, "number": number, "text": text, "claims": []}
    return store.create(f"github:{event}:{repo}#{number}", "received", request)[0]


class FakeHTTP:
    """Stands in for GitHub's REST API in process: issue comments keyed by (repo, number)."""

    def __init__(self, existing: list[dict] | None = None, fail: Exception | None = None):
        self.calls: list[tuple] = []
        self.comments = list(existing or [])
        self.fail = fail

    def __call__(self, method: str, url: str, token: str, data=None) -> str:
        self.calls.append((method, url, token, data))
        if self.fail is not None:
            raise self.fail
        if method == "GET":
            return json.dumps(self.comments)
        if method == "POST":
            comment = {"id": 100 + len(self.comments), "body": data["body"]}
            self.comments.append(comment)
            return json.dumps(comment)
        if method == "PATCH":
            comment_id = int(url.rsplit("/", 1)[1])
            for comment in self.comments:
                if comment["id"] == comment_id:
                    comment["body"] = data["body"]
                    return json.dumps(comment)
        raise AssertionError(f"unexpected {method} {url}")


def writer(http, api: str = "https://api.github.com") -> github_client.CommentWriter:
    return github_client.CommentWriter(TOKEN, api, http=http)


# Source parsing and configuration


@pytest.mark.parametrize("source, expected", [
    ("github:issues:octo/widgets#7", ("octo/widgets", 7)),
    ("github:pull_request:Octo-Org/my.repo_1#123", ("Octo-Org/my.repo_1", 123)),
])
def test_parse_source_reads_repo_and_number(source, expected):
    assert github_client.parse_source(source) == expected


@pytest.mark.parametrize("source", [
    "api", "github:issues:octo/widgets", "github:issues:octo/widgets#0", "github:issues:octo/widgets#-1",
    "github:issues:../../evil#1", "github:issues:octo/widgets/../x#1", "github:issues:octo#1",
    "github:push:octo/widgets#1", "github:issues:octo/wid gets#1", "github:issues:octo/widgets#1?x=y",
    "github:issues:octo/widgets#1\n",
])
def test_parse_source_refuses_anything_else(source):
    assert github_client.parse_source(source) is None


def test_api_base_defaults_to_github_and_strips_a_trailing_slash():
    assert github_client.api_base({}) == "https://api.github.com"
    assert github_client.api_base({"CLAIMCHECK_GITHUB_API": "http://127.0.0.1:9999/"}) == "http://127.0.0.1:9999"


@pytest.mark.parametrize("value", ["ftp://example.com", "file:///etc/passwd", "javascript:alert(1)", "api.github.com",
                                   "https://user:pw@example.com", "http://", "http://example.com:bad",
                                   "http://example.com:99999", "http://[", "http://[pw]",
                                   "https://example.com/?query=1", "https://example.com/#fragment",
                                   "https://exam\nple.com"])
def test_api_base_must_be_plain_http_or_https(value):
    with pytest.raises(ValueError) as raised:
        github_client.api_base({"CLAIMCHECK_GITHUB_API": value})
    assert "pw" not in str(raised.value)


def test_from_env_is_disabled_without_a_token():
    assert github_client.from_env({}) is None
    assert github_client.from_env({"CLAIMCHECK_GITHUB_TOKEN": ""}) is None
    assert isinstance(github_client.from_env({"CLAIMCHECK_GITHUB_TOKEN": TOKEN}), github_client.CommentWriter)


# Create vs update


def test_creates_one_comment_with_the_marker_when_there_is_none():
    http = FakeHTTP(existing=[{"id": 1, "body": "unrelated"}, {"id": 2, "body": "<!-- claimcheck -->\naction's"}])
    assert writer(http).post("github:issues:octo/widgets#7", "# report\n") == "created"
    assert [(m, u) for m, u, _, _ in http.calls] == [
        ("GET", "https://api.github.com/repos/octo/widgets/issues/7/comments?per_page=100&page=1"),
        ("POST", "https://api.github.com/repos/octo/widgets/issues/7/comments")]
    assert http.calls[-1][3] == {"body": MARKER + "\n# report\n"}
    assert all(token == TOKEN for _, _, token, _ in http.calls)


def test_updates_the_comment_that_has_the_marker():
    http = FakeHTTP(existing=[{"id": 1, "body": "unrelated"}, {"id": 5, "body": MARKER + "\nold"}])
    assert writer(http).post("github:pull_request:octo/widgets#7", "new\n") == "updated"
    assert [(m, u) for m, u, _, _ in http.calls][-1] == ("PATCH", "https://api.github.com/repos/octo/widgets/issues/comments/5")
    assert http.comments[1]["body"] == MARKER + "\nnew\n" and len(http.comments) == 2


def test_looks_past_the_first_page_of_comments():
    class Paged(FakeHTTP):
        def __call__(self, method, url, token, data=None):
            if method == "GET":
                self.calls.append((method, url, token, data))
                page = int(url.rsplit("page=", 1)[1])
                return json.dumps([{"id": n, "body": "x"} for n in range(100)] if page == 1 else
                                  [{"id": 999, "body": MARKER + "\nold"}])
            return super().__call__(method, url, token, data)
    http = Paged(existing=[{"id": 999, "body": MARKER}])
    assert writer(http).post("github:issues:octo/widgets#7", "new\n") == "updated"
    assert [u.rsplit("page=", 1)[1] for m, u, _, _ in http.calls if m == "GET"] == ["1", "2"]


def test_refuses_a_source_it_cannot_parse_without_calling_github():
    http = FakeHTTP()
    with pytest.raises(github_client.GitHubError):
        writer(http).post("github:issues:../x#1", "body")
    assert http.calls == []


def test_an_exhausted_comment_search_never_creates_a_duplicate(monkeypatch):
    monkeypatch.setattr(github_client, "MAX_PAGES", 2)
    http = FakeHTTP(existing=[{"id": n + 1, "body": "unrelated"} for n in range(100)])
    with pytest.raises(github_client.GitHubError, match="pagination"):
        writer(http).post("github:issues:octo/widgets#7", "body")
    assert [method for method, _, _, _ in http.calls] == ["GET", "GET"]


@pytest.mark.parametrize("response", [{}, None, {"message": "bad response"},
                                     [{"id": 0, "body": MARKER}], [{"id": -1, "body": MARKER}],
                                     [{"id": True, "body": MARKER}], [{"id": "5", "body": MARKER}]])
def test_invalid_comment_responses_never_create_or_update(response):
    calls = []

    def http(method, url, token, data=None):
        calls.append(method)
        return json.dumps(response)

    with pytest.raises(github_client.GitHubError):
        writer(http).post("github:issues:octo/widgets#7", "body")
    assert calls == ["GET"]


# Through the worker


def test_completed_investigation_posts_the_markdown_evidence(store, root, repos):
    row = receive(store)
    http = FakeHTTP()
    done = worker.process_one(store, root, repos, "w1", writeback=writer(http))
    assert done.status == "completed" and done.error is None
    assert (done.writeback, done.writeback_error) == ("created", None)
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == 1 and posts[0][1].endswith("/repos/octo/widgets/issues/7/comments")
    body = posts[0][3]["body"]
    assert body.startswith(MARKER + "\n")
    assert "**Verdict: refuted**" in body and "parse_url_v2" in body
    assert body == MARKER + "\n" + github_client.result_markdown(store.get(row.id).result)


def test_result_markdown_is_report_to_markdown():
    verdict = Verdict([Evidence(Claim("symbol", "`parse_url`", "parse_url"), Status.CONFIRMED, "defined", "urls.py:1"),
                       Evidence(Claim("symbol", "`gone`", "gone"), Status.REFUTED, "not found", "", "inferred")], "abc123")
    result = json.loads(report.to_json(verdict, None))
    assert github_client.result_markdown(result) == report.to_markdown(verdict, None)


def test_a_second_investigation_of_the_same_issue_updates_the_comment(store, root, repos):
    http = FakeHTTP()
    receive(store)
    receive(store, text="Now `parse_url()` only")
    first = worker.process_one(store, root, repos, "w1", writeback=writer(http))
    second = worker.process_one(store, root, repos, "w1", writeback=writer(http))
    assert (first.writeback, second.writeback) == ("created", "updated")
    assert len(http.comments) == 1 and "**Verdict: supported**" in http.comments[0]["body"]


def test_skipped_and_failed_investigations_post_nothing(store, root, repos):
    http = FakeHTTP()
    receive(store, repo="someone/else")
    receive(store, number=8)
    skipped = worker.process_one(store, root, repos, "w1", writeback=writer(http))
    failed = worker.process_one(store, root, repos, "w1", writeback=writer(http),
                                check=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert (skipped.status, failed.status) == ("skipped", "failed")
    assert (skipped.writeback, failed.writeback, skipped.writeback_error, failed.writeback_error) == (None,) * 4
    assert http.calls == []


def test_without_a_writer_nothing_is_posted(store, root, repos):
    receive(store)
    done = worker.process_one(store, root, repos, "w1")
    assert done.status == "completed" and (done.writeback, done.writeback_error) == (None, None)


def test_concurrent_workers_for_the_same_target_create_one_comment(store, root, repos):
    receive(store)
    receive(store, repo="OCTO/Widgets", event="pull_request", text="Only `parse_url()` now")
    entered, release = threading.Event(), threading.Event()

    class SlowHTTP(FakeHTTP):
        def __call__(self, method, url, token, data=None):
            if method == "GET" and not entered.is_set():
                response = super().__call__(method, url, token, data)
                entered.set()
                assert release.wait(5)
                return response
            return super().__call__(method, url, token, data)

    http = SlowHTTP()
    results = []

    def first():
        results.append(worker.process_one(Store(store.path), root, repos, "w1", writeback=writer(http)))

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(5)
        blocked = worker.process_one(Store(store.path), root, repos, "w2", writeback=writer(http))
        assert blocked is None
        receive(store, number=8)
        assert worker.process_one(store, root, repos, "w2", writeback=writer(FakeHTTP())).writeback == "created"
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and results[0].writeback == "created"
    assert worker.process_one(store, root, repos, "w2", writeback=writer(http)).writeback == "updated"
    assert len(http.comments) == 1


def test_abandoned_writeback_does_not_block_later_checks_forever(store, root, repos):
    first = receive(store)
    store.claim("dead", stale_after=60, now=time.time() - 120, writeback=True)
    assert store.finish(first.id, "dead", "completed", {"evidence": []})
    receive(store)
    done = worker.process_one(store, root, repos, "w2", stale_after=60, writeback=writer(FakeHTTP()))
    assert done.writeback == "created"
    abandoned = store.get(first.id)
    assert abandoned.status == "completed" and abandoned.writeback == "error"
    assert "abandoned" in abandoned.writeback_error


def test_failed_abandoned_checks_clear_pending_writeback(store):
    row = receive(store)
    assert store.claim("dead", stale_after=60, max_attempts=1, now=1000, writeback=True).writeback == "pending"
    assert store.claim("new", stale_after=60, max_attempts=1, now=2000, writeback=True) is None
    done = store.get(row.id)
    assert done.status == "failed" and (done.writeback, done.writeback_error) == (None, None)


def test_a_worker_that_lost_its_claim_never_posts(store, root, repos):
    row = receive(store)

    def reclaimed(*args, **kwargs):
        assert store.claim("new", stale_after=0, now=time.time() + 1).id == row.id
        return Verdict([]), None

    http = FakeHTTP()
    done = worker.process_one(store, root, repos, "old", check=reclaimed, writeback=writer(http))
    assert done.status == "processing" and done.worker == "new"
    assert http.calls == []


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/x", code, "boom", {}, None)


@pytest.mark.parametrize("failure, expected", [
    (http_error(403), "HTTP 403"), (http_error(404), "HTTP 404"), (http_error(502), "HTTP 502"),
    (urllib.error.URLError(ConnectionRefusedError("refused")), "network"), (TimeoutError("slow"), "network"),
    (ValueError(f"bad json near Bearer {TOKEN}"), "ValueError"),
])
def test_github_errors_are_recorded_without_failing_the_check(store, root, repos, failure, expected):
    row = receive(store)
    done = worker.process_one(store, root, repos, "w1", writeback=writer(FakeHTTP(fail=failure)))
    assert done.status == "completed" and done.result["verdict"] == "refuted" and done.error is None
    assert done.writeback == "error" and expected in done.writeback_error
    assert TOKEN not in done.writeback_error
    assert store.get(row.id).writeback_error == done.writeback_error


def test_an_unparseable_github_response_is_recorded(store, root, repos):
    receive(store)
    done = worker.process_one(store, root, repos, "w1", writeback=writer(lambda *a, **k: "not json"))
    assert done.status == "completed" and done.writeback == "error" and done.writeback_error


def test_the_worker_keeps_going_after_a_writeback_error(store, root, repos):
    for n in range(3):
        receive(store, number=n + 1)
    lines = []
    assert worker.run(store, root, repos, "w1", once=True, log=lines.append,
                      writeback=writer(FakeHTTP(fail=http_error(500)))) == 3
    assert [json.loads(line)["writeback"] for line in lines] == ["error"] * 3


def test_token_never_reaches_logs_errors_or_sqlite(store, root, repos, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    receive(store)
    receive(store, number=8)
    lines = []
    leaky = FakeHTTP(fail=RuntimeError(f"connection reset; Authorization: Bearer {TOKEN}"))
    worker.run(store, root, repos, "w1", once=True, log=lines.append, writeback=writer(leaky))
    receive(store, number=9)
    worker.run(store, root, repos, "w1", once=True, log=lines.append, writeback=writer(FakeHTTP()))
    assert leaky.calls and all(call[2] == TOKEN for call in leaky.calls)  # it was used...
    captured = capsys.readouterr()
    with closing(sqlite3.connect(store.path)) as conn:
        dump = "\n".join(conn.iterdump())
    for text in ("\n".join(lines), caplog.text, captured.out, captured.err, dump):  # ...and never written down
        assert TOKEN not in text and TOKEN[4:20] not in text
    assert "error" in dump and "created" in dump


def test_main_takes_the_token_from_the_environment_and_refuses_a_bad_api_url(tmp_path, root, monkeypatch, capsys):
    repos_file = tmp_path / "repos.json"
    repos_file.write_text(json.dumps({REPO: "widgets"}))
    args = ["--db", str(tmp_path / "w.db"), "--repo-root", str(root), "--repos", str(repos_file), "--once"]
    monkeypatch.setenv("CLAIMCHECK_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("CLAIMCHECK_GITHUB_API", "ftp://example.com")
    assert worker.main(args) == 2
    err = capsys.readouterr().err
    assert "CLAIMCHECK_GITHUB_API" in err and TOKEN not in err


def test_main_posts_with_the_environment_token_without_exposing_it(store, root, tmp_path, monkeypatch, capsys):
    row = receive(store)
    repos_file = tmp_path / "repos.json"
    repos_file.write_text(json.dumps({REPO: "widgets"}))
    api = f"https://example.com/{TOKEN}"
    monkeypatch.setenv("CLAIMCHECK_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("CLAIMCHECK_GITHUB_API", api)
    http, client_class = FakeHTTP(), github_client.CommentWriter
    monkeypatch.setattr(github_client, "CommentWriter", lambda token, api: client_class(token, api, http=http))
    args = ["--db", str(store.path), "--repo-root", str(root), "--repos", str(repos_file), "--once", "--worker-id", "cli"]
    assert worker.main(args) == 0
    assert store.get(row.id).writeback == "created" and all(call[2] == TOKEN for call in http.calls)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert TOKEN not in repr(client_class(TOKEN, api, http=http))


def test_old_database_files_get_a_writeback_table_without_changing_investigation_columns(tmp_path):
    db = tmp_path / "old.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE investigations (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL, "
                     "delivery_id TEXT UNIQUE NULL, status TEXT NOT NULL, request TEXT NOT NULL, result TEXT)")
        conn.execute("INSERT INTO investigations VALUES ('a', '2026-01-01', 'api', NULL, 'completed', '{}', NULL)")
    old = Store(db).get("a")
    assert (old.writeback, old.writeback_error) == (None, None)
    with closing(sqlite3.connect(db)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(investigations)")]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "writeback" not in columns and "writeback_error" not in columns
    assert "writebacks" in tables


# The real HTTP function, against a local fake GitHub


class FakeGitHub:
    """A tiny GitHub REST API on 127.0.0.1: list, create and update issue comments."""

    def __init__(self, token: str = TOKEN):
        self.token, self.comments, self.requests, self.lock = token, {}, [], threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code: int, payload, headers: dict | None = None):
                body = json.dumps(payload).encode()
                self.send_response(code)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length)) if length else None
                with fake.lock:
                    fake.requests.append((self.command, self.path, self.headers.get("Authorization"), data))
                    if self.path.startswith("/redirect"):
                        return self.reply(302, {}, {"Location": f"http://127.0.0.1:{fake.port}/elsewhere"})
                    if self.headers.get("Authorization") != f"Bearer {fake.token}":
                        return self.reply(401, {"message": "Bad credentials"})
                    parts = self.path.split("?")[0].strip("/").split("/")
                    if self.command == "GET" and len(parts) == 6 and parts[3:] == ["issues", parts[4], "comments"]:
                        page = int(self.path.rsplit("page=", 1)[1]) if "page=" in self.path else 1
                        found = fake.comments.get((f"{parts[1]}/{parts[2]}", int(parts[4])), [])
                        return self.reply(200, found[(page - 1) * 100:page * 100])
                    if self.command == "POST" and len(parts) == 6 and parts[5] == "comments":
                        key = (f"{parts[1]}/{parts[2]}", int(parts[4]))
                        comment = {"id": sum(map(len, fake.comments.values())) + 1, "body": data["body"]}
                        fake.comments.setdefault(key, []).append(comment)
                        return self.reply(201, comment)
                    if self.command == "PATCH" and parts[3:5] == ["issues", "comments"]:
                        for comment in (c for found in fake.comments.values() for c in found):
                            if comment["id"] == int(parts[5]):
                                comment["body"] = data["body"]
                                return self.reply(200, comment)
                    return self.reply(404, {"message": "Not Found"})

            do_GET = do_POST = do_PATCH = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_real_http_function_sends_the_bearer_token_and_maps_errors():
    with FakeGitHub() as fake:
        client = github_client.CommentWriter(TOKEN, fake.base)
        assert client.post("github:issues:octo/widgets#3", "one") == "created"
        assert client.post("github:issues:octo/widgets#3", "two") == "updated"
        assert fake.comments[("octo/widgets", 3)] == [{"id": 1, "body": MARKER + "\ntwo"}]
        assert {auth for _, _, auth, _ in fake.requests} == {f"Bearer {TOKEN}"}
        with pytest.raises(github_client.GitHubError) as raised:
            github_client.CommentWriter("wrong-token-value", fake.base).post("github:issues:octo/widgets#3", "x")
        assert "HTTP 401" in str(raised.value) and "wrong-token-value" not in str(raised.value)


def test_real_http_function_does_not_follow_redirects_with_the_token():
    with FakeGitHub() as fake:
        with pytest.raises(github_client.GitHubError) as raised:
            github_client.CommentWriter(TOKEN, fake.base + "/redirect").post("github:issues:octo/widgets#3", "x")
        assert "HTTP 302" in str(raised.value)
        assert [path for _, path, _, _ in fake.requests if path.startswith("/elsewhere")] == []


def test_network_errors_are_github_errors():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # nothing listens here once closed
    with pytest.raises(github_client.GitHubError) as raised:
        github_client.CommentWriter(TOKEN, f"http://127.0.0.1:{port}").post("github:issues:octo/widgets#3", "x")
    assert "network" in str(raised.value) and TOKEN not in str(raised.value)


# End to end: real server process, signed webhook, real worker process, fake GitHub


SECRET = "writeback-secret"


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def deliver(base: str, body: bytes, delivery: str, event: str = "issues") -> int:
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(base + "/v1/github/webhook", body, {
        "Content-Type": "application/json", "X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def issue_event(body: str, event: str = "issues", repo: str = REPO) -> bytes:
    key = "issue" if event == "issues" else "pull_request"
    return json.dumps({"action": "edited", key: {"number": 12, "title": "Crash", "body": body},
                       "repository": {"full_name": repo}}).encode()


def wait_for(predicate, what: str, timeout: float = 60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


def statuses(db: Path) -> list[str]:
    with closing(sqlite3.connect(db)) as conn:
        return [row[0] for row in conn.execute("SELECT status FROM investigations ORDER BY rowid")]


@pytest.mark.parametrize("event", ["issues", "pull_request"])
def test_end_to_end_one_comment_is_created_then_updated(tmp_path, root, event):
    db = tmp_path / "claimcheck.db"
    repos_file = tmp_path / "repos.json"
    repos_file.write_text(json.dumps({REPO: "widgets"}))
    with FakeGitHub() as fake:
        env = {**os.environ, "CLAIMCHECK_WEBHOOK_SECRET": SECRET, "CLAIMCHECK_API_TOKEN": "",
               "CLAIMCHECK_GITHUB_TOKEN": TOKEN, "CLAIMCHECK_GITHUB_API": fake.base}
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        server_log = open(tmp_path / "server.log", "w+")
        server = subprocess.Popen([sys.executable, "-m", "claimcheck.server", "--db", str(db), "--repo-root", str(root),
                                   "--port", str(port)], cwd=tmp_path, env=env, stdout=server_log,
                                  stderr=subprocess.STDOUT)
        processes = [server]
        try:
            def up():
                try:
                    urllib.request.urlopen(base + "/v1/investigations/probe", timeout=2)
                except urllib.error.HTTPError as error:
                    return error.code == 404
                except OSError:
                    return False
            wait_for(up, "the server")
            work = subprocess.Popen([sys.executable, "-m", "claimcheck.server.worker", "--db", str(db), "--repo-root",
                                     str(root), "--repos", str(repos_file), "--poll", "0.05", "--worker-id", "e2e"],
                                    cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(work)

            assert deliver(base, issue_event(REPORT, event), "d-1", event) == 202
            wait_for(lambda: statuses(db) == ["completed"], "the first investigation")
            wait_for(lambda: fake.comments.get((REPO, 12)), "the first comment")
            first = [dict(c) for c in fake.comments[(REPO, 12)]]
            assert len(first) == 1 and first[0]["body"].startswith(MARKER + "\n")
            assert "**Verdict: refuted**" in first[0]["body"]

            assert deliver(base, issue_event(REPORT, event), "d-1", event) == 202
            assert statuses(db) == ["completed"]
            assert deliver(base, issue_event("Only `parse_url()` now", event), "d-2", event) == 202
            wait_for(lambda: statuses(db) == ["completed", "completed"], "the second investigation")
            wait_for(lambda: "supported" in fake.comments[(REPO, 12)][0]["body"], "the update")
            assert len(fake.comments[(REPO, 12)]) == 1 and fake.comments[(REPO, 12)][0]["id"] == first[0]["id"]
            methods = [method for method, _, _, _ in fake.requests]
            assert methods.count("POST") == 1 and methods.count("PATCH") == 1

            assert deliver(base, issue_event(REPORT, event, "other/repo"), "d-3", event) == 202
            wait_for(lambda: statuses(db) == ["completed", "completed", "skipped"], "the skipped investigation")
            assert [method for method, _, _, _ in fake.requests] == methods

            fake.token = "reject-this-request"
            assert deliver(base, issue_event(REPORT, event), "d-4", event) == 202
            wait_for(lambda: Store(db).list()[0].writeback == "error", "the recorded GitHub API error")
            failed_comment = Store(db).list()[0]
            assert failed_comment.status == "completed" and "HTTP 401" in failed_comment.writeback_error
            fake.token = TOKEN
            assert deliver(base, issue_event(REPORT, event), "d-5", event) == 202
            wait_for(lambda: Store(db).list()[0].writeback == "updated", "the worker's next successful comment")
            assert len(fake.comments[(REPO, 12)]) == 1 and fake.comments[(REPO, 12)][0]["id"] == first[0]["id"]
            methods = [method for method, _, _, _ in fake.requests]
            assert methods.count("POST") == 1 and methods.count("PATCH") == 2
        finally:
            for process in processes:
                process.terminate()
            outputs = [process.communicate(timeout=10) for process in processes]
            server_log.seek(0)
            server_output = server_log.read()
            server_log.close()
        worker_out, worker_err = outputs[1]
        assert [json.loads(line).get("writeback") for line in worker_out.splitlines()] == ["created", "updated", None,
                                                                                       "error", "updated"]
        with closing(sqlite3.connect(db)) as conn:
            dump = "\n".join(conn.iterdump())
        for text in (worker_out, worker_err, server_output, dump):
            assert TOKEN not in text
