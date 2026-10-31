"""The Action's verify-fix input (issue #92): run the workflow's repro on the PR base and head commits."""

import json
import subprocess
from pathlib import Path

import pytest

from claimcheck import action, revisions, sandbox
from claimcheck.models import Claim, Evidence, Status

from test_action import FakeApi, check_calls, read_outputs

BUGGY = "def parse_url(s):\n    if len(s) > 64:\n        raise RuntimeError('buffer overflow')\n    return s\n"
FIXED = "def parse_url(s):\n    return s[:64]\n"
POC = "from urls import parse_url\nparse_url('x' * 100)\nprint('parsed fine')\n"
REPRO, EXPECT = "python3 poc.py", "buffer overflow"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=example", "-c", "user.email=",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> tuple[Path, str, str]:
    """A checkout with a buggy commit (base) and a fixing commit (head), head checked out."""
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q")
    (path / "urls.py").write_text(BUGGY)
    (path / "poc.py").write_text(POC)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "parser")
    base = git(path, "rev-parse", "HEAD")
    (path / "urls.py").write_text(FIXED)
    git(path, "commit", "-qam", "Fix #120: truncate long URLs")
    return path, base, git(path, "rev-parse", "HEAD")


def pr_event(tmp_path, base, head, body="Fixes the overflow in parse_url()"):
    pr = {"number": 7, "title": "Fix #120", "body": body}
    if base is not None:
        pr["base"] = {"sha": base}
    if head is not None:
        pr["head"] = {"sha": head}
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"pull_request": pr}))
    return path


def run_action(tmp_path, root, base, head, body="Fixes the overflow in parse_url()", run_repro=None, **extra):
    api = FakeApi()
    environment = {"GITHUB_EVENT_PATH": str(pr_event(tmp_path, base, head, body)), "GITHUB_REPOSITORY": "o/r",
                   "GITHUB_TOKEN": "t", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
                   "GITHUB_OUTPUT": str(tmp_path / "out.txt"), "RUNNER_TEMP": str(tmp_path),
                   "INPUT_PATH": str(root), "INPUT_REPRO": REPRO, "INPUT_EXPECT": EXPECT, **extra}
    kwargs = {"run_repro": run_repro} if run_repro else {}
    code = action.run(environment, api, **kwargs)
    comment = next(c[2]["body"] for c in api.calls if c[0] == "POST" and c[1].endswith("/comments"))
    report = json.loads((tmp_path / action.REPORT_NAME).read_text())
    return code, api, comment, report


def fix_rows(report):
    return [e for e in report["evidence"] if e["kind"] == "fix"]


def repro_rows(report):
    return [e for e in report["evidence"] if e["kind"] == "repro"]


class FakeVerify:
    def __init__(self, status=Status.CONFIRMED):
        self.calls, self.status = [], status

    def __call__(self, repo, base, head, command, expect, run_repro, timeout=30):
        self.calls.append({"repo": repo, "base": base, "head": head, "command": command, "expect": expect,
                           "run_repro": run_repro})
        return Evidence(Claim("fix", f"`{head}` fixes `{command}`", command), self.status, "fake verify detail",
                        f"base {base[:12]} → head {head[:12]}; fake")


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def __call__(self, repo, command, expect, timeout=30):
        self.calls.append((Path(repo), command, expect))
        return Evidence(Claim("repro", f"`{command}` reproduces the reported failure", command), Status.CONFIRMED,
                        "fake executor", "fake")


@pytest.fixture
def fake_verify(monkeypatch):
    fake = FakeVerify()
    monkeypatch.setattr(revisions, "verify_fix", fake)
    return fake


@pytest.mark.parametrize("value", [None, "false", "TRUE"])
def test_off_by_default_runs_the_single_repro(tmp_path, repo, fake_verify, value):
    root, base, head = repo
    executor = FakeExecutor()
    extra = {"INPUT_VERIFY_FIX": value} if value is not None else {}
    _, _, _, report = run_action(tmp_path, root, base, head, run_repro=executor, **extra)
    assert fake_verify.calls == []
    assert [c[1:] for c in executor.calls] == [(REPRO, EXPECT)]
    assert fix_rows(report) == [] and len(repro_rows(report)) == 1


def test_action_yml_declares_verify_fix_off_by_default():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((Path(__file__).resolve().parents[1] / "action.yml").read_text())
    assert spec["inputs"]["verify-fix"]["default"] == "false"
    assert "fetch-depth" in spec["inputs"]["verify-fix"]["description"]
    step = next(s for s in spec["runs"]["steps"] if s.get("id") == "check")
    assert step["env"]["INPUT_VERIFY_FIX"] == "${{ inputs.verify-fix }}"


