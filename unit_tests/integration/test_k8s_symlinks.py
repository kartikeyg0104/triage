"""Integration test: a repo's symlinks cannot reach files outside the repo inside the pod.

Run with CLAIMCHECK_K8S_CONTEXT=<kubectl context> (e.g. kind-claimcheck). Skipped otherwise.
Uses the existing namespace claimcheck-it (override with CLAIMCHECK_K8S_NAMESPACE); its CNI must
enforce NetworkPolicy. Only this run's own objects are created and deleted.
"""

import os
import re

import pytest

from claimcheck import k8s
from claimcheck.models import Status

CONTEXT = os.environ.get("CLAIMCHECK_K8S_CONTEXT")
NAMESPACE = os.environ.get("CLAIMCHECK_K8S_NAMESPACE", "claimcheck-it")
IMAGE = os.environ.get("CLAIMCHECK_K8S_IMAGE", "python:3.12-slim")

pytestmark = pytest.mark.skipif(not CONTEXT, reason="set CLAIMCHECK_K8S_CONTEXT to run against a real cluster")


def test_absolute_symlink_does_not_read_the_pods_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "poc.txt").write_text("INSIDE-LINK-OK\n")
    (repo / "inside").symlink_to("poc.txt")
    (repo / "leak").symlink_to("/etc/hostname")  # in the pod: the pod's hostname, i.e. its Job pod name
    executor = k8s.KubernetesExecutor(namespace=NAMESPACE, image=IMAGE, context=CONTEXT, deadline=180)

    evidence = executor.run_repro(repo, "cat inside && cat leak", "", timeout=90)

    assert "egress blocked, verified" in evidence.source, evidence.detail
    assert "INSIDE-LINK-OK" in evidence.detail  # the inside link still works in the pod
    assert "leak: No such file or directory" in evidence.detail  # the outside link never reached the pod
    assert not re.search(r"claimcheck-[0-9a-f]{10}", evidence.detail), evidence.detail  # no pod hostname
    if os.path.exists("/etc/hostname"):  # the machine running the test: not its file either
        host = open("/etc/hostname").read().strip()
        assert not host or host not in evidence.detail
    assert evidence.status is Status.UNVERIFIED  # a missing file is a setup failure, never a verdict
    assert "1 symlink pointing outside the repo was not copied" in evidence.detail
