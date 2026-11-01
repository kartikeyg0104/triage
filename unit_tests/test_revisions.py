import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from claimcheck import cli, k8s, revisions, sandbox
from claimcheck.models import Claim, Evidence, Status

C, R, U = Status.CONFIRMED, Status.REFUTED, Status.UNVERIFIED


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@dataclass
class Repo:
    path: Path
    base: str
    head: str


@pytest.fixture
def repo(tmp_path) -> Repo:
    path = tmp_path / "checkout"
    path.mkdir()
    git(path, "init", "-q")
    (path / "version.txt").write_text("buggy\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "buggy")
    base = git(path, "rev-parse", "HEAD")
    (path / "version.txt").write_text("fixed\n")
    git(path, "commit", "-qam", "fixed")
    return Repo(path, base, git(path, "rev-parse", "HEAD"))


class FakeExecutor:
    """Stands in for sandbox.run_repro / KubernetesExecutor.run_repro and records where and when it ran.

    `delay` maps a version ("buggy" is base, "fixed" is head) to seconds to sleep; `error` is raised on every
    side, or only on the versions in `fail_on`."""

    def __init__(self, base: Status = C, head: Status = R, error: Exception | None = None,
                 delay: dict[str, float] | None = None, fail_on: tuple[str, ...] = ("buggy", "fixed")):
        self.statuses = {"buggy": base, "fixed": head}
        self.error = error
        self.delay = delay or {}
        self.fail_on = fail_on
        self.runs: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, repo: Path, command: str, expect: str, timeout: int = 30) -> Evidence:
        start = time.monotonic()
        version = (repo / "version.txt").read_text().strip()
        run = {"dir": Path(repo), "sha": git(repo, "rev-parse", "HEAD"), "version": version,
               "command": command, "expect": expect, "timeout": timeout, "start": start}
        with self.lock:
            self.runs.append(run)
        time.sleep(self.delay.get(version, 0))
        run["end"] = time.monotonic()
        run["dir_existed_at_end"] = Path(repo).is_dir()
        if self.error and version in self.fail_on:
            raise self.error
        status = self.statuses[version]
        return Evidence(Claim("repro", command, command), status, f"{status.value} on {version}",
                        f"fake: {command} on {version}")

    def by_version(self) -> dict[str, dict]:
        return {run["version"]: run for run in self.runs}


