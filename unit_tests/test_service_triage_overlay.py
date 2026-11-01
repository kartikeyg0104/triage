"""deploy/triage: the service overlay that checks this repository (kartikeyg0104/triage).

Renders `kubectl kustomize deploy/triage` (skips without kubectl or PyYAML) and checks that the
worker maps our own repo, that an init container clones it into /repos, and that the base's
security settings still hold for the added container.
"""

import json
from pathlib import Path

import pytest

from test_service_manifests import all_containers, render

OVERLAY = Path(__file__).resolve().parents[1] / "deploy" / "triage"
REPO = "kartikeyg0104/triage"
URL = "https://github.com/kartikeyg0104/triage.git"
IMAGE = "ghcr.io/kartikeyg0104/claimcheck"


@pytest.fixture(scope="module")
def objects() -> dict[tuple[str, str], dict]:
    return {(d["kind"], d["metadata"]["name"]): d for d in render(OVERLAY)}


@pytest.fixture(scope="module")
def pod(objects) -> dict:
    return objects[("Deployment", "claimcheck-api")]["spec"]["template"]["spec"]


def test_repo_map_points_at_our_repo(objects):
    repos = json.loads(objects[("ConfigMap", "claimcheck-repos")]["data"]["repos.json"])
    assert repos == {REPO: "triage"}


def test_init_container_clones_our_repo_into_the_mapped_path(pod):
    [init] = pod["initContainers"]
    assert init["name"] == "checkout"
    assert init["image"] == f"{IMAGE}:latest"  # same image (it ships git), so image overrides apply to it too
    command = init["command"] + init["args"]
    assert command[:2] == ["git", "clone"]
    assert "--depth" in command
    assert command[-2:] == [URL, "/repos/triage"]
    mounts = {m["mountPath"]: m for m in init["volumeMounts"]}
    assert mounts["/repos"]["name"] == "repos" and not mounts["/repos"].get("readOnly")
    assert mounts["/tmp"]["name"] == "tmp"  # HOME and TMPDIR; the root filesystem stays read-only


def test_api_and_worker_still_read_the_checkout_read_only(pod):
    for container in pod["containers"]:
        mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
        assert mounts["/repos"]["name"] == "repos"
        assert mounts["/repos"].get("readOnly") is True, container["name"]


def test_repos_volume_has_room_for_the_checkout(pod):
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["repos"]["emptyDir"]["sizeLimit"] == "256Mi"


def test_init_container_is_locked_down_like_the_rest(pod):
    for container in all_containers(pod):
        context = container["securityContext"]
        assert context["runAsNonRoot"] is True, container["name"]
        assert context["runAsUser"] == 10001, container["name"]
        assert context["readOnlyRootFilesystem"] is True, container["name"]
        assert context["allowPrivilegeEscalation"] is False, container["name"]
        assert context["capabilities"]["drop"] == ["ALL"], container["name"]
        assert context["seccompProfile"]["type"] == "RuntimeDefault", container["name"]
        for kind in ("requests", "limits"):
            assert {"cpu", "memory"} <= set(container["resources"][kind]), (container["name"], kind)


def test_base_objects_are_unchanged_otherwise(objects):
    assert ("Namespace", "claimcheck-service") in objects
    assert ("PersistentVolumeClaim", "claimcheck-data") in objects
    assert ("Service", "claimcheck-api") in objects
    assert not [key for key in objects if key[0] == "Secret"]