def test_passes_event_shas_repro_and_executor_to_verify_fix(tmp_path, repo, fake_verify):
    root, base, head = repo
    executor = FakeExecutor()
    run_action(tmp_path, root, base, head, run_repro=executor, INPUT_VERIFY_FIX="true")
    assert len(fake_verify.calls) == 1
    call = fake_verify.calls[0]
    assert (Path(call["repo"]), call["base"], call["head"]) == (root, base, head)
    assert (call["command"], call["expect"], call["run_repro"]) == (REPRO, EXPECT, executor)
    assert executor.calls == []  # only verify_fix runs anything


def test_default_executor_is_the_local_sandbox(tmp_path, repo, fake_verify):
    root, base, head = repo
    run_action(tmp_path, root, base, head, INPUT_VERIFY_FIX="true")
    assert [call["run_repro"] for call in fake_verify.calls] == [sandbox.run_repro]


def test_fix_row_replaces_the_repro_row_in_comment_check_run_and_outputs(tmp_path, repo, fake_verify):
    root, base, head = repo
    code, api, comment, report = run_action(tmp_path, root, base, head, run_repro=FakeExecutor(),
                                            INPUT_VERIFY_FIX="true")
    assert code == 0
    assert repro_rows(report) == []
    assert [r["status"] for r in fix_rows(report)] == ["confirmed"]
    assert "fake verify detail" in comment
    summary = check_calls(api)[0][2]["output"]["summary"]
    assert "fake verify detail" in summary
    assert check_calls(api)[0][2]["head_sha"] == head
    assert (tmp_path / "summary.md").read_text() == summary
    outputs = read_outputs(tmp_path / "out.txt")
    assert outputs["verdict"] == report["verdict"]
    assert Path(outputs["report-json"]) == tmp_path / action.REPORT_NAME
    for status in Status:
        assert int(outputs[status.value]) == sum(e["status"] == status.value for e in report["evidence"])
    assert int(outputs["confirmed"]) >= 1


def test_refuted_fix_fails_when_asked(tmp_path, repo, monkeypatch):
    root, base, head = repo
    monkeypatch.setattr(revisions, "verify_fix", FakeVerify(Status.REFUTED))
    code, api, _, report = run_action(tmp_path, root, base, head, run_repro=FakeExecutor(),
                                      INPUT_VERIFY_FIX="true", INPUT_FAIL_ON_REFUTED="true")
    assert [r["status"] for r in fix_rows(report)] == ["refuted"]
    assert code == 1
    assert check_calls(api)[0][2]["conclusion"] == "failure"


@pytest.mark.parametrize("base, head", [
    ("main", "HEAD"),
    ("HEAD~1", None),
    (None, None),
    ("a" * 40, None),
    (None, "b" * 40),
    ("A" * 40, "B" * 40),             # upper-case hex is not what GitHub sends
    ("a" * 39, "b" * 40),
    ("a" * 40 + "\n", "b" * 40),
    ("--output=/tmp/x" + "a" * 25, "b" * 40),
    (12345, ["b" * 40]),
])
def test_shas_must_be_full_hex_from_the_payload(tmp_path, repo, fake_verify, base, head):
    root, real_base, real_head = repo
    executor = FakeExecutor()
    # A PR body naming real commits must not stand in for the payload's SHAs.
    body = f"Fixes parse_url(); base {real_base} head {real_head}"
    _, _, comment, report = run_action(tmp_path, root, base, head, body=body, run_repro=executor,
                                       INPUT_VERIFY_FIX="true")
    assert fake_verify.calls == [] and executor.calls == []
    assert [r["status"] for r in fix_rows(report)] == ["unverified"]
    assert repro_rows(report) == []


def test_missing_commit_is_unverified_with_a_fetch_depth_hint(tmp_path, repo, fake_verify):
    root, base, _ = repo
    executor = FakeExecutor()
    missing = "0123456789abcdef" * 2 + "01234567"
    code, _, comment, report = run_action(tmp_path, root, base, missing, run_repro=executor,
                                          INPUT_VERIFY_FIX="true", INPUT_FAIL_ON_REFUTED="true")
    assert code == 0
    assert fake_verify.calls == [] and executor.calls == []
    rows = fix_rows(report)
    assert [r["status"] for r in rows] == ["unverified"]
    assert "commit not available" in rows[0]["detail"] and "fetch-depth" in rows[0]["detail"]
    assert missing[:12] in rows[0]["detail"]
    assert "fetch-depth" in comment


def test_missing_base_commit_too(tmp_path, repo, fake_verify):
    root, _, head = repo
    _, _, _, report = run_action(tmp_path, root, "f" * 40, head, run_repro=FakeExecutor(), INPUT_VERIFY_FIX="true")
    assert fake_verify.calls == []
    assert "commit not available, increase fetch-depth" in fix_rows(report)[0]["detail"]


def test_verify_fix_without_repro_adds_no_row(tmp_path, repo, fake_verify):
    root, base, head = repo
    _, _, _, report = run_action(tmp_path, root, base, head, run_repro=FakeExecutor(), INPUT_VERIFY_FIX="true",
                                 INPUT_REPRO="")
    assert fake_verify.calls == []
    assert fix_rows(report) == [] and repro_rows(report) == []


