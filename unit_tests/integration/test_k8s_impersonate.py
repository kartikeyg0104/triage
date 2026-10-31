"""Integration tests for `claimcheck check --k8s-impersonate` and RBAC-denial reporting.

Applies `kubectl apply -k deploy/k8s`, then runs the CLI as the restricted `claimcheck-executor`
ServiceAccount and as under-privileged identities: `system:serviceaccount:claimcheck:default`
(no permissions at all) and a made-up user bound to a copy of the executor Role with one
permission removed. A denial must come back as an UNVERIFIED RBAC setup failure that names the
missing permission, never as a NetworkPolicy problem or a verdict. The kubectl context must be
allowed to impersonate (kind's admin is).

Run with CLAIMCHECK_K8S_CONTEXT=<kubectl context> (CI uses a kind cluster). Skipped otherwise.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from claimcheck import cli, k8s
from claimcheck.models import Status

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
NAMESPACE = "claimcheck"
EXECUTOR_SA = f"system:serviceaccount:{NAMESPACE}:claimcheck-executor"
DEFAULT_SA = f"system:serviceaccount:{NAMESPACE}:default"
IMAGE = os.environ.get("CLAIMCHECK_K8S_IMAGE", "python:3.12-slim")
DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "k8s"
EXPECT = "buffer overflow in parse_url"

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def kubectl(*args, input=None):
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], input=input, capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module", autouse=True)
def deployed():
    done = kubectl("apply", "-k", str(DEPLOY))
    assert done.returncode == 0, done.stderr


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "urls.py").write_text("def parse_url(s):\n    return s\n")
    (root / "poc.py").write_text(f"raise RuntimeError('{EXPECT}')\n")
    (tmp_path / "report.txt").write_text("crash in `parse_url()` on long input")
    return root


@pytest.fixture
def run_names(monkeypatch):
    """Names of the runs this test's executor applied, read from the real kubectl calls."""
    names, real = [], k8s.KubernetesExecutor

    def recording(command, input=None, **kw):
        if command[1] == "apply" and input:
            names.append(json.loads(input)["items"][0]["metadata"]["name"])
        return subprocess.run(command, input=input, **kw)

    monkeypatch.setattr(k8s, "KubernetesExecutor", lambda **kw: real(**kw, run=recording))
    return names


def left_behind(names) -> list[str]:
    """Objects of these runs that still exist (other agents may share the cluster, so filter by run)."""
    found = []
    for name in names:
        done = kubectl("get", "jobs,configmaps,networkpolicies", "-n", NAMESPACE, "-l", f"{k8s.RUN_LABEL}={name}", "-o", "name")
        found += done.stdout.split()
    return found


def check(repo, capsys, identity):
    code = cli.main(["check", "--repo", str(repo), "--report", str(repo.parent / "report.txt"), "--no-model", "--json",
                     "--repro", "python3 poc.py", "--expect", EXPECT, "--executor", "kubernetes",
                     "--k8s-namespace", NAMESPACE, "--k8s-image", IMAGE, "--kube-context", CONTEXT,
                     "--k8s-impersonate", identity])
    out = json.loads(capsys.readouterr().out)
    [repro] = [e for e in out["evidence"] if e["kind"] == "repro"]
    return code, out, repro


def test_cli_as_the_executor_service_account_confirms(repo, capsys, run_names):
    code, out, repro = check(repo, capsys, EXECUTOR_SA)
    assert repro["status"] == "confirmed", repro
    assert "egress blocked, verified" in repro["source"], repro
    assert code == 0 and out["verdict"] == "supported"
    assert len(run_names) == 2 and left_behind(run_names) == []  # probe + repro, both cleaned up


def test_cli_as_an_identity_with_no_permissions_is_an_rbac_failure(repo, capsys):
    code, out, repro = check(repo, capsys, DEFAULT_SA)
    assert code == 2 and out["verdict"] == "unverified"
    assert repro["status"] == "unverified"
    detail = repro["detail"]
    assert "RBAC setup failure" in detail and "network probe" in detail, detail
    assert f'User "{DEFAULT_SA}" cannot get configmaps' in detail, detail
    assert 'namespace "claimcheck"' in detail
    assert "NetworkPolicy" not in detail
    assert "egress not verified" in repro["source"]


def test_repro_stage_denial_is_named_as_rbac(repo):
    """Without the probe, the first denial comes from the repro's own apply."""
    executor = k8s.KubernetesExecutor(namespace=NAMESPACE, image=IMAGE, context=CONTEXT, deadline=180,
                                      impersonate=DEFAULT_SA, require_network_isolation=False)
    evidence = executor.run_repro(repo, "python3 poc.py", EXPECT, timeout=90)
    assert evidence.status is Status.UNVERIFIED
    assert "RBAC setup failure" in evidence.detail and "repro" in evidence.detail, evidence.detail
    assert "cannot get configmaps" in evidence.detail


@pytest.fixture
def user_without(request):
    """A made-up user bound to the executor's Role minus one (resource, verb); removed afterwards."""
    resource, verb = request.param
    name = f"claimcheck-it-{uuid.uuid4().hex[:8]}"
    role = json.loads(kubectl("get", "role", "claimcheck-executor", "-n", NAMESPACE, "-o", "json").stdout)
    rules = []
    for rule in role["rules"]:
        if resource in rule["resources"]:
            rule = {**rule, "verbs": [v for v in rule["verbs"] if v != verb]}
        if rule["verbs"]:
            rules.append(rule)
    objects = {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": name}, "rules": rules},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": name},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
         "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": name}]},
    ]}
    done = kubectl("apply", "-n", NAMESPACE, "-f", "-", input=json.dumps(objects))
    assert done.returncode == 0, done.stderr
    try:
        yield name
    finally:
        kubectl("delete", "-n", NAMESPACE, "role,rolebinding", name, "--ignore-not-found")


@pytest.mark.parametrize("user_without, named", [
    (("pods/log", "get"), "cannot get pods/log"),  # the probe pod runs, then its output cannot be read
    (("pods", "list"), "cannot list pods"),  # the probe is created, then its state cannot be read
], indirect=["user_without"], ids=["no-pod-logs", "no-pod-list"])
def test_partially_privileged_identity_names_the_missing_permission(repo, capsys, run_names, user_without, named):
    code, out, repro = check(repo, capsys, user_without)
    assert code == 2 and repro["status"] == "unverified"
    detail = repro["detail"]
    assert "RBAC setup failure" in detail and "network probe" in detail, detail
    assert f'User "{user_without}" {named}' in detail, detail
    assert "NetworkPolicy" not in detail and "did not finish" not in detail
    assert len(run_names) == 1  # only the probe; the repro never ran
    assert left_behind(run_names) == []  # delete is still allowed, so nothing is left behind
