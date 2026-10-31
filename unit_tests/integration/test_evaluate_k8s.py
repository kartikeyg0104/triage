"""Ground-truth evaluation on kind; opt in with CLAIMCHECK_K8S_CONTEXT."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claimcheck import evaluate

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
ROOT = Path(__file__).resolve().parents[2]
NAMESPACE = "claimcheck-it"

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def test_ground_truth_cases_through_kubernetes(tmp_path):
    """Score every case and compare shared answers with the local evaluation."""
    reports = {}
    for executor in ("local", "kubernetes"):
        out, js = tmp_path / f"{executor}.md", tmp_path / f"{executor}.json"
        command = [sys.executable, "-m", "claimcheck.evaluate", "--cases", str(ROOT / "eval" / "cases"),
                   "--runs", "2", "--executor", executor, "--out", str(out), "--json", str(js)]
        if executor == "kubernetes":
            command += ["--kube-context", CONTEXT, "--k8s-namespace", NAMESPACE]
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=600)
        assert done.returncode == 0, done.stdout + done.stderr
        reports[executor] = json.loads(js.read_text()), out.read_text()

    local, _ = reports["local"]
    cluster, markdown = reports["kubernetes"]
    metrics = cluster["metrics"]
    cases = evaluate.load_cases(ROOT / "eval" / "cases")
    assert metrics["cases"] == metrics["scored"] == len(cases)
    assert metrics["skipped"] == metrics["wrong_confirmed"] == metrics["wrong_refuted"] == 0
    assert metrics["claims_correct"] == metrics["claims_total"]
    assert metrics["verdicts_correct"] == metrics["repeatable"] == len(cases)
    assert set(metrics) == set(local["metrics"])
    if not local["metrics"]["skipped"]:
        assert metrics == local["metrics"]
    for local_case, cluster_case in zip(local["cases"], cluster["cases"], strict=True):
        assert local_case["name"] == cluster_case["name"]
        if not local_case["skipped"]:
            assert local_case["claims"] == cluster_case["claims"]
            assert local_case["overall"] == cluster_case["overall"]
        assert cluster_case["correct"] and cluster_case["repeatable"]
        for run in cluster_case["runs"]:
            if "repro" in run["claims"]:
                assert run["repro_source"].startswith(f"kubernetes[{NAMESPACE}/")
                assert "(egress blocked, verified):" in run["repro_source"]
            else:
                assert run["repro_source"] is None
    assert cluster["executor"] == {"name": "kubernetes", "context": CONTEXT, "namespace": NAMESPACE,
                                   "egress_verified": True}
    repro_runs = sum(bool(case.repro) for case in cases) * 2
    assert repro_runs > 0
    assert f"- Egress verified: yes ({repro_runs}/{repro_runs} repro runs)" in markdown
    assert f"- Executor: kubernetes (context {CONTEXT}, namespace {NAMESPACE})" in markdown
    assert "`claimcheck.k8s.KubernetesExecutor.run_repro`" in markdown
    assert "| Wrong confirmed (cases) | 0 |" in markdown
    assert "| Wrong refuted (cases) | 0 |" in markdown
