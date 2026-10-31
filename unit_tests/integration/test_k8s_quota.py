"""Integration tests for the ResourceQuota and LimitRange in deploy/k8s, on a real cluster.

The module applies `kubectl apply -k deploy/k8s` (namespace `claimcheck`), runs the executor as the
`claimcheck-executor` ServiceAccount under the quota (once, and four runs at the same time), and
checks with server-side dry-run that the API server rejects pods above the LimitRange max or the
quota. To fill the object-count quotas for real it copies the same ResourceQuota and LimitRange into
a temporary namespace, which it deletes afterwards; nothing is filled in `claimcheck` itself, which
other runs may be using.

Run with CLAIMCHECK_K8S_CONTEXT=<kubectl context> (CI uses a kind cluster). Skipped otherwise.
"""

import copy
import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from claimcheck import k8s
from claimcheck.models import Status

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
NAMESPACE = "claimcheck"
SERVICE_ACCOUNT = f"system:serviceaccount:{NAMESPACE}:claimcheck-executor"
IMAGE = os.environ.get("CLAIMCHECK_K8S_IMAGE", "python:3.12-slim")
DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "k8s"
QUOTA_KEYS = {"count/pods", "count/jobs.batch", "configmaps", "requests.cpu", "requests.memory",
              "limits.cpu", "limits.memory"}

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def kubectl(*args, input=None, as_user=None):
    command = ["kubectl", "--context", CONTEXT, *args]
    if as_user:
        command += ["--as", as_user]
    return subprocess.run(command, input=input, capture_output=True, text=True, timeout=120)


def can_i(verb, resource, namespace=NAMESPACE) -> bool:
    done = kubectl("auth", "can-i", verb, resource, "-n", namespace, as_user=SERVICE_ACCOUNT)
    answer = done.stdout.strip()
    assert answer in ("yes", "no"), done.stderr
    return answer == "yes"


def load(path: Path) -> dict:
    """A manifest file as JSON, parsed by kubectl so the test needs no PyYAML."""
    done = kubectl("create", "--dry-run=client", "-o", "json", "-f", str(path))
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture(scope="module", autouse=True)
def deployed():
    done = kubectl("apply", "-k", str(DEPLOY))
    assert done.returncode == 0, done.stderr
    yield


def live(kind) -> list:
    done = kubectl("get", kind, "-n", NAMESPACE, "-o", "json")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["items"]


def executor(namespace=NAMESPACE, **kw):
    return k8s.KubernetesExecutor(namespace=namespace, image=IMAGE, context=CONTEXT, deadline=180,
                                  impersonate=SERVICE_ACCOUNT, **kw)


def executor_pod(name: str) -> dict:
    """The executor's Job template as a bare Pod, as the Job controller would create it."""
    spec = k8s.build_job(name, IMAGE, "true", timeout=30)["spec"]["template"]["spec"]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name}, "spec": spec}


def dry_run(manifest: dict, namespace=NAMESPACE) -> subprocess.CompletedProcess:
    # As the admin, so RBAC allows it and only admission (LimitRanger, ResourceQuota) can refuse it.
    return kubectl("create", "--dry-run=server", "-n", namespace, "-o", "json", "-f", "-", input=json.dumps(manifest))


# --- the objects are installed ------------------------------------------------------------------


def test_live_quota_caps_what_the_issue_names():
    quotas = live("resourcequotas")
    assert len(quotas) == 1, [q["metadata"]["name"] for q in quotas]
    assert QUOTA_KEYS <= set(quotas[0]["spec"]["hard"])


def test_live_limit_range_has_container_default_default_request_and_max():
    ranges = live("limitranges")
    assert len(ranges) == 1, [r["metadata"]["name"] for r in ranges]
    (container,) = [item for item in ranges[0]["spec"]["limits"] if item["type"] == "Container"]
    for field in ("default", "defaultRequest", "max"):
        assert {"cpu", "memory"} <= set(container[field]), field


# --- (a) the executor still works, also several runs at once (c) as the ServiceAccount -----------


def test_full_run_as_the_service_account_under_the_quota(tmp_path):
    (tmp_path / "poc.py").write_text("raise RuntimeError('buffer overflow in parse_url')\n")
    evidence = executor().run_repro(tmp_path, "python3 poc.py", "buffer overflow in parse_url", timeout=90)
    assert "egress blocked, verified" in evidence.source, evidence.detail  # the probe pod ran too
    assert evidence.status is Status.CONFIRMED, evidence.detail


def test_four_concurrent_runs_fit_in_the_quota(tmp_path):
    def run(i):
        repo = tmp_path / f"repo{i}"
        repo.mkdir()
        (repo / "poc.py").write_text(f"raise RuntimeError('crash number {i}')\n")
        return executor().run_repro(repo, "python3 poc.py", f"crash number {i}", timeout=90)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    for evidence in results:
        assert evidence.status is Status.CONFIRMED, evidence.detail


