"""Integration tests: real Jobs on a real cluster.

Run with CLAIMCHECK_K8S_CONTEXT=<kubectl context> (CI uses a kind cluster). Skipped otherwise.
"""

import json
import os
import subprocess

import pytest

from claimcheck import k8s
from claimcheck.models import Status

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
NAMESPACE = "claimcheck-it"
IMAGE = os.environ.get("CLAIMCHECK_K8S_IMAGE", "python:3.12-slim")

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def kubectl(*args, input=None):
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], input=input, capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module", autouse=True)
def namespace():
    manifest = json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
    assert kubectl("apply", "-f", "-", input=manifest).returncode == 0
    yield


@pytest.fixture
def run_in_pod(tmp_path):
    def run(script: str, expect: str):
        (tmp_path / "probe.py").write_text(script)
        executor = k8s.KubernetesExecutor(namespace=NAMESPACE, image=IMAGE, context=CONTEXT, deadline=180)
        evidence = executor.run_repro(tmp_path, "python3 probe.py", expect, timeout=90)
        assert "egress blocked, verified" in evidence.source, evidence.detail
        return evidence
    return run


def test_reproduces_a_real_failure_in_a_pod(run_in_pod):
    evidence = run_in_pod("raise RuntimeError('buffer overflow in parse_url')\n", "buffer overflow in parse_url")
    assert evidence.status is Status.CONFIRMED, evidence.detail
    assert evidence.source.startswith(f"kubernetes[{NAMESPACE}/")


def test_command_that_succeeds_is_refuted(run_in_pod):
    assert run_in_pod("print('fine')\n", "buffer overflow").status is Status.REFUTED


def test_pod_is_non_root_read_only_and_has_no_service_account_token(run_in_pod):
    script = """
import os, sys
problems = []
if os.getuid() == 0:
    problems.append("running as root")
if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"):
    problems.append("service account token mounted")
try:
    open("/etc/claimcheck-write-test", "w")
    problems.append("root filesystem is writable")
except OSError:
    pass
print("PROBLEMS:", problems) if problems else print("ISOLATION-OK")
sys.exit(1)
"""
    evidence = run_in_pod(script, "ISOLATION-OK")
    assert evidence.status is Status.CONFIRMED, evidence.detail


def test_network_egress_is_blocked(run_in_pod):
    script = """
import os, socket, sys
targets = [(os.environ.get("KUBERNETES_SERVICE_HOST", "10.96.0.1"), 443), ("1.1.1.1", 53), ("8.8.8.8", 443)]
for host, port in targets:
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        print("CONNECTED", host, port)
        sys.exit(0)
    except OSError:
        pass
print("BLOCKED")
sys.exit(1)
"""
    evidence = run_in_pod(script, "BLOCKED")
    assert "CONNECTED" not in evidence.detail, evidence.detail
    assert evidence.status is Status.CONFIRMED, evidence.detail


def test_nothing_is_left_behind(run_in_pod):
    run_in_pod("raise SystemExit(1)\n", "")
    left = kubectl("get", "jobs,configmaps,networkpolicies", "-n", NAMESPACE, "-l", k8s.RUN_LABEL, "-o", "name")
    assert left.stdout.strip() == "", left.stdout


def test_cluster_enforces_network_policy():
    executor = k8s.KubernetesExecutor(namespace=NAMESPACE, image=IMAGE, context=CONTEXT, deadline=180)
    assert executor.network_isolated(), "this cluster's CNI does not enforce NetworkPolicy"
