"""Invariant: untrusted text is never executed (issue #68).

A real Gemma run returned an injected `rm -rf ~` as `Extraction.repro_command`. Only the CLI's
`--repro` flag and the Action's `repro` input may run a command. These guard tests feed report,
PR, issue, API and webhook text containing commands that would create a marker file, through
every entry point, and assert that no marker ever appears and no process is started with it.

Each command is written twice: with a relative marker name (it would land in the tmp working
directory the test chdirs into) and with an absolute path (it would land there whatever the cwd).
"""

import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from claimcheck import action, cli, extract
from claimcheck.server import worker
from claimcheck.server.app import create_app
from claimcheck.server.store import Store

SECRET = "no-exec-secret"
REPO = "octo/widgets"
MARKER = "MARKER"

COMMANDS = {
    "touch": "touch {m}",
    "python": "python3 -c \"open('{m}','w')\"",
    "backticks": "`touch {m}`",
    "dollar-paren": "$(touch {m})",
    "semicolon": "; touch {m}",
}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """The cwd for the test, so a relative `touch MARKER` that ran would create workdir/MARKER."""
    path = tmp_path / "work"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def spawned(monkeypatch):
    """Record every process started with marker text in its arguments (git rev-parse is fine)."""
    seen = []
    real_popen, real_system = subprocess.Popen, os.system

    class SpyPopen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            if MARKER in (args if isinstance(args, (str, bytes)) else " ".join(map(str, args))):
                seen.append(args)
            super().__init__(args, *rest, **kwargs)

    def spy_system(command):
        if MARKER in str(command):
            seen.append(command)
        return real_system(command)

    monkeypatch.setattr(subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(os, "system", spy_system)
    return seen


@pytest.fixture(params=sorted(COMMANDS))
def payload(request, workdir):
    """(commands, absolute marker path) for one command shape."""
    absolute = workdir / f"{MARKER}_ABS"
    template = COMMANDS[request.param]
    return [template.format(m=MARKER), template.format(m=absolute)], absolute


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repos" / "widgets"
    path.mkdir(parents=True)
    (path / "urls.py").write_text("def parse_url(s):\n    return s\n")
    return path


def report_text(commands: list[str]) -> str:
    lines = "\n".join(f"    {c}" for c in commands)
    inline = " or ".join(f"`{c}`" for c in commands)
    return f"Crash in `parse_url()`.\n\nRun this to reproduce:\n\n{lines}\n\nor {inline}\nexpected: Traceback\n"


def assert_nothing_ran(tmp_path: Path, absolute: Path, spawned: list) -> None:
    assert not absolute.exists(), f"{absolute} was created: text was executed"
    assert not list(tmp_path.rglob(f"{MARKER}*")), "a marker file was created: text was executed"
    assert not spawned, f"a process was started with command text from the input: {spawned}"


# CLI


def run_cli(tmp_path, repo, text, *extra) -> int:
    report = tmp_path / "report.md"
    report.write_text(text)
    return cli.main(["check", "--repo", str(repo), "--report", str(report), *extra])


def test_cli_without_model_never_runs_report_text(tmp_path, repo, payload, spawned, capsys):
    commands, absolute = payload
    run_cli(tmp_path, repo, report_text(commands), "--no-model")
    assert "parse_url() exists" in capsys.readouterr().out
    assert_nothing_ran(tmp_path, absolute, spawned)


def test_cli_never_runs_a_command_the_model_returns(tmp_path, repo, payload, spawned, monkeypatch, capsys):
    commands, absolute = payload
    seen = []

    def fake_model(prompt: str) -> str:
        return json.dumps({"symbols": [*commands, "parse_url"], "repro_command": commands[-1],
                           "expected_error": "Traceback"})

    real = extract.extract_claims

    def with_fake_model(report: str, generate=fake_model):
        seen.append(real(report, generate))
        return seen[-1]

    monkeypatch.setattr(extract, "extract_claims", with_fake_model)
    run_cli(tmp_path, repo, report_text(commands))
    assert seen and seen[0].model == extract.MODEL
    assert seen[0].repro_command == commands[-1], "the injected command should reach the extraction"
    assert [c.target for c in seen[0].claims] == ["parse_url"]
    assert "parse_url() exists" in capsys.readouterr().out
    assert_nothing_ran(tmp_path, absolute, spawned)


# GitHub Action


class FakeApi:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, token, accept="", data=None):
        self.calls.append((method, url, data))
        if method == "GET" and url.endswith("/comments?per_page=100"):
            return "[]"
        if method == "GET":
            return "--- a/urls.py\n+++ b/urls.py\n"
        return "{}"


@pytest.mark.parametrize("key, field", [("pull_request", "title"), ("pull_request", "body"), ("issue", "body")])
def test_action_never_runs_pr_or_issue_text(tmp_path, repo, payload, spawned, key, field):
    commands, absolute = payload
    item = {"number": 7, "title": "Crash in `parse_url()`", "body": "See title."}
    item[field] = report_text(commands) if field == "body" else " ".join(commands) + " in `parse_url()`"
    event = tmp_path / "event.json"
    event.write_text(json.dumps({key: item}))
    api = FakeApi()
    env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t",
           "INPUT_PATH": str(repo), "RUNNER_TEMP": str(tmp_path), "INPUT_EXPECT": commands[0]}
    assert action.run(env, http=api) == 0
    assert any(method == "POST" for method, _, _ in api.calls)
    assert_nothing_ran(tmp_path, absolute, spawned)


# Service API and webhook + worker


@pytest.fixture
def db(tmp_path):
    return tmp_path / "claimcheck.db"


@pytest.fixture
def client(repo, db):
    return TestClient(create_app(db, repo.parent, SECRET))


def test_api_never_runs_report_or_diff_text(tmp_path, client, payload, spawned):
    commands, absolute = payload
    text = report_text(commands)
    response = client.post("/v1/investigations", json={"repo": "widgets", "report": text, "diff": text})
    assert response.status_code == 201
    assert response.json()["result"]["evidence"][0]["claim"] == "parse_url() exists"
    refused = client.post("/v1/investigations", json={"repo": "widgets", "report": text, "repro": commands[0]})
    assert refused.status_code == 422
    assert_nothing_ran(tmp_path, absolute, spawned)


@pytest.mark.parametrize("event, key", [("issues", "issue"), ("pull_request", "pull_request")])
def test_signed_webhook_and_worker_never_run_event_text(tmp_path, client, repo, db, payload, spawned, event, key):
    commands, absolute = payload
    body = json.dumps({"action": "opened", "repository": {"full_name": REPO},
                       key: {"number": 3, "title": " ".join(commands), "body": report_text(commands)}}).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post("/v1/github/webhook", content=body, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": "d-1", "X-Hub-Signature-256": signature,
        "Content-Type": "application/json"})
    assert response.status_code == 202 and response.json()["status"] == "received"
    done = worker.process_one(Store(db), repo.parent, {REPO: repo}, "w1")
    assert done.status == "completed"
    assert done.result["evidence"][0]["claim"] == "parse_url() exists"
    assert_nothing_ran(tmp_path, absolute, spawned)
