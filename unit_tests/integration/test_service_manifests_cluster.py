"""Integration test for deploy/service: a real API server accepts it under Pod Security `restricted`.

Creates only the Namespace (with its `restricted` labels) for real, then server-side dry-runs the
whole kustomization, so the Pod Security admission check runs against the Deployment's pod
template without any pod being started (the image need not be in the cluster). The namespace is
deleted at the end.

Run with CLAIMCHECK_K8S_CONTEXT=<kubectl context> (CI uses a kind cluster). Skipped otherwise.
"""

import os
import subprocess
from pathlib import Path

import pytest

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
NAMESPACE = "claimcheck-service"
DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "service"

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def kubectl(*args):
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module", autouse=True)
def namespace():
    yaml = pytest.importorskip("yaml", reason="PyYAML not installed")
    rendered = kubectl("kustomize", str(DEPLOY))
    assert rendered.returncode == 0, rendered.stderr
    docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
    namespaces = [d for d in docs if d["kind"] == "Namespace"]
    assert [d["metadata"]["name"] for d in namespaces] == [NAMESPACE]
    created = subprocess.run(["kubectl", "--context", CONTEXT, "apply", "-f", "-"], input=yaml.safe_dump(namespaces[0]),
                             capture_output=True, text=True, timeout=120)
    assert created.returncode == 0, created.stderr
    yield
    kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--timeout=90s")  # waits, so a rerun never hits a Terminating namespace


def test_namespace_is_restricted():
    done = kubectl("get", "namespace", NAMESPACE, "-o",
                   "jsonpath={.metadata.labels.pod-security\\.kubernetes\\.io/enforce}")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "restricted"


def test_server_dry_run_accepts_everything_under_restricted():
    done = kubectl("apply", "--dry-run=server", "-k", str(DEPLOY))
    assert done.returncode == 0, done.stderr
    assert "violate" not in done.stderr.lower(), done.stderr  # a PodSecurity warning would land here
    assert "deployment.apps/claimcheck-api" in done.stdout


def pod_from_template(yaml, privileged=False) -> str:
    """`enforce` applies to Pods; for a Deployment the API server only warns. So the pod template
    is also submitted as a Pod, which is what the ReplicaSet controller will create."""
    rendered = kubectl("kustomize", str(DEPLOY))
    assert rendered.returncode == 0, rendered.stderr
    deployment = next(d for d in yaml.safe_load_all(rendered.stdout) if d and d["kind"] == "Deployment")
    template = deployment["spec"]["template"]
    if privileged:
        template["spec"]["containers"][0]["securityContext"]["allowPrivilegeEscalation"] = True
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": "claimcheck-api-dry-run", "namespace": NAMESPACE, "labels": template["metadata"]["labels"]},
           "spec": template["spec"]}
    return yaml.safe_dump(pod)


def apply_dry_run(manifest: str):
    return subprocess.run(["kubectl", "--context", CONTEXT, "apply", "--dry-run=server", "-f", "-"], input=manifest,
                          capture_output=True, text=True, timeout=120)


def test_enforce_admits_the_pod_template():
    yaml = pytest.importorskip("yaml")
    done = apply_dry_run(pod_from_template(yaml))
    assert done.returncode == 0, done.stderr
    assert "violate" not in done.stderr.lower(), done.stderr


def test_enforce_rejects_a_privileged_pod():
    """The namespace really enforces: the same pod with privilege escalation allowed is refused."""
    yaml = pytest.importorskip("yaml")
    done = apply_dry_run(pod_from_template(yaml, privileged=True))
    assert done.returncode != 0
    assert "allowPrivilegeEscalation" in done.stderr, done.stderr


def test_server_dry_run_warns_on_a_privileged_deployment(tmp_path):
    """And the Deployment variant is flagged by `warn`, so the clean run above is meaningful."""
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "kustomization.yaml").write_text(
        f"resources:\n  - {os.path.relpath(DEPLOY, overlay.resolve())}\n"  # kustomize refuses absolute bases
        "patches:\n"
        "  - target: {kind: Deployment, name: claimcheck-api}\n"
        "    patch: |\n"
        "      - op: replace\n"
        "        path: /spec/template/spec/containers/0/securityContext/allowPrivilegeEscalation\n"
        "        value: true\n")
    done = kubectl("apply", "--dry-run=server", "-k", str(overlay))
    assert "allowPrivilegeEscalation" in done.stderr, (done.stdout, done.stderr)
