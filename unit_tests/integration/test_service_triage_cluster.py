"""Integration test for deploy/triage: a real API server admits the overlay's pod (with the
`checkout` init container) under Pod Security `restricted`.

Uses its own throwaway namespace with the `restricted` labels, never `claimcheck-service`, so it
is safe to run against a cluster where the service is deployed. The pod is submitted with
--dry-run=server, so nothing starts and the image need not be in the cluster.

Run with CLAIMCHECK_K8S_CONTEXT=<kubectl context>. Skipped otherwise.
"""

import os
import subprocess
import uuid
from pathlib import Path

import pytest

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
OVERLAY = Path(__file__).resolve().parents[2] / "deploy" / "triage"
NAMESPACE = f"claimcheck-triage-test-{uuid.uuid4().hex[:8]}"

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def kubectl(*args, stdin=None):
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], input=stdin, capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def yaml():
    return pytest.importorskip("yaml", reason="PyYAML not installed")


@pytest.fixture(scope="module", autouse=True)
def namespace(yaml):
    labels = {f"pod-security.kubernetes.io/{mode}": "restricted" for mode in ("enforce", "audit", "warn")}
    created = kubectl("apply", "-f", "-", stdin=yaml.safe_dump(
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": labels}}))
    assert created.returncode == 0, created.stderr
    yield
    kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=false")


def pod(yaml, mutate=None) -> str:
    rendered = kubectl("kustomize", str(OVERLAY))
    assert rendered.returncode == 0, rendered.stderr
    deployment = next(d for d in yaml.safe_load_all(rendered.stdout) if d and d["kind"] == "Deployment")
    spec = deployment["spec"]["template"]["spec"]
    assert [c["name"] for c in spec["initContainers"]] == ["checkout"]
    if mutate:
        mutate(spec)
    return yaml.safe_dump({"apiVersion": "v1", "kind": "Pod",
                           "metadata": {"name": "claimcheck-triage-dry-run", "namespace": NAMESPACE}, "spec": spec})


def test_restricted_admits_the_pod_with_the_checkout_init_container(yaml):
    done = kubectl("apply", "--dry-run=server", "-f", "-", stdin=pod(yaml))
    assert done.returncode == 0, done.stderr
    assert "violate" not in done.stderr.lower(), done.stderr


def test_restricted_would_refuse_a_loosened_checkout_container(yaml):
    """Proves the check above is real: the same pod with the init container loosened is refused."""
    def loosen(spec):
        spec["initContainers"][0]["securityContext"]["allowPrivilegeEscalation"] = True
    done = kubectl("apply", "--dry-run=server", "-f", "-", stdin=pod(yaml, loosen))
    assert done.returncode != 0
    assert "allowPrivilegeEscalation" in done.stderr
