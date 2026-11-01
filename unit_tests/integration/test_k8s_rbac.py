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
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from claimcheck import k8s
from claimcheck.models import Status

from ._runs import RecordingRun, left_behind

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


def full_run(repo, recorder):
    (repo / "poc.py").write_text("raise RuntimeError('buffer overflow in parse_url')\n")
    evidence = executor(run=recorder).run_repro(repo, "python3 poc.py", "buffer overflow in parse_url", timeout=90)
    assert "egress blocked, verified" in evidence.source, evidence.detail  # the probe ran too
    assert evidence.status is Status.CONFIRMED, evidence.detail
    assert evidence.source.startswith(f"kubernetes[{NAMESPACE}/")
    assert len(recorder.names) == 2, recorder.names  # the network probe, then the repro
    return evidence


def test_full_run_as_the_service_account(tmp_path):
    # Only this run's objects: the namespace is shared, other runs and leftovers may be there (#63).
    recorder = RecordingRun()
    full_run(tmp_path, recorder)
    assert left_behind(kubectl, NAMESPACE, recorder.names) == []


def test_two_concurrent_runs_both_pass_and_both_clean_up(tmp_path):
    recorders, errors = [RecordingRun(), RecordingRun()], []

    def one(i):
        try:
            repo = tmp_path / f"run{i}"
            repo.mkdir()
            full_run(repo, recorders[i])
        except BaseException as error:  # noqa: BLE001 - re-raised below, in the test's thread
            errors.append(error)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=600)
    assert not any(thread.is_alive() for thread in threads), "a run did not finish"
    if errors:
        raise errors[0]
    names = recorders[0].names + recorders[1].names
    assert len(set(names)) == 4, names  # distinct runs, nothing shared
    assert left_behind(kubectl, NAMESPACE, names) == []


def test_service_account_cannot_run_in_default(tmp_path):
    (tmp_path / "poc.py").write_text("raise SystemExit(1)\n")
    evidence = executor(namespace="default", require_network_isolation=False).run_repro(tmp_path, "python3 poc.py", "x")
    assert evidence.status is Status.UNVERIFIED
    assert "forbidden" in evidence.detail, evidence.detail


# --- what the ServiceAccount may and may not do ---------------------------------------------


def test_live_role_has_no_wildcards_secrets_or_exec():
    """The same static checks as the unit tests, on the Role the cluster really has (no PyYAML needed)."""
    role = json.loads(kubectl("get", "role", "claimcheck-executor", "-n", NAMESPACE, "-o", "json").stdout)
    assert role["metadata"]["namespace"] == NAMESPACE
    granted = {}
    for rule in role["rules"]:
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                granted.setdefault((group, resource), set()).update(rule["verbs"])
    assert granted.get(("", "events")) == {"get", "list"}
    assert {group for group, resource in granted if resource == "events"} == {""}
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


@pytest.mark.parametrize("verb", ["get", "list"])
def test_service_account_can_read_events(verb: str) -> None:
    assert can_i(verb, "events")


@pytest.mark.parametrize("verb", ["watch", "create", "update", "patch", "delete", "deletecollection"])
def test_service_account_cannot_watch_or_write_events(verb: str) -> None:
    assert not can_i(verb, "events")


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
    ("get", "events"), ("list", "events"),
])
def test_service_account_cannot_touch_other_namespaces(namespace, verb, resource):
    assert not can_i(verb, resource, namespace=namespace)


@pytest.mark.parametrize("verb, resource", [
    ("list", "namespaces"), ("create", "namespaces"), ("list", "nodes"),
    ("create", "clusterrolebindings.rbac.authorization.k8s.io"), ("list", "pods"),
    ("get", "events"), ("list", "events"),
])
def test_service_account_has_no_cluster_wide_access(verb, resource):
    assert not can_i(verb, resource, namespace=None)


