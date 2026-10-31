"""Integration tests for deploy/k8s: least-privilege RBAC and Pod Security on a real cluster.

The module applies `kubectl apply -k deploy/k8s` (namespace `claimcheck`), then runs the executor
as the `claimcheck-executor` ServiceAccount through `kubectl --as`, and checks what that identity
may and may not do with `kubectl auth can-i --as`. The kubectl context must be allowed to
impersonate (kind's admin is).

Run with CLAIMCHECK_K8S_CONTEXT=<kubectl context> (CI uses a kind cluster). Skipped otherwise.
"""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from claimcheck import k8s
from claimcheck.models import Status

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
NAMESPACE = "claimcheck"
SERVICE_ACCOUNT = f"system:serviceaccount:{NAMESPACE}:claimcheck-executor"
IMAGE = os.environ.get("CLAIMCHECK_K8S_IMAGE", "python:3.12-slim")
DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "k8s"

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def kubectl(*args, input=None, as_user=None):
    command = ["kubectl", "--context", CONTEXT, *args]
    if as_user:
        command += ["--as", as_user]
    return subprocess.run(command, input=input, capture_output=True, text=True, timeout=120)


def can_i(verb, resource, namespace=NAMESPACE, subresource=None) -> bool:
    args = ["auth", "can-i", verb, resource]
    args += ["-n", namespace] if namespace else ["--all-namespaces"]
    if subresource:
        args += ["--subresource", subresource]
    done = kubectl(*args, as_user=SERVICE_ACCOUNT)
    answer = done.stdout.strip()
    assert answer in ("yes", "no"), done.stderr
    return answer == "yes"


@pytest.fixture(scope="module", autouse=True)
def deployed():
    done = kubectl("apply", "-k", str(DEPLOY))
    assert done.returncode == 0, done.stderr
    yield


def executor(namespace=NAMESPACE, **kw):
    return k8s.KubernetesExecutor(namespace=namespace, image=IMAGE, context=CONTEXT, deadline=180,
                                  impersonate=SERVICE_ACCOUNT, **kw)


# --- the executor works as the ServiceAccount -----------------------------------------------


def test_full_run_as_the_service_account(tmp_path):
    (tmp_path / "poc.py").write_text("raise RuntimeError('buffer overflow in parse_url')\n")
    evidence = executor().run_repro(tmp_path, "python3 poc.py", "buffer overflow in parse_url", timeout=90)
    assert "egress blocked, verified" in evidence.source, evidence.detail  # the probe ran too
    assert evidence.status is Status.CONFIRMED, evidence.detail
    assert evidence.source.startswith(f"kubernetes[{NAMESPACE}/")
    left = kubectl("get", "jobs,configmaps,networkpolicies", "-n", NAMESPACE, "-l", k8s.RUN_LABEL, "-o", "name")
    assert left.stdout.strip() == "", left.stdout


def test_service_account_cannot_run_in_default(tmp_path):
    (tmp_path / "poc.py").write_text("raise SystemExit(1)\n")
    evidence = executor(namespace="default", require_network_isolation=False).run_repro(tmp_path, "python3 poc.py", "x")
    assert evidence.status is Status.UNVERIFIED
    assert "forbidden" in evidence.detail, evidence.detail


# --- what the ServiceAccount may and may not do ---------------------------------------------


