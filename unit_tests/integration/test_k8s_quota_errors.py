"""Admission denials on a real cluster, confined to this test's temporary namespace.

Run with CLAIMCHECK_K8S_CONTEXT=kind-claimcheck. Only objects created here are deleted.
"""

import json
import os
import subprocess
import time
import uuid

import pytest

from claimcheck import k8s
from claimcheck.models import Status

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
IMAGE = os.environ.get("CLAIMCHECK_K8S_IMAGE", "python:3.12-slim")

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def kubectl(*args: str, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], input=input,
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
def namespace():
    """Admission policies cannot affect other jobs sharing the cluster."""
    name = f"claimcheck-admission-{uuid.uuid4().hex[:10]}"
    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
    done = kubectl("create", "-f", "-", input=json.dumps(manifest))
    assert done.returncode == 0, done.stderr
    try:
        yield name
    finally:
        done = kubectl("delete", "namespace", name, "--wait=false")
        assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("kind, spec, category, constraint, stage", [
    ("ResourceQuota", {"hard": {"count/jobs.batch": "0"}}, "ResourceQuota", "count/jobs.batch", "apply"),
    ("ResourceQuota", {"hard": {"count/pods": "0"}}, "ResourceQuota", "count/pods", "events"),
    ("ResourceQuota", {"hard": {"requests.cpu": "50m"}}, "ResourceQuota", "requests.cpu", "events"),
    ("LimitRange", {"limits": [{"type": "Container", "max": {"cpu": "50m"}}]},
     "LimitRange", "maximum cpu usage per Container", "events"),
], ids=["job-count-apply", "pod-count-events", "cpu-quota-events", "limit-range-events"])
@pytest.mark.parametrize("probe", [False, True], ids=["repro", "network-probe"])
def test_admission_failure_returns_before_deadline(namespace, tmp_path, kind, spec, category, constraint, stage, probe):
    policy_name = "tiny-admission"
    manifest = {"apiVersion": "v1", "kind": kind,
                "metadata": {"name": policy_name, "namespace": namespace}, "spec": spec}
    done = kubectl("create", "-f", "-", input=json.dumps(manifest))
    assert done.returncode == 0, done.stderr
    if kind == "ResourceQuota":
        end = time.monotonic() + 30
        while True:
            done = kubectl("get", "resourcequota", policy_name, "-n", namespace, "-o", "json")
            assert done.returncode == 0, done.stderr
            used = json.loads(done.stdout).get("status", {}).get("used", {})
            if set(used) >= set(spec["hard"]):
                break
            assert time.monotonic() < end, "quota controller did not initialize usage"
            time.sleep(0.2)

    names, denials = [], []

    def recording(args, input=None, **kw):
        done = subprocess.run(args, input=input, **kw)
        if args[1] == "apply":
            names.append(json.loads(input)["items"][0]["metadata"]["name"])
            if done.returncode != 0:
                denials.append(done.stderr)
        if args[1:3] == ["get", "events"] and done.returncode == 0:
            denials.extend(event["message"] for event in json.loads(done.stdout).get("items", []))
        return done

    (tmp_path / "poc.py").write_text("raise RuntimeError('boom')\n")
    executor = k8s.KubernetesExecutor(namespace=namespace, image=IMAGE, context=CONTEXT, deadline=20,
                                      poll_interval=0.2, require_network_isolation=probe, run=recording)
    start = time.monotonic()
    evidence = executor.run_repro(tmp_path, "python3 poc.py", "boom", timeout=60)
    elapsed = time.monotonic() - start

    assert evidence.status is Status.UNVERIFIED, evidence.detail
    assert f"{category} setup failure" in evidence.detail, evidence.detail
    assert constraint in evidence.detail, evidence.detail
    if kind == "ResourceQuota":
        assert policy_name in evidence.detail, evidence.detail
    assert "did not finish" not in evidence.detail and "NetworkPolicy" not in evidence.detail
    assert elapsed < 15, f"waited {elapsed:.1f}s for a 20s deadline: {evidence.detail}"
    assert len(names) == 1, names
    assert any(constraint in denial for denial in denials), (stage, denials)
    if probe:
        assert "network probe" in evidence.detail and "egress not verified" in evidence.source
    for name in names:
        done = kubectl("get", "jobs,configmaps,networkpolicies,pods", "-n", namespace,
                       "-l", f"{k8s.RUN_LABEL}={name}", "-o", "name")
        assert done.returncode == 0, done.stderr
        assert not done.stdout.strip(), done.stdout
