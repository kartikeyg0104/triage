"""deploy/service: the claimcheck API and worker, checked without a cluster.

Renders `kubectl kustomize deploy/service` (skips without kubectl) and parses it with PyYAML
(skips without it). unit_tests/integration/test_service_manifests_cluster.py has a real API
server check the same objects against the `restricted` Pod Security Standard.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "service"
NAMESPACE = "claimcheck-service"
IMAGE = "ghcr.io/kartikeyg0104/claimcheck"
SECRET = "claimcheck-secrets"
SECRET_ENV = ("CLAIMCHECK_API_TOKEN", "CLAIMCHECK_WEBHOOK_SECRET")
DB = "/data/claimcheck.db"
API_ARGS = ["-m", "claimcheck.server", "--db", DB, "--repo-root", "/repos", "--host", "0.0.0.0"]


def render(path: Path = DEPLOY) -> list[dict]:
    yaml = pytest.importorskip("yaml", reason="PyYAML not installed")
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not installed; needed for `kubectl kustomize`")
    done = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


@pytest.fixture(scope="module")
def objects() -> dict[tuple[str, str], dict]:
    return {(d["kind"], d["metadata"]["name"]): d for d in render()}


@pytest.fixture(scope="module")
def pod(objects) -> dict:
    return objects[("Deployment", "claimcheck-api")]["spec"]["template"]["spec"]


@pytest.fixture(scope="module")
def containers(pod) -> dict[str, dict]:
    return {c["name"]: c for c in pod["containers"]}


def all_containers(pod) -> list[dict]:
    return pod["containers"] + pod.get("initContainers", [])


def test_namespace_enforces_restricted(objects):
    labels = objects[("Namespace", NAMESPACE)]["metadata"]["labels"]
    for mode in ("enforce", "audit", "warn"):
        assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"


def test_every_namespaced_object_is_in_the_service_namespace(objects):
    for (kind, name), obj in objects.items():
        if kind != "Namespace":
            assert obj["metadata"].get("namespace") == NAMESPACE, (kind, name)


def test_secret_is_not_shipped(objects):
    assert not [key for key in objects if key[0] == "Secret"]


def test_api_container(containers):
    api = containers["api"]
    assert api["image"] == f"{IMAGE}:latest"
    assert api["command"] == ["python"]  # the image's ENTRYPOINT is the `claimcheck` CLI
    assert api["args"] == API_ARGS
    assert any(p["containerPort"] == 8080 for p in api["ports"])


def test_worker_container_shares_the_database(containers):
    worker = containers["worker"]
    assert worker["image"] == f"{IMAGE}:latest"
    assert worker["command"] == ["python"]
    args = worker["args"]
    assert args[:2] == ["-m", "claimcheck.server.worker"]
    assert args[args.index("--db") + 1] == DB
    assert args[args.index("--repo-root") + 1] == "/repos"
    assert "--repos" in args


def test_secrets_come_from_the_secret(containers):
    env = {e["name"]: e for e in containers["api"]["env"]}
    for name in SECRET_ENV:
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == SECRET and ref["key"] == name
        assert "value" not in env[name]
    assert not [e for e in containers["worker"].get("env", []) if e["name"] in SECRET_ENV]  # the worker needs neither


def test_sqlite_on_one_rwo_volume_stays_in_one_pod(objects, pod):
    """Both processes open one SQLite file, so they share one pod (one node, one mount) and the
    Deployment never runs two pods at once."""
    pvc = objects[("PersistentVolumeClaim", "claimcheck-data")]
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    deployment = objects[("Deployment", "claimcheck-api")]["spec"]
    assert deployment["replicas"] == 1
    assert deployment["strategy"]["type"] == "Recreate"
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["data"]["persistentVolumeClaim"]["claimName"] == "claimcheck-data"
    assert "emptyDir" in volumes["tmp"]
    for container in pod["containers"]:
        mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
        assert mounts["/data"]["name"] == "data" and not mounts["/data"].get("readOnly")
        assert mounts["/tmp"]["name"] == "tmp"
        assert mounts["/repos"].get("readOnly") is True
    assert not [key for key in objects if key == ("Deployment", "claimcheck-worker")]


def test_pod_security_context(pod):
    assert pod["automountServiceAccountToken"] is False
    context = pod["securityContext"]
    assert context["runAsNonRoot"] is True
    assert context["runAsUser"] == 10001
    assert context["fsGroup"] == 10001  # so the PVC is writable by the non-root user
    assert context["seccompProfile"]["type"] == "RuntimeDefault"
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        assert not pod.get(field)


def test_every_container_is_locked_down(pod):
    for container in all_containers(pod):
        context = container["securityContext"]
        assert context["runAsNonRoot"] is True, container["name"]
        assert context["runAsUser"] == 10001, container["name"]
        assert context["readOnlyRootFilesystem"] is True, container["name"]
        assert context["allowPrivilegeEscalation"] is False, container["name"]
        assert context["capabilities"]["drop"] == ["ALL"], container["name"]
        assert not context["capabilities"].get("add"), container["name"]
        assert context["seccompProfile"]["type"] == "RuntimeDefault", container["name"]
        assert not context.get("privileged"), container["name"]


def test_every_container_has_requests_and_limits(pod):
    for container in all_containers(pod):
        resources = container["resources"]
        for kind in ("requests", "limits"):
            assert {"cpu", "memory"} <= set(resources[kind]), (container["name"], kind)


def test_every_container_has_probes(pod):
    for container in pod["containers"]:
        for probe in ("readinessProbe", "livenessProbe"):
            assert probe in container, (container["name"], probe)


def test_api_probes_are_tcp(containers):
    """The server has no unauthenticated health path, so the probes only check the socket."""
    for probe in ("readinessProbe", "livenessProbe"):
        assert containers["api"][probe]["tcpSocket"]["port"] in (8080, "http")


def test_service_targets_the_api(objects, pod):
    service = objects[("Service", "claimcheck-api")]["spec"]
    assert service.get("type", "ClusterIP") == "ClusterIP"
    labels = objects[("Deployment", "claimcheck-api")]["spec"]["template"]["metadata"]["labels"]
    assert service["selector"].items() <= labels.items()
    assert [p["targetPort"] for p in service["ports"]] in ([8080], ["http"])


def test_image_is_overridable(tmp_path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "kustomization.yaml").write_text(
        f"resources:\n  - {os.path.relpath(DEPLOY, overlay.resolve())}\nimages:\n  - name: {IMAGE}\n    newName: registry.example/cc\n    newTag: v1\n")
    deployment = next(d for d in render(overlay) if d["kind"] == "Deployment")
    images = {c["image"] for c in deployment["spec"]["template"]["spec"]["containers"]}
    assert images == {"registry.example/cc:v1"}