def worktrees(path: Path) -> list[str]:
    return [line for line in git(path, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


def verify(repo: Repo, executor, base: str | None = None, head: str | None = None, **kwargs) -> Evidence:
    return revisions.verify_fix(repo.path, repo.base if base is None else base, repo.head if head is None else head,
                                "python3 poc.py", "buffer overflow", executor, **kwargs)


def test_runs_base_and_head_in_their_own_worktrees(repo):
    fake = FakeExecutor()
    verify(repo, fake, base="HEAD~1", head="HEAD", timeout=7)
    runs = fake.by_version()
    assert len(fake.runs) == 2
    assert (runs["buggy"]["sha"], runs["fixed"]["sha"]) == (repo.base, repo.head)
    assert runs["buggy"]["dir"] != runs["fixed"]["dir"]
    for run in fake.runs:
        assert run["dir"].resolve() != repo.path.resolve()
        assert repo.path.resolve() not in run["dir"].resolve().parents
        assert (run["command"], run["expect"], run["timeout"]) == ("python3 poc.py", "buffer overflow", 7)
        assert not run["dir"].exists()


@pytest.mark.parametrize("base,head", [("HEAD", "HEAD"), ("HEAD~1", "HEAD~1"), ("same-base", "same-head")])
def test_same_commit_is_unverified_and_runs_nothing(repo, base, head):
    git(repo.path, "branch", "same-base", repo.head)
    git(repo.path, "branch", "same-head", repo.head)
    sha = git(repo.path, "rev-parse", base)
    fake = FakeExecutor()
    evidence = verify(repo, fake, base=base, head=head)
    assert evidence.status is U
    assert "base and head resolve to the same commit" in evidence.detail
    assert sha in evidence.detail
    assert "repro not run" in evidence.detail
    assert evidence.claim.kind == "fix"
    assert evidence.source == f"base {sha[:12]} → head {sha[:12]}"
    assert fake.runs == []
    assert worktrees(repo.path) == [f"worktree {repo.path.resolve()}"]


@pytest.mark.parametrize("base,head,expected,headline", [
    (C, R, C, "fix confirmed"),
    (C, C, R, "not fixed"),
    (R, R, U, "bug not reproduced on base"),
    (R, C, U, "bug not reproduced on base"),
    (U, R, U, "no clean reproduction on base"),
    (U, C, U, "no clean reproduction on base"),
    (U, U, U, "no clean reproduction on base"),
    (C, U, U, "head gave no clean result"),
])
def test_outcome_table(repo, base, head, expected, headline):
    evidence = verify(repo, FakeExecutor(base, head))
    assert evidence.status is expected
    assert headline in evidence.detail
    assert evidence.basis == "observed"
    assert evidence.claim.kind == "fix"
    assert f"base {repo.base}" in evidence.detail and f"head {repo.head}" in evidence.detail
    assert f"{base.value} on buggy" in evidence.detail and f"{head.value} on fixed" in evidence.detail
    assert evidence.source.startswith(f"base {repo.base[:12]} → head {repo.head[:12]}")
    assert "fake: python3 poc.py" in evidence.source
    assert "|" not in evidence.detail  # it is rendered inside a markdown table row


def test_checkout_is_untouched_and_worktrees_are_removed(repo):
    (repo.path / "version.txt").write_text("uncommitted edit\n")
    (repo.path / "scratch.txt").write_text("untracked\n")
    branch, status = git(repo.path, "symbolic-ref", "HEAD"), git(repo.path, "status", "--porcelain")
    fake = FakeExecutor()
    assert verify(repo, fake).status is C
    assert sorted(run["version"] for run in fake.runs) == ["buggy", "fixed"]  # committed content, not the dirty tree
    assert git(repo.path, "rev-parse", "HEAD") == repo.head
    assert git(repo.path, "symbolic-ref", "HEAD") == branch
    assert git(repo.path, "status", "--porcelain") == status
    assert (repo.path / "version.txt").read_text() == "uncommitted edit\n"
    assert worktrees(repo.path) == [f"worktree {repo.path.resolve()}"]


def test_worktrees_are_removed_when_the_executor_raises(repo):
    fake = FakeExecutor(error=RuntimeError("executor crashed"))
    with pytest.raises(RuntimeError, match="executor crashed"):
        verify(repo, fake)
    assert len(fake.runs) == 2
    assert not any(run["dir"].exists() for run in fake.runs)
    assert worktrees(repo.path) == [f"worktree {repo.path.resolve()}"]


def test_base_and_head_run_concurrently(repo):
    fake = FakeExecutor(delay={"buggy": 1.0, "fixed": 1.0})
    assert verify(repo, fake).status is C
    base, head = fake.by_version()["buggy"], fake.by_version()["fixed"]
    assert base["start"] < head["end"] and head["start"] < base["end"]  # sequential runs could not overlap


@pytest.mark.parametrize("failing", ["buggy", "fixed"])
def test_one_side_raising_still_waits_for_the_other_and_cleans_up_both(repo, failing):
    other = {"buggy": "fixed", "fixed": "buggy"}[failing]
    fake = FakeExecutor(error=RuntimeError(f"{failing} crashed"), fail_on=(failing,), delay={other: 0.3})
    with pytest.raises(RuntimeError, match=f"{failing} crashed"):
        verify(repo, fake)
    runs = fake.by_version()
    assert set(runs) == {"buggy", "fixed"}
    assert runs[other]["dir_existed_at_end"]  # its worktree was not removed under it
    assert not any(run["dir"].exists() for run in fake.runs)
    assert worktrees(repo.path) == [f"worktree {repo.path.resolve()}"]


def test_result_does_not_depend_on_which_side_finishes_first(repo):
    slow_base = verify(repo, FakeExecutor(delay={"buggy": 0.3}))
    slow_head = verify(repo, FakeExecutor(delay={"fixed": 0.3}))
    assert (slow_base.status, slow_base.detail, slow_base.source) == (slow_head.status, slow_head.detail, slow_head.source)
    assert slow_base.detail.index(f"base {repo.base}") < slow_base.detail.index(f"head {repo.head}")
    assert slow_base.source.endswith("fake: python3 poc.py on fixed")  # head's executor source, as before


def test_worktrees_are_created_one_at_a_time_before_any_repro_runs(repo, monkeypatch):
    real_git = revisions._git
    adds: list[tuple[float, float]] = []

    def recording_git(repo_, *args):
        start = time.monotonic()
        done = real_git(repo_, *args)
        if "worktree" in args and "add" in args:
            adds.append((start, time.monotonic()))
        return done

    monkeypatch.setattr(revisions, "_git", recording_git)
    fake = FakeExecutor()
    verify(repo, fake)
    assert len(adds) == 2
    (_, first_end), (second_start, second_end) = sorted(adds)
    assert first_end <= second_start
    assert second_end <= min(run["start"] for run in fake.runs)


def test_users_own_stale_worktree_is_not_pruned(repo, tmp_path):
    elsewhere = tmp_path / "on-an-unmounted-drive"
    git(repo.path, "worktree", "add", "-q", "--detach", str(elsewhere), repo.base)
    entry = f"worktree {elsewhere.resolve()}"
    elsewhere.rename(tmp_path / "moved")
    verify(repo, FakeExecutor())
    assert entry in worktrees(repo.path)


def test_prune_is_the_fallback_when_remove_fails(repo, monkeypatch):
    real_git = revisions._git
    monkeypatch.setattr(revisions, "_git", lambda repo_, *args: (
        subprocess.CompletedProcess(args, 1, "", "refused") if args[:2] == ("worktree", "remove") else real_git(repo_, *args)))
    verify(repo, FakeExecutor())
    assert worktrees(repo.path) == [f"worktree {repo.path.resolve()}"]


@pytest.mark.parametrize("side", ["base", "head"])
@pytest.mark.parametrize("ref", ["no-such-ref", "HEAD^{tree}", "--output=x", ""])
def test_bad_ref_is_a_setup_failure_and_runs_nothing(repo, side, ref):
    fake = FakeExecutor()
    evidence = verify(repo, fake, **{side: ref})
    assert evidence.status is U
    assert "setup failure" in evidence.detail and f"{side} ref" in evidence.detail
    assert fake.runs == []
    assert worktrees(repo.path) == [f"worktree {repo.path.resolve()}"]


def test_not_a_git_checkout_is_a_setup_failure(tmp_path):
    fake = FakeExecutor()
    evidence = revisions.verify_fix(tmp_path, "HEAD~1", "HEAD", "x", "y", fake)
    assert evidence.status is U
    assert "setup failure" in evidence.detail
    assert fake.runs == []


def test_repo_subdirectory_is_run_from_the_same_subdirectory(tmp_path):
    root = tmp_path / "mono"
    (root / "pkg").mkdir(parents=True)
    git(root, "init", "-q")
    (root / "pkg" / "version.txt").write_text("buggy\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "buggy")
    (root / "pkg" / "version.txt").write_text("fixed\n")
    git(root, "commit", "-qam", "fixed")
    fake = FakeExecutor()
    evidence = revisions.verify_fix(root / "pkg", "HEAD~1", "HEAD", "x", "y", fake)
    assert evidence.status is C
    assert [run["dir"].name for run in fake.runs] == ["pkg", "pkg"]


def test_subdirectory_missing_at_a_revision_is_a_setup_failure(tmp_path):
    root = tmp_path / "mono"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "commit", "-q", "--allow-empty", "-m", "before pkg existed")
    (root / "pkg").mkdir()
    (root / "pkg" / "version.txt").write_text("fixed\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add pkg")
    fake = FakeExecutor()
    evidence = revisions.verify_fix(root / "pkg", "HEAD~1", "HEAD", "x", "y", fake)
    assert evidence.status is U
    assert "setup failure" in evidence.detail and "pkg" in evidence.detail
    assert [run["version"] for run in fake.runs] == ["fixed"]  # head still ran, for the summary


def test_checkout_hooks_do_not_run(repo):
    hook = repo.path / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{repo.path / 'hook-ran'}'\n")
    hook.chmod(0o755)
    verify(repo, FakeExecutor())
    assert not (repo.path / "hook-ran").exists()


def write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def check(repo: Repo, tmp_path: Path, *extra: str) -> list[str]:
    report = write(tmp_path, "report.txt", "Fixes #120. Repro: run `python3 evil.py --from-report`")
    return ["check", "--repo", str(repo.path), "--report", report, "--no-model", *extra]


def test_cli_verifies_the_fix_with_the_repro_flag_only(repo, tmp_path, monkeypatch, capsys):
    fake = FakeExecutor()
    monkeypatch.setattr(sandbox, "run_repro", fake)
    code = cli.main(check(repo, tmp_path, "--repro", "python3 poc.py", "--expect", "buffer overflow",
                          "--base", repo.base, "--head", repo.head, "--json"))
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [run["command"] for run in fake.runs] == ["python3 poc.py", "python3 poc.py"]
    assert [e["kind"] for e in data["evidence"]] == ["fix"]  # replaces the single repro row
    assert data["evidence"][0]["status"] == "confirmed"
    assert repo.base in data["evidence"][0]["detail"] and repo.head in data["evidence"][0]["detail"]


@pytest.mark.parametrize("base,head", [("HEAD", "HEAD"), ("same-base", "same-head")])
def test_cli_same_commit_is_unverified_and_runs_nothing(repo, tmp_path, monkeypatch, capsys, base, head):
    git(repo.path, "branch", "same-base", repo.head)
    git(repo.path, "branch", "same-head", repo.head)
    fake = FakeExecutor(C, C)
    monkeypatch.setattr(sandbox, "run_repro", fake)
    code = cli.main(check(repo, tmp_path, "--repro", "python3 poc.py", "--expect", "buffer overflow",
                          "--base", base, "--head", head, "--json"))
    data = json.loads(capsys.readouterr().out)
    assert code == 2
    assert data["verdict"] == "unverified"
    assert len(data["evidence"]) == 1
    evidence = data["evidence"][0]
    assert evidence["kind"] == "fix" and evidence["status"] == "unverified"
    assert "base and head resolve to the same commit" in evidence["detail"]
    assert repo.head in evidence["detail"]
    assert fake.runs == []


def test_cli_without_base_and_head_still_runs_a_single_repro(repo, tmp_path, monkeypatch, capsys):
    fake = FakeExecutor()
    monkeypatch.setattr(sandbox, "run_repro", fake)
    cli.main(check(repo, tmp_path, "--repro", "python3 poc.py", "--expect", "buffer overflow", "--json"))
    data = json.loads(capsys.readouterr().out)
    assert [e["kind"] for e in data["evidence"]] == ["repro"]
    assert [run["dir"] for run in fake.runs] == [repo.path]


@pytest.mark.parametrize("extra,message", [
    (["--repro", "x", "--base", "HEAD~1"], "--base and --head"),
    (["--repro", "x", "--head", "HEAD"], "--base and --head"),
    (["--base", "HEAD~1", "--head", "HEAD"], "--repro"),
])
def test_cli_rejects_incomplete_fix_flags(repo, tmp_path, monkeypatch, capsys, extra, message):
    fake = FakeExecutor()
    monkeypatch.setattr(sandbox, "run_repro", fake)
    assert cli.main(check(repo, tmp_path, *extra)) == 2
    assert message in capsys.readouterr().err
    assert fake.runs == []


def test_cli_uses_the_kubernetes_executor_on_both_sides(repo, tmp_path, monkeypatch, capsys):
    fake = FakeExecutor()

    class FakeKubernetes:
        def __init__(self, **kwargs):
            self.run_repro = fake

    monkeypatch.setattr(k8s, "KubernetesExecutor", FakeKubernetes)
    code = cli.main(check(repo, tmp_path, "--repro", "python3 poc.py", "--expect", "buffer overflow",
                          "--base", repo.base, "--head", repo.head, "--executor", "kubernetes"))
    assert code == 0
    assert sorted(run["sha"] for run in fake.runs) == sorted([repo.base, repo.head])
    assert f"base {repo.base[:12]} → head {repo.head[:12]}" in capsys.readouterr().out