def test_live_role_has_no_wildcards_secrets_or_exec():
    """The same static checks as the unit tests, on the Role the cluster really has (no PyYAML needed)."""
    role = json.loads(kubectl("get", "role", "claimcheck-executor", "-n", NAMESPACE, "-o", "json").stdout)
    granted = {(g, r): set(rule["verbs"]) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"]}
    assert not [key for key, verbs in granted.items() if "*" in key or "*" in verbs]
    assert not set().union(*granted.values()) & {"patch", "update", "deletecollection", "escalate", "bind", "impersonate"}
    assert not {r for _, r in granted} & {"secrets", "pods/exec", "pods/attach", "pods/portforward", "serviceaccounts/token"}
    assert "create" not in granted.get(("", "pods"), set())
    binding = json.loads(kubectl("get", "rolebinding", "claimcheck-executor", "-n", NAMESPACE, "-o", "json").stdout)
    assert binding["roleRef"]["kind"] == "Role"
    assert [(s["kind"], s["name"]) for s in binding["subjects"]] == [("ServiceAccount", "claimcheck-executor")]
    sa = json.loads(kubectl("get", "serviceaccount", "claimcheck-executor", "-n", NAMESPACE, "-o", "json").stdout)
    assert sa["automountServiceAccountToken"] is False


@pytest.mark.parametrize("resource", ["configmaps", "networkpolicies.networking.k8s.io", "jobs.batch"])
@pytest.mark.parametrize("verb", ["get", "create", "list", "delete", "watch"])
def test_service_account_can_do_what_the_executor_needs(verb, resource):
    assert can_i(verb, resource)


def test_service_account_can_list_pods():
    assert can_i("list", "pods")


def test_service_account_can_read_pod_logs():
    assert can_i("get", "pods", subresource="log")


@pytest.mark.parametrize("verb", ["get", "list", "watch", "create"])
def test_service_account_cannot_touch_secrets(verb):
    assert not can_i(verb, "secrets")


@pytest.mark.parametrize("verb, resource, subresource", [
    ("create", "pods", None), ("delete", "pods", None), ("get", "pods", None), ("update", "jobs.batch", None),
    ("patch", "jobs.batch", None), ("patch", "networkpolicies.networking.k8s.io", None), ("patch", "configmaps", None),
    ("create", "pods", "exec"), ("create", "pods", "attach"), ("create", "pods", "portforward"),
    ("create", "serviceaccounts", "token"), ("create", "roles.rbac.authorization.k8s.io", None),
    ("create", "rolebindings.rbac.authorization.k8s.io", None), ("deletecollection", "configmaps", None),
])
def test_service_account_cannot_exec_or_escalate(verb, resource, subresource):
    assert not can_i(verb, resource, subresource=subresource)


@pytest.mark.parametrize("namespace", ["default", "kube-system", "claimcheck-it"])
@pytest.mark.parametrize("verb, resource", [
    ("create", "pods"), ("create", "jobs.batch"), ("list", "pods"), ("get", "configmaps"),
    ("delete", "jobs.batch"), ("create", "networkpolicies.networking.k8s.io"), ("get", "secrets"),
])
def test_service_account_cannot_touch_other_namespaces(namespace, verb, resource):
    assert not can_i(verb, resource, namespace=namespace)


@pytest.mark.parametrize("verb, resource", [
    ("list", "namespaces"), ("create", "namespaces"), ("list", "nodes"),
    ("create", "clusterrolebindings.rbac.authorization.k8s.io"), ("list", "pods"),
])
def test_service_account_has_no_cluster_wide_access(verb, resource):
    assert not can_i(verb, resource, namespace=None)


# --- Pod Security admission (restricted) ------------------------------------------------------


def test_namespace_is_labelled_restricted():
    labels = json.loads(kubectl("get", "namespace", NAMESPACE, "-o", "json").stdout)["metadata"]["labels"]
    for mode in ("enforce", "audit", "warn"):
        assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"


def pod(name, **container):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name},
            "spec": {"containers": [{"name": "c", "image": IMAGE, "command": ["true"], **container}]}}


@pytest.mark.parametrize("manifest", [
    pod("claimcheck-privileged", securityContext={"privileged": True}),  # fails even "baseline"
    pod("claimcheck-defaults"),  # fine under "baseline", but runs as root with capabilities
], ids=["privileged", "root-defaults"])
def test_non_restricted_pod_is_rejected_by_admission(manifest):
    # Applied as the admin, so RBAC allows it and only Pod Security can reject it.
    done = kubectl("apply", "--dry-run=server", "-n", NAMESPACE, "-f", "-", input=json.dumps(manifest))
    assert done.returncode != 0, done.stdout
    assert "violates PodSecurity" in done.stderr and "restricted" in done.stderr, done.stderr


def test_executor_job_is_accepted_under_restricted():
    job = k8s.build_job("claimcheck-admission-check", IMAGE, "true", timeout=30)
    done = kubectl("apply", "--dry-run=server", "-n", NAMESPACE, "-f", "-", input=json.dumps(job), as_user=SERVICE_ACCOUNT)
    assert done.returncode == 0, done.stderr
    assert "PodSecurity" not in done.stderr, done.stderr  # warn=restricted would warn about the template
    # A Job's template is only warned about; the Pod is what enforce checks. Check that directly too.
    template = job["spec"]["template"]
    as_pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "claimcheck-admission-check"},
              "spec": template["spec"]}
    done = kubectl("apply", "--dry-run=server", "-n", NAMESPACE, "-f", "-", input=json.dumps(as_pod))
    assert done.returncode == 0, done.stderr


def test_privileged_job_from_the_service_account_never_gets_a_pod():
    """The ServiceAccount may create Jobs, but Pod Security still stops a privileged pod."""
    name = f"claimcheck-priv-{uuid.uuid4().hex[:6]}"
    job = k8s.build_job(name, IMAGE, "true", timeout=30)
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["securityContext"] = {"privileged": True}
    try:
        done = kubectl("apply", "-n", NAMESPACE, "-f", "-", input=json.dumps(job), as_user=SERVICE_ACCOUNT)
        assert done.returncode == 0, done.stderr
        assert "would violate PodSecurity" in done.stderr, done.stderr  # warn mode
        message, end = "", time.monotonic() + 60
        while "violates PodSecurity" not in message and time.monotonic() < end:
            events = kubectl("get", "events", "-n", NAMESPACE, "-o", "json",
                             "--field-selector", f"involvedObject.name={name},reason=FailedCreate")
            message = " ".join(e.get("message", "") for e in json.loads(events.stdout or "{}").get("items", []))
            time.sleep(1)
        assert "violates PodSecurity" in message, message
        pods = kubectl("get", "pods", "-n", NAMESPACE, "-l", f"{k8s.RUN_LABEL}={name}", "-o", "name")
        assert pods.stdout.strip() == ""
    finally:
        kubectl("delete", "job", name, "-n", NAMESPACE, "--ignore-not-found", "--wait=true")