def test_executor_pod_is_admitted_unchanged():
    done = dry_run(executor_pod("claimcheck-quota-fit"))
    assert done.returncode == 0, done.stderr
    resources = json.loads(done.stdout)["spec"]["containers"][0]["resources"]
    assert resources == k8s.build_job("x", IMAGE, "true", 30)["spec"]["template"]["spec"]["containers"][0]["resources"]


def test_pod_without_resources_gets_the_limit_range_defaults():
    pod = executor_pod("claimcheck-quota-defaults")
    del pod["spec"]["containers"][0]["resources"]
    done = dry_run(pod)
    assert done.returncode == 0, done.stderr
    resources = json.loads(done.stdout)["spec"]["containers"][0]["resources"]
    assert resources == {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}


@pytest.mark.parametrize("service_account_may_not", ["create", "update", "patch", "delete"])
@pytest.mark.parametrize("resource", ["resourcequotas", "limitranges"])
def test_service_account_cannot_lift_its_own_caps(service_account_may_not, resource):
    assert not can_i(service_account_may_not, resource)


# --- (b) the API server rejects what does not fit ----------------------------------------------


@pytest.mark.parametrize("resource, amount", [("cpu", "2"), ("memory", "1Gi")])
def test_container_above_the_limit_range_max_is_rejected(resource, amount):
    pod = executor_pod("claimcheck-quota-max")
    resources = pod["spec"]["containers"][0]["resources"]
    resources["limits"][resource] = amount
    done = dry_run(pod)
    assert done.returncode != 0, done.stdout
    assert f"maximum {resource} usage per Container" in done.stderr, done.stderr


def test_pod_above_the_cpu_quota_is_rejected():
    """Each container is within the LimitRange max, but together they request more CPU than the quota."""
    pod = executor_pod("claimcheck-quota-cpu")
    first = pod["spec"]["containers"][0]
    first["resources"] = {"requests": {"cpu": "1", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}}
    second = copy.deepcopy(first)
    second["name"] = "second"
    pod["spec"]["containers"].append(second)
    done = dry_run(pod)
    assert done.returncode != 0, done.stdout
    assert "exceeded quota" in done.stderr and "requests.cpu" in done.stderr, done.stderr


@pytest.fixture
def scratch():
    """A temporary namespace with the same ResourceQuota and LimitRange as `claimcheck`."""
    name = f"claimcheck-quota-{uuid.uuid4().hex[:6]}"
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
    assert kubectl("create", "-f", "-", input=json.dumps(namespace)).returncode == 0
    try:
        quota, limits = load(DEPLOY / "quota.yaml"), load(DEPLOY / "limitrange.yaml")
        for manifest in (quota, limits):
            manifest["metadata"]["namespace"] = name
            done = kubectl("create", "-f", "-", input=json.dumps(manifest))
            assert done.returncode == 0, done.stderr
        end = time.monotonic() + 60  # admission refuses everything until the quota controller has counted
        while time.monotonic() < end:
            status = json.loads(kubectl("get", "resourcequota", quota["metadata"]["name"], "-n", name, "-o", "json").stdout)
            if set(status.get("status", {}).get("used", {})) >= QUOTA_KEYS:
                break
            time.sleep(1)
        yield name, {key: value for key, value in quota["spec"]["hard"].items()}
    finally:
        kubectl("delete", "namespace", name, "--wait=false")


def suspended_job(name):
    job = k8s.build_job(name, IMAGE, "true", timeout=30)
    job["spec"]["suspend"] = True  # counts as a Job, but no pod is ever created
    return job


def pending_pod(name):
    pod = executor_pod(name)
    pod["spec"]["schedulerName"] = "claimcheck-no-such-scheduler"  # counts as a pod, never scheduled
    return pod


@pytest.mark.parametrize("key, make", [("count/jobs.batch", suspended_job), ("count/pods", pending_pod)],
                         ids=["jobs", "pods"])
def test_one_object_beyond_the_count_quota_is_rejected(scratch, key, make):
    namespace, hard = scratch
    for i in range(int(hard[key])):
        done = kubectl("create", "-n", namespace, "-f", "-", input=json.dumps(make(f"claimcheck-fill-{i}")))
        assert done.returncode == 0, f"object {i + 1} of {hard[key]}: {done.stderr}"
    done = kubectl("create", "-n", namespace, "-f", "-", input=json.dumps(make("claimcheck-fill-one-too-many")))
    assert done.returncode != 0, done.stdout
    assert "exceeded quota" in done.stderr and key in done.stderr, done.stderr
