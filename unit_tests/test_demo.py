"""The presentation must finish without a cluster and keep setup problems inconclusive."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from claimcheck import sandbox

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo.sh"
HEADERS = (
    "=== Local sandbox ===",
    "=== Kubernetes ===",
    "=== Fix verification ===",
    "=== SARIF output ===",
    "=== Evaluation summary ===",
)


def executable(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o755)
    return path


@pytest.fixture
def demo_env(tmp_path: Path) -> dict[str, str]:
    """A kubectl recorder proves that no-cluster mode never probes a real context."""
    tools = tmp_path / "tools"
    tools.mkdir()
    executable(tools / "kubectl", """import json, os, sys
with open(os.environ['DEMO_KUBECTL_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
sys.exit(int(os.environ.get('DEMO_KUBECTL_EXIT', '1')))
""")
    temporary = tmp_path / "temporary files"
    temporary.mkdir()
    env = {**os.environ, "PYTHON": sys.executable, "TMPDIR": str(temporary),
           "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "DEMO_KUBECTL_LOG": str(tmp_path / "kubectl.jsonl")}
    env.pop("CLAIMCHECK_K8S_CONTEXT", None)
    return env


@pytest.fixture
def fake_cli(tmp_path: Path, demo_env: dict[str, str]) -> dict[str, str]:
    """Control tool outcomes while the shell still builds and cleans a real git repo."""
    launcher = executable(tmp_path / "fake python", """import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['DEMO_CLI_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[:3] == ['-m', 'claimcheck', 'check']:
    if '--help' in args:
        print('check options: ' + ('--sarif PATH' if os.environ.get('DEMO_SARIF', '1') == '1' else '--json'))
        sys.exit(0)
    code = int(os.environ.get('DEMO_CHECK_EXIT', '2'))
    if code in (0, 1, 2) and not os.environ.get('DEMO_INVALID_REPORT'):
        print('**Verdict: ' + ('supported', 'refuted', 'unverified')[code] + '**')
    else:
        print('unexpected check failure', file=sys.stderr)
    if '--sarif' in args:
        pathlib.Path(args[args.index('--sarif') + 1]).write_text(json.dumps({'version': '2.1.0', 'runs': []}))
    sys.exit(code)
if args[:2] == ['-m', 'claimcheck.evaluate']:
    print('evaluation: claims 1/1, verdicts 1/1, wrong confirmed 0, wrong refuted 0, skipped 0')
    sys.exit(int(os.environ.get('DEMO_EVAL_EXIT', '0')))
sys.exit(99)
""")
    return {**demo_env, "PYTHON": str(launcher), "DEMO_CLI_LOG": str(tmp_path / "cli.jsonl")}


def run_demo(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(DEMO), *args], cwd=env["TMPDIR"], env=env,
                          capture_output=True, text=True, timeout=120)


def recorded(env: dict[str, str], key: str) -> list[list[str]]:
    path = Path(env[key])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def assert_sections(output: str) -> None:
    positions = [output.index(header) for header in HEADERS]
    assert positions == sorted(positions)


def test_demo_without_cluster_runs_the_real_flow(demo_env: dict[str, str]) -> None:
    proc = run_demo(demo_env, "--no-cluster")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert_sections(proc.stdout)
    assert "Skipped: Kubernetes disabled (--no-cluster)." in proc.stdout
    assert recorded(demo_env, "DEMO_KUBECTL_LOG") == []
    assert "**Verdict: refuted**" in proc.stdout.split(HEADERS[1])[0]
    fix = proc.stdout.split(HEADERS[2])[1].split(HEADERS[3])[0]
    assert len(set(re.findall(r"\b[0-9a-f]{40}\b", fix))) == 2
    if sandbox.detect_isolation() is None:
        assert "**Verdict: unverified**" in fix and "inconclusive" in fix
    else:
        assert "fix confirmed" in fix
    sarif = json.loads(proc.stdout.split(HEADERS[3])[1].split(HEADERS[4])[0])
    assert sarif["version"] == "2.1.0"
    findings = sarif["runs"][0]["results"]
    assert any(row["level"] == "error" and "parse_url_v2" in row["message"]["text"] for row in findings)
    summary = proc.stdout.split(HEADERS[4])[1]
    assert "cases, claims" in summary and "wrong confirmed" in summary and "skipped" in summary
    assert list(Path(demo_env["TMPDIR"]).iterdir()) == []


def test_missing_local_isolation_does_not_confirm_a_fix(tmp_path: Path, demo_env: dict[str, str]) -> None:
    launcher = executable(tmp_path / "no isolation python", """import runpy, sys
from claimcheck import sandbox
sandbox.detect_isolation = lambda: None
module = sys.argv[2]
sys.argv = sys.argv[2:]
runpy.run_module(module, run_name='__main__')
""")
    proc = run_demo({**demo_env, "PYTHON": str(launcher)}, "--no-cluster")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert_sections(proc.stdout)
    fix = proc.stdout.split(HEADERS[2])[1].split(HEADERS[3])[0]
    assert "**Verdict: unverified**" in fix
    assert "fix confirmed" not in fix and "not fixed" not in fix
    assert "no clean reproduction on base" in fix
    assert "skipped" in proc.stdout.split(HEADERS[4])[1]


@pytest.mark.parametrize("code", [0, 1, 2])
def test_verdict_exit_codes_are_expected(fake_cli: dict[str, str], code: int) -> None:
    proc = run_demo({**fake_cli, "DEMO_CHECK_EXIT": str(code)}, "--no-cluster")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert_sections(proc.stdout)
    calls = recorded(fake_cli, "DEMO_CLI_LOG")
    checks = [args for args in calls if args[:3] == ["-m", "claimcheck", "check"] and "--help" not in args]
    assert len(checks) == 3
    assert all("--no-model" in args for args in checks)
    assert "--repro" in checks[0]
    assert "--base" in checks[1] and "--head" in checks[1]
    assert not Path(checks[1][checks[1].index("--repo") + 1]).exists()
    assert list(Path(fake_cli["TMPDIR"]).iterdir()) == []


def test_unreachable_cluster_is_skipped(fake_cli: dict[str, str]) -> None:
    proc = run_demo(fake_cli)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert_sections(proc.stdout)
    assert "Skipped: kind-claimcheck is not reachable" in proc.stdout
    probes = recorded(fake_cli, "DEMO_KUBECTL_LOG")
    assert len(probes) == 1
    assert "--context" in probes[0] and "kind-claimcheck" in probes[0]
    assert any(arg.startswith("--request-timeout=") for arg in probes[0])
    assert not any("kubernetes" in args for args in recorded(fake_cli, "DEMO_CLI_LOG"))


def test_reachable_cluster_uses_the_same_report(fake_cli: dict[str, str]) -> None:
    proc = run_demo({**fake_cli, "DEMO_KUBECTL_EXIT": "0"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert_sections(proc.stdout)
    checks = [args for args in recorded(fake_cli, "DEMO_CLI_LOG") if "--repro" in args]
    local, cluster = checks[:2]
    for flag in ("--repo", "--report", "--diff", "--repro", "--expect"):
        assert local[local.index(flag) + 1] == cluster[cluster.index(flag) + 1]
    assert cluster[cluster.index("--executor") + 1] == "kubernetes"
    assert cluster[cluster.index("--kube-context") + 1] == "kind-claimcheck"


def test_sarif_is_optional(fake_cli: dict[str, str]) -> None:
    proc = run_demo({**fake_cli, "DEMO_SARIF": "0"}, "--no-cluster")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert_sections(proc.stdout)
    assert "Skipped: SARIF output is not available" in proc.stdout
    assert not any("--sarif" in args for args in recorded(fake_cli, "DEMO_CLI_LOG"))


@pytest.mark.parametrize("code,invalid", [(7, ""), (2, "1"), (0, "1")])
def test_unexpected_cli_errors_fail_and_clean_up(fake_cli: dict[str, str], code: int, invalid: str) -> None:
    proc = run_demo({**fake_cli, "DEMO_CHECK_EXIT": str(code), "DEMO_INVALID_REPORT": invalid}, "--no-cluster")
    assert proc.returncode != 0
    assert "unexpected" in proc.stderr.lower()
    assert HEADERS[4] not in proc.stdout
    assert list(Path(fake_cli["TMPDIR"]).iterdir()) == []


@pytest.mark.parametrize("code,expected", [(1, 0), (2, 2)])
def test_evaluation_findings_and_errors_have_distinct_exits(fake_cli: dict[str, str], code: int, expected: int) -> None:
    proc = run_demo({**fake_cli, "DEMO_EVAL_EXIT": str(code)}, "--no-cluster")
    assert proc.returncode == expected, proc.stdout + proc.stderr
    assert_sections(proc.stdout)
    assert list(Path(fake_cli["TMPDIR"]).iterdir()) == []
