"""Integration tests: fix verification (--base/--head) with real Jobs on a real cluster (#96).

Each revision is checked out as a git worktree and run as its own Job, so a confirmed fix means two
real pods: one that failed with the expected text on base, one that exited 0 on head.

Cluster tests require CLAIMCHECK_K8S_CONTEXT=<kubectl context> (CI uses kind); cleanup tests run offline.
"""

import functools
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from claimcheck import cli, k8s, revisions
from claimcheck.models import Status

from ._runs import RecordingRun, left_behind

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
NAMESPACE = "claimcheck-it"
IMAGE = os.environ.get("CLAIMCHECK_K8S_IMAGE", "python:3.12-slim")
EXPECT = "buffer overflow in parse_url"
REPRO = "python3 poc.py"

cluster = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")

IDENTITY = {"GIT_AUTHOR_NAME": "example", "GIT_AUTHOR_EMAIL": "",
            "GIT_COMMITTER_NAME": "example", "GIT_COMMITTER_EMAIL": ""}
BUGGY = "def parse_url(s):\n    if len(s) > 64:\n        raise RuntimeError('buffer overflow in parse_url')\n    return s\n"
FIXED = "def parse_url(s):\n    return s[:64]\n"
POC = """import os, socket
from urls import parse_url

api = (os.environ.get("KUBERNETES_SERVICE_HOST", "10.96.0.1"),
       int(os.environ.get("KUBERNETES_SERVICE_PORT", "443")))
for host, port in [api, ("1.1.1.1", 53), ("8.8.8.8", 443)]:
    with socket.socket() as connection:
        connection.settimeout(3)
        try:
            connection.connect((host, port))
        except OSError:
            continue
        print("CONNECTED", host, port, flush=True)
        raise SystemExit(2)
try:
    parse_url('x' * 100)
except RuntimeError as error:
    print(f"RuntimeError: {error}", flush=True)
    code = 1
else:
    print('parsed fine', flush=True)
    code = 0
print("POD", socket.gethostname(), flush=True)
print("EGRESS-BLOCKED", flush=True)
raise SystemExit(code)
"""


def kubectl(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], input=input, capture_output=True, text=True, timeout=120)


def git(repo: Path, *args: str, date: str = "2024-01-01T00:00:00+00:00") -> str:
    env = {**os.environ, **IDENTITY, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                          capture_output=True, text=True, check=True, env=env, timeout=120)
    return done.stdout.strip()


@pytest.fixture(scope="module")
def namespace() -> Iterator[None]:
    manifest = json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
    assert kubectl("apply", "-f", "-", input=manifest).returncode == 0
    yield


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A buggy commit, then a commit that fixes it. Returns (repo, buggy sha, fixed sha)."""
    path = tmp_path / "project"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "urls.py").write_text(BUGGY)
    (path / "poc.py").write_text(POC)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "parser", date="2024-01-01T00:00:00+00:00")
    buggy = git(path, "rev-parse", "HEAD")
    (path / "urls.py").write_text(FIXED)
    git(path, "commit", "-qam", "Fix parse_url overflow", date="2024-01-02T00:00:00+00:00")
    fixed = git(path, "rev-parse", "HEAD")
    return path, buggy, fixed


def assert_cleaned(names: list[str]) -> None:
    """Only this test's runs may contribute leftovers on a shared cluster."""
    assert left_behind(kubectl, NAMESPACE, names) == []
    selector = f"{k8s.RUN_LABEL} in ({','.join(names)})"
    done = kubectl("wait", "pods", "-n", NAMESPACE, "-l", selector, "--for=delete", "--timeout=30s")
    assert done.returncode == 0, done.stderr
    done = kubectl("get", "pods", "-n", NAMESPACE, "-l", selector, "-o", "name")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == [], done.stdout