@pytest.mark.parametrize("command", [REPRO, ""])
def test_pr_text_never_becomes_the_command(tmp_path, repo, fake_verify, command):
    root, base, head = repo
    marker = tmp_path / "PWNED"
    body = f"Fixes parse_url(). Repro:\n```\ntouch {marker}\n```\n$(touch {marker})"
    executor = FakeExecutor()
    run_action(tmp_path, root, base, head, body=body, run_repro=executor,
               INPUT_VERIFY_FIX="true", INPUT_REPRO=command)
    assert [c["command"] for c in fake_verify.calls] == ([REPRO] if command else [])
    assert executor.calls == []
    assert not marker.exists()


def test_issue_events_have_no_commits_to_compare(tmp_path, repo, fake_verify):
    root, _, _ = repo
    event = tmp_path / "issue.json"
    event.write_text(json.dumps({"issue": {"number": 3, "title": "Bug", "body": "parse_url crashes"}}))
    executor = FakeExecutor()
    action.run({"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t",
                "RUNNER_TEMP": str(tmp_path), "INPUT_PATH": str(root), "INPUT_REPRO": REPRO,
                "INPUT_EXPECT": EXPECT, "INPUT_VERIFY_FIX": "true"}, FakeApi(), run_repro=executor)
    report = json.loads((tmp_path / action.REPORT_NAME).read_text())
    assert fake_verify.calls == []
    assert [r["status"] for r in fix_rows(report)] == ["unverified"]
    assert executor.calls == []


class RevisionExecutor:
    """Read committed content in real worktrees while faking the sandbox result."""

    def __init__(self, base: Status = Status.CONFIRMED, head: Status = Status.REFUTED):
        self.statuses = {BUGGY: base, FIXED: head}
        self.calls: list[tuple[Path, str, str, str]] = []

    def __call__(self, repo: Path, command: str, expect: str, timeout: int = 30) -> Evidence:
        self.calls.append((repo, git(repo, "rev-parse", "HEAD"), command, expect))
        status = self.statuses[(repo / "urls.py").read_text()]
        return Evidence(Claim("repro", command, command), status, "fake side result", "fake executor")


@pytest.mark.parametrize("base_status, head_status, expected, code, conclusion", [
    (Status.CONFIRMED, Status.REFUTED, "confirmed", 0, "neutral"),
    (Status.CONFIRMED, Status.CONFIRMED, "refuted", 1, "failure"),
    (Status.UNVERIFIED, Status.REFUTED, "unverified", 0, "neutral"),
    (Status.CONFIRMED, Status.UNVERIFIED, "unverified", 0, "neutral"),
    (Status.REFUTED, Status.REFUTED, "unverified", 0, "neutral"),
])
def test_real_git_uses_event_commits_and_preserves_checkout(tmp_path, repo, base_status, head_status,
                                                         expected, code, conclusion):
    root, base, head = repo
    git(root, "checkout", "-q", "--detach", base)
    (root / "urls.py").write_text("local edit\n")
    (root / "untracked.txt").write_text("keep me\n")
    before = git(root, "status", "--porcelain")
    executor = RevisionExecutor(base_status, head_status)
    result, api, comment, report = run_action(tmp_path, root, base, head, run_repro=executor,
                                             INPUT_VERIFY_FIX="true", INPUT_FAIL_ON_REFUTED="true")
    assert [call[1:] for call in executor.calls] == [(base, REPRO, EXPECT), (head, REPRO, EXPECT)]
    assert [r["status"] for r in fix_rows(report)] == [expected]
    assert repro_rows(report) == []
    assert base in comment and head in comment
    assert result == code
    assert check_calls(api)[0][2]["head_sha"] == head
    assert check_calls(api)[0][2]["conclusion"] == conclusion
    assert git(root, "rev-parse", "HEAD") == base
    assert git(root, "status", "--porcelain") == before
    assert (root / "urls.py").read_text() == "local edit\n"
    assert (root / "untracked.txt").read_text() == "keep me\n"
    assert all(path.resolve() != root.resolve() and not path.exists() for path, *_ in executor.calls)
    assert git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.skipif(sandbox.detect_isolation() is None, reason="no network isolation on this machine")
def test_real_git_and_sandbox_confirm_the_fix(tmp_path, repo):
    root, base, head = repo
    code, api, comment, report = run_action(tmp_path, root, base, head, INPUT_VERIFY_FIX="true")
    rows = fix_rows(report)
    assert [r["status"] for r in rows] == ["confirmed"], rows
    assert base in rows[0]["detail"] and head in rows[0]["detail"]
    assert repro_rows(report) == []
    assert code == 0
    assert int(read_outputs(tmp_path / "out.txt")["confirmed"]) >= 1
    assert git(root, "worktree", "list", "--porcelain").count("worktree ") == 1
