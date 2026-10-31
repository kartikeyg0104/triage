"""Fix verification end to end: a real git repo, real worktrees and the real local sandbox."""

import json
import subprocess
from pathlib import Path

import pytest

from claimcheck import cli, revisions, sandbox
from claimcheck.models import Status

needs_isolation = pytest.mark.skipif(sandbox.detect_isolation() is None, reason="no network isolation on this machine")

BUGGY = "def parse_url(s):\n    if len(s) > 64:\n        raise RuntimeError('buffer overflow')\n    return s\n"
FIXED = "def parse_url(s):\n    return s[:64]\n"
POC = "from urls import parse_url\nparse_url('x' * 100)\nprint('parsed fine')\n"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """main has the bug; branch fix-120 fixes it; branch fake-fix-120 only edits the README."""
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "checkout", "-q", "-b", "main")
    (path / "urls.py").write_text(BUGGY)
    (path / "poc.py").write_text(POC)
    (path / "README").write_text("parser\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "parser")
    git(path, "checkout", "-q", "-b", "fix-120")
    (path / "urls.py").write_text(FIXED)
    git(path, "commit", "-qam", "Fix #120: truncate long URLs")
    git(path, "checkout", "-q", "-b", "fake-fix-120", "main")
    (path / "README").write_text("parser, now safe\n")
    git(path, "commit", "-qam", "Fix #120 (does not)")
    git(path, "checkout", "-q", "main")
    return path


def snapshot(path: Path) -> dict:
    return {"head": git(path, "rev-parse", "HEAD"), "branch": git(path, "symbolic-ref", "HEAD"),
            "status": git(path, "status", "--porcelain", "--untracked-files=all"),
            "branches": git(path, "for-each-ref", "--format=%(refname) %(objectname)"),
            "worktrees": git(path, "worktree", "list", "--porcelain"), "stash": git(path, "stash", "list"),
            "files": {p.name: p.read_text() for p in path.iterdir() if p.is_file()}}


@needs_isolation
def test_real_fix_is_confirmed(repo):
    base, head = git(repo, "rev-parse", "main"), git(repo, "rev-parse", "fix-120")
    evidence = revisions.verify_fix(repo, "main", "fix-120", "python3 poc.py", "buffer overflow", sandbox.run_repro)
    assert evidence.status is Status.CONFIRMED, evidence.detail
    assert evidence.basis == "observed"
    assert base in evidence.detail and head in evidence.detail
    assert evidence.source.startswith(f"base {base[:12]} → head {head[:12]}")
    assert "RuntimeError: buffer overflow" in evidence.detail and "parsed fine" in evidence.detail


@needs_isolation
def test_change_that_does_not_fix_is_refuted(repo):
    evidence = revisions.verify_fix(repo, "main", "fake-fix-120", "python3 poc.py", "buffer overflow", sandbox.run_repro)
    assert evidence.status is Status.REFUTED, evidence.detail
    assert "not fixed" in evidence.detail


@needs_isolation
def test_swapped_refs_are_not_confirmed(repo):
    evidence = revisions.verify_fix(repo, "fix-120", "main", "python3 poc.py", "buffer overflow", sandbox.run_repro)
    assert evidence.status is Status.UNVERIFIED, evidence.detail
    assert "bug not reproduced on base" in evidence.detail


@needs_isolation
def test_missing_repro_file_is_a_setup_failure_not_a_fix(repo):
    evidence = revisions.verify_fix(repo, "main", "fix-120", "python3 missing.py", "buffer overflow", sandbox.run_repro)
    assert evidence.status is Status.UNVERIFIED
    assert "setup or environment failure" in evidence.detail


@needs_isolation
def test_users_checkout_and_working_tree_are_untouched(repo):
    (repo / "urls.py").write_text(BUGGY + "# local edit, not committed\n")
    (repo / "notes.txt").write_text("untracked\n")
    before = snapshot(repo)
    evidence = revisions.verify_fix(repo, "main", "fix-120", "python3 poc.py", "buffer overflow", sandbox.run_repro)
    assert evidence.status is Status.CONFIRMED, evidence.detail
    assert snapshot(repo) == before
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not (repo / "__pycache__").exists()


@needs_isolation
def test_cli_checks_a_fix_claim_across_revisions(repo, tmp_path, capsys):
    report = tmp_path / "pr.txt"
    report.write_text("Fixes #120: `parse_url()` overflowed on long URLs.")
    before = snapshot(repo)
    code = cli.main(["check", "--repo", str(repo), "--report", str(report), "--no-model", "--json",
                     "--repro", "python3 poc.py", "--expect", "buffer overflow", "--base", "main", "--head", "fix-120"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0, data
    assert data["verdict"] == "supported"
    fix = [e for e in data["evidence"] if e["kind"] == "fix"]
    assert len(fix) == 1 and fix[0]["status"] == "confirmed" and fix[0]["basis"] == "observed"
    assert git(repo, "rev-parse", "main") in fix[0]["detail"] and git(repo, "rev-parse", "fix-120") in fix[0]["detail"]
    assert snapshot(repo) == before


@needs_isolation
def test_cli_exits_1_when_the_fix_does_not_fix(repo, tmp_path, capsys):
    report = tmp_path / "pr.txt"
    report.write_text("Fixes #120")
    code = cli.main(["check", "--repo", str(repo), "--report", str(report), "--no-model",
                     "--repro", "python3 poc.py", "--expect", "buffer overflow", "--base", "main", "--head", "fake-fix-120"])
    assert code == 1
    assert "not fixed" in capsys.readouterr().out