@pytest.mark.parametrize("kind", ["jobs", "pods", "configmaps", "networkpolicies"])
def test_cleanup_rejects_each_kind_of_leftover(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    names = ["claimcheck-own-probe", "claimcheck-own-base", "claimcheck-own-head"]
    queries: list[tuple[str, ...]] = []

    def leftover(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        queries.append(args)
        output = f"{kind}/claimcheck-own-base\n" if args[0] == "get" and kind in args[1].split(",") else ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(__name__ + ".kubectl", leftover)
    with pytest.raises(AssertionError):
        assert_cleaned(names)
    for args in queries:
        assert args[0] in {"get", "wait"}
        assert args[args.index("-n") + 1] == NAMESPACE
        assert args[args.index("-l") + 1] == f"{k8s.RUN_LABEL} in ({','.join(names)})"


@pytest.mark.parametrize("stage", ["objects", "wait", "pods"])
def test_cleanup_query_failure_is_not_a_clean_run(stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        current = "wait" if args[0] == "wait" else "pods" if args[1] == "pods" else "objects"
        return subprocess.CompletedProcess(args, int(current == stage), "", "cluster unavailable")

    monkeypatch.setattr(__name__ + ".kubectl", unavailable)
    with pytest.raises(AssertionError, match="cluster unavailable"):
        assert_cleaned(["claimcheck-own-base"])


def test_cleanup_checks_only_recorded_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[tuple[str, ...]] = []

    def cleaned(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        queries.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(__name__ + ".kubectl", cleaned)
    assert_cleaned(["claimcheck-own-base", "claimcheck-own-head"])
    assert queries
    for args in queries:
        assert args[0] in {"get", "wait"}
        assert args[args.index("-n") + 1] == NAMESPACE
        assert args[args.index("-l") + 1] == f"{k8s.RUN_LABEL} in (claimcheck-own-base,claimcheck-own-head)"


def assert_side_pods(detail: str, recorder: RecordingRun) -> None:
    """Both revisions must report their own pod and blocked egress."""
    assert detail.count("EGRESS-BLOCKED") == 2, detail
    assert "CONNECTED" not in detail, detail
    assert len(recorder.names) == 3, recorder.names
    for name in recorder.names[1:]:
        assert f"POD {name}-" in detail, detail


@pytest.fixture
def recorder(namespace: None) -> Iterator[RecordingRun]:
    """Records the run names this test applies; afterwards, exactly those objects must be gone."""
    recording = RecordingRun()
    yield recording
    names = recording.names
    assert_cleaned(names)
    assert len(names) == 3, names  # the network probe, then one Job per revision


def executor(recorder: RecordingRun) -> k8s.KubernetesExecutor:
    return k8s.KubernetesExecutor(namespace=NAMESPACE, image=IMAGE, context=CONTEXT, deadline=180, run=recorder)


@cluster
def test_real_fix_is_confirmed_across_two_pods(repo: tuple[Path, str, str], recorder: RecordingRun) -> None:
    path, buggy, fixed = repo
    evidence = revisions.verify_fix(path, buggy, fixed, REPRO, EXPECT, run_repro=executor(recorder).run_repro)
    assert evidence.status is Status.CONFIRMED, evidence.detail
    assert buggy in evidence.detail and fixed in evidence.detail
    assert f"RuntimeError: {EXPECT}" in evidence.detail and "parsed fine" in evidence.detail
    assert evidence.source.startswith(f"base {buggy[:12]} → head {fixed[:12]}")
    assert "egress blocked, verified" in evidence.source, evidence.source
    assert f"kubernetes[{NAMESPACE}/" in evidence.source
    assert_side_pods(evidence.detail, recorder)


@cluster
def test_swapped_refs_are_not_confirmed(repo: tuple[Path, str, str], recorder: RecordingRun) -> None:
    path, buggy, fixed = repo
    evidence = revisions.verify_fix(path, fixed, buggy, REPRO, EXPECT, run_repro=executor(recorder).run_repro)
    assert evidence.status is not Status.CONFIRMED, evidence.detail
    assert evidence.status is Status.UNVERIFIED, evidence.detail
    assert "bug not reproduced on base" in evidence.detail
    assert evidence.source.startswith(f"base {fixed[:12]} → head {buggy[:12]}")
    assert "egress blocked, verified" in evidence.source, evidence.source
    assert_side_pods(evidence.detail, recorder)


@cluster
@pytest.mark.parametrize("swapped", [False, True], ids=["fixed", "swapped"])
def test_cli_verifies_a_fix_on_kubernetes(repo: tuple[Path, str, str], recorder: RecordingRun, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                        swapped: bool) -> None:
    # The CLI builds its own executor; inject only the recording `run`, everything else is the real path.
    monkeypatch.setattr(k8s, "KubernetesExecutor", functools.partial(k8s.KubernetesExecutor, run=recorder))
    path, buggy, fixed = repo
    base, head = (fixed, buggy) if swapped else (buggy, fixed)
    report = tmp_path / "pr.txt"
    report.write_text("Fixes #120: `parse_url()` overflowed on long URLs.")
    code = cli.main(["check", "--repo", str(path), "--report", str(report), "--no-model", "--json",
                     "--repro", REPRO, "--expect", EXPECT,
                     "--executor", "kubernetes", "--kube-context", CONTEXT, "--k8s-namespace", NAMESPACE,
                     "--k8s-image", IMAGE, "--base", base, "--head", head])
    data = json.loads(capsys.readouterr().out)
    assert code == (2 if swapped else 0), data
    assert data["verdict"] == ("unverified" if swapped else "supported"), data
    fix = [e for e in data["evidence"] if e["kind"] == "fix"]
    assert len(fix) == 1, fix
    assert fix[0]["status"] == ("unverified" if swapped else "confirmed"), fix
    assert buggy in fix[0]["detail"] and fixed in fix[0]["detail"]
    assert fix[0]["source"].startswith(f"base {base[:12]} → head {head[:12]}")
    assert "egress blocked, verified" in fix[0]["source"], fix[0]["source"]
    assert_side_pods(fix[0]["detail"], recorder)