@pytest.fixture
def quota_namespace() -> Iterator[str]:
    """Copy the shipped Role into an isolated namespace so no shared run loses quota."""
    name = f"claimcheck-rbac-quota-{uuid.uuid4().hex[:10]}"
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
    done = kubectl("create", "-f", "-", input=json.dumps(namespace))
    assert done.returncode == 0, done.stderr
    try:
        for filename in ("role.yaml", "rolebinding.yaml"):
            done = kubectl("create", "--dry-run=client", "-o", "json", "-f", str(DEPLOY / filename))
            assert done.returncode == 0, done.stderr
            manifest = json.loads(done.stdout)
            manifest["metadata"]["namespace"] = name
            done = kubectl("create", "-f", "-", input=json.dumps(manifest))
            assert done.returncode == 0, done.stderr
        quota = {"apiVersion": "v1", "kind": "ResourceQuota",
                 "metadata": {"name": "no-pods", "namespace": name}, "spec": {"hard": {"count/pods": "0"}}}
        done = kubectl("create", "-f", "-", input=json.dumps(quota))
        assert done.returncode == 0, done.stderr
        end = time.monotonic() + 30
        while True:
            done = kubectl("get", "resourcequota", "no-pods", "-n", name, "-o", "json")
            assert done.returncode == 0, done.stderr
            if "count/pods" in json.loads(done.stdout).get("status", {}).get("used", {}):
                break
            assert time.monotonic() < end, "quota controller did not initialize usage"
            time.sleep(0.2)
        yield name
    finally:
        done = kubectl("delete", "namespace", name, "--wait=false")
        assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("probe", [False, True], ids=["repro", "network-probe"])
def test_service_account_names_quota_denial_before_deadline(quota_namespace: str, tmp_path: Path,
                                                          probe: bool) -> None:
    """The Job succeeds at apply; its FailedCreate event must explain the missing pod."""
    events = []

    def recording(command: list[str], input: str | None = None, **kw) -> subprocess.CompletedProcess:
        done = subprocess.run(command, input=input, **kw)
        if command[1:3] == ["get", "events"]:
            assert command[command.index("--as") + 1] == SERVICE_ACCOUNT
            events.append(done)
        return done

    recorder = RecordingRun(run=recording)
    (tmp_path / "poc.py").write_text("raise RuntimeError('boom')\n")
    runner = k8s.KubernetesExecutor(namespace=quota_namespace, image=IMAGE, context=CONTEXT,
                                    impersonate=SERVICE_ACCOUNT, deadline=20, poll_interval=0.2,
                                    require_network_isolation=probe, run=recorder)
    start = time.monotonic()
    evidence = runner.run_repro(tmp_path, "python3 poc.py", "boom", timeout=60)
    elapsed = time.monotonic() - start

    assert evidence.status is Status.UNVERIFIED, evidence.detail
    assert "ResourceQuota setup failure" in evidence.detail, evidence.detail
    assert "no-pods" in evidence.detail and "count/pods" in evidence.detail, evidence.detail
    assert "did not finish" not in evidence.detail and "NetworkPolicy" not in evidence.detail
    assert elapsed < 15, f"waited {elapsed:.1f}s for a 20s deadline: {evidence.detail}"
    assert len(recorder.names) == 1, recorder.names
    assert events, "the executor did not try to read Job events"
    for done in events:
        assert done.returncode == 0, done.stderr
    messages = [event.get("message", "") for done in events for event in json.loads(done.stdout).get("items", [])]
    assert any("exceeded quota: no-pods" in message and "count/pods" in message for message in messages), messages
    if probe:
        assert "network probe" in evidence.detail and "egress not verified" in evidence.source
    assert left_behind(kubectl, quota_namespace, recorder.names) == []
    done = kubectl("get", "pods", "-n", quota_namespace, "-l", f"{k8s.RUN_LABEL}={recorder.names[0]}", "-o", "name")
    assert done.returncode == 0, done.stderr
    assert not done.stdout.strip(), done.stdout


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
