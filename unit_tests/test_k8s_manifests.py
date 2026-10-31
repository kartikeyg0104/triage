"""deploy/k8s: ResourceQuota and LimitRange for namespace claimcheck, checked without a cluster.

Needs PyYAML (skips without it, like the manifest tests in test_k8s.py); the cluster tests in
unit_tests/integration/test_k8s_quota.py check the same objects on a real API server.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest

from claimcheck import k8s

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "k8s"
NAMESPACE = "claimcheck"
MIN_CONCURRENT_RUNS = 4  # each run has one pod (probe, then repro) at a time
QUOTA_KEYS = {"count/pods", "count/jobs.batch", "configmaps", "requests.cpu", "requests.memory",
              "limits.cpu", "limits.memory"}

_SUFFIX = {"m": Decimal("0.001"), "": Decimal(1), "k": Decimal(10**3), "M": Decimal(10**6), "G": Decimal(10**9),
           "Ki": Decimal(2**10), "Mi": Decimal(2**20), "Gi": Decimal(2**30)}


def quantity(value) -> Decimal:
    match = re.fullmatch(r"([0-9.]+)(m|k|M|G|Ki|Mi|Gi)?", str(value))
    assert match, f"unsupported quantity {value!r}"
    return Decimal(match.group(1)) * _SUFFIX[match.group(2) or ""]


@pytest.fixture(scope="module")
def manifests():
    yaml = pytest.importorskip("yaml", reason="PyYAML not installed; the cluster tests cover deploy/k8s")
    kustomization = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text())
    docs = []
    for resource in kustomization["resources"]:
        docs += [d for d in yaml.safe_load_all((DEPLOY / resource).read_text()) if d]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


@pytest.fixture(scope="module")
def quota(manifests):
    quotas = [d for (kind, _), d in manifests.items() if kind == "ResourceQuota"]
    assert len(quotas) == 1, quotas
    assert quotas[0]["metadata"]["namespace"] == NAMESPACE
    return {key: quantity(value) for key, value in quotas[0]["spec"]["hard"].items()}


@pytest.fixture(scope="module")
def container_limits(manifests):
    ranges = [d for (kind, _), d in manifests.items() if kind == "LimitRange"]
    assert len(ranges) == 1, ranges
    assert ranges[0]["metadata"]["namespace"] == NAMESPACE
    limits = [item for item in ranges[0]["spec"]["limits"] if item["type"] == "Container"]
    assert len(limits) == 1
    return limits[0]


def executor_container():
    return k8s.build_job("claimcheck-x", "python:3.12-slim", "true", timeout=30)["spec"]["template"]["spec"]["containers"][0]


def test_quota_caps_every_resource_the_issue_names(quota):
    assert QUOTA_KEYS <= set(quota)


def test_limit_range_sets_container_default_default_request_and_max(container_limits):
    for field in ("default", "defaultRequest", "max"):
        assert {"cpu", "memory"} <= set(container_limits[field]), field


def test_limit_range_is_consistent(container_limits):
    for resource in ("cpu", "memory"):
        request = quantity(container_limits["defaultRequest"][resource])
        limit = quantity(container_limits["default"][resource])
        assert request <= limit <= quantity(container_limits["max"][resource]), resource


def test_executor_container_fits_under_the_limit_range_max(container_limits):
    resources = executor_container()["resources"]
    for resource in ("cpu", "memory"):
        maximum = quantity(container_limits["max"][resource])
        assert quantity(resources["requests"][resource]) <= maximum
        assert quantity(resources["limits"][resource]) <= maximum


def test_limit_range_defaults_match_the_executor(container_limits):
    """A pod that names no resources gets what the executor asks for, not more."""
    resources = executor_container()["resources"]
    for resource in ("cpu", "memory"):
        assert quantity(container_limits["defaultRequest"][resource]) == quantity(resources["requests"][resource])
        assert quantity(container_limits["default"][resource]) == quantity(resources["limits"][resource])


def test_quota_leaves_room_for_several_concurrent_runs(quota):
    resources = executor_container()["resources"]
    for kind in ("requests", "limits"):
        for resource in ("cpu", "memory"):
            per_pod = quantity(resources[kind][resource])
            assert quota[f"{kind}.{resource}"] >= MIN_CONCURRENT_RUNS * per_pod, f"{kind}.{resource}"
    assert quota["count/pods"] >= MIN_CONCURRENT_RUNS
    assert quota["count/jobs.batch"] >= MIN_CONCURRENT_RUNS
    # one ConfigMap per run, plus kube-root-ca.crt, which every namespace has
    assert quota["configmaps"] >= MIN_CONCURRENT_RUNS + 1


def test_quota_is_a_real_cap(quota):
    """Room for a few runs, not effectively unlimited: at most 10 executor pods at once."""
    resources = executor_container()["resources"]
    assert quota["count/pods"] <= 10
    assert quota["count/jobs.batch"] <= 10
    assert quota["limits.cpu"] <= 10 * quantity(resources["limits"]["cpu"])
    assert quota["limits.memory"] <= 10 * quantity(resources["limits"]["memory"])
