"""Unit tests for the Kubernetes executor. kubectl is faked; see integration/ for a real cluster."""

import io
import json
import subprocess
import tarfile

import pytest

from claimcheck import k8s
from claimcheck.models import Status


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "poc.py").write_text("raise SystemExit('boom')\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret-ish\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "big.js").write_text("x")
    return tmp_path


def pod_spec(job):
    return job["spec"]["template"]["spec"]


# --- manifests ---------------------------------------------------------------------------


def test_job_is_hardened():
    job = k8s.build_job("run-1", "python:3.12-slim", "python3 poc.py", timeout=30)
    spec = pod_spec(job)
    container = spec["containers"][0]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 30
    assert job["spec"]["ttlSecondsAfterFinished"] > 0
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    sc = container["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["privileged"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]}
    assert container["resources"]["limits"]["cpu"]
    assert container["resources"]["limits"]["memory"]
    assert "runtimeClassName" not in spec


def test_job_env_has_no_inherited_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    container = pod_spec(k8s.build_job("run-1", "img", "true", timeout=5))["containers"][0]
    names = {e["name"] for e in container["env"]}
    assert names == {"HOME", "LANG", "PATH"}
    assert "ghs_secret" not in json.dumps(container)


def test_command_is_passed_as_one_argument_not_interpolated():
    command = "python3 poc.py; echo $HOME"
    container = pod_spec(k8s.build_job("run-1", "img", command, timeout=5))["containers"][0]
    assert container["command"][:2] == ["sh", "-c"]
    assert container["env"][0] != command
    assert container["args"] == [command]  # reaches the inner script as "$0", never pasted into it
    assert command not in container["command"][2]


def test_repo_is_mounted_read_only_and_workdir_is_size_limited():
    spec = pod_spec(k8s.build_job("run-1", "img", "true", timeout=5))
    mounts = {m["mountPath"]: m for m in spec["containers"][0]["volumeMounts"]}
    assert mounts["/input"]["readOnly"] is True
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["input"]["configMap"]["name"] == "run-1"
    assert volumes["work"]["emptyDir"]["sizeLimit"]


def test_runtime_class_is_set_when_asked():
    spec = pod_spec(k8s.build_job("run-1", "img", "true", timeout=5, runtime_class="gvisor"))
    assert spec["runtimeClassName"] == "gvisor"


def test_network_policy_denies_all_traffic_for_the_run():
    policy = k8s.build_network_policy("run-1")
    assert policy["kind"] == "NetworkPolicy"
    assert policy["spec"]["podSelector"] == {"matchLabels": {k8s.RUN_LABEL: "run-1"}}
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in policy["spec"] and "egress" not in policy["spec"]


def test_pack_repo_skips_git_and_vendored_dirs(repo):
    data = k8s.pack_repo(repo)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "repo/poc.py" in names
    assert not [n for n in names if ".git" in n or "node_modules" in n]


def test_configmap_carries_the_tarball_and_is_size_capped(repo):
    cm = k8s.build_configmap("run-1", k8s.pack_repo(repo))
    assert "repo.tgz" in cm["binaryData"]
    with pytest.raises(k8s.RepoTooLarge):
        k8s.build_configmap("run-1", b"x" * (k8s.MAX_REPO_BYTES + 1))


def test_all_objects_carry_the_run_label(repo):
    for obj in (k8s.build_job("run-1", "img", "true", timeout=5), k8s.build_network_policy("run-1"),
                k8s.build_configmap("run-1", k8s.pack_repo(repo))):
        assert obj["metadata"]["labels"][k8s.RUN_LABEL] == "run-1"


# --- executor against a fake kubectl -------------------------------------------------------


class FakeKubectl:
    """Records kubectl calls and answers like a cluster where the pod ends with `exit_code`."""

    def __init__(self, exit_code=1, logs="Traceback\nRuntimeError: boom", pod_state=None, apply_fails=False):
        # logs may be a list: one entry per pod run, in order (e.g. network probe, then the repro)
        self.calls, self.exit_code = [], exit_code
        self.logs = list(logs) if isinstance(logs, list) else None
        self.fixed_logs = logs
        self.pod_state = pod_state
        self.apply_fails = apply_fails

    def __call__(self, args, input=None, capture_output=True, text=True, timeout=None):
        self.calls.append((args, input))
        verb = args[args.index("kubectl") + 1] if "kubectl" in args else args[1]
        if verb == "apply":
            if self.apply_fails:
                return subprocess.CompletedProcess(args, 1, "", "error: forbidden")
            return subprocess.CompletedProcess(args, 0, "applied", "")
        if verb == "get":
            state = self.pod_state or {"terminated": {"exitCode": self.exit_code, "reason": "Error"}}
            pods = {"items": [{"status": {"containerStatuses": [{"state": state}]}}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(pods), "")
        if verb == "logs":
            out = self.logs.pop(0) if self.logs is not None else self.fixed_logs
            return subprocess.CompletedProcess(args, 0, out, "")
        if verb == "delete":
            return subprocess.CompletedProcess(args, 0, "deleted", "")
        raise AssertionError(f"unexpected kubectl call: {args}")

    def verbs(self):
        return [a[a.index("kubectl") + 1] for a, _ in self.calls]


def executor(fake, **kw):
    kw.setdefault("require_network_isolation", False)  # network preflight has its own tests below
    return k8s.KubernetesExecutor(namespace="claimcheck", image="python:3.12-slim", run=fake, poll_interval=0, **kw)


def test_reproduced_in_a_pod(repo):
    fake = FakeKubectl(exit_code=1, logs="RuntimeError: boom")
    evidence = executor(fake).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.CONFIRMED
    assert evidence.source.startswith("kubernetes[claimcheck/default runtime]")
    assert fake.verbs()[0] == "apply" and fake.verbs()[-1] == "delete"


def test_applied_objects_are_job_configmap_and_policy(repo):
    fake = FakeKubectl()
    executor(fake).run_repro(repo, "python3 poc.py", "boom")
    applied = json.loads(fake.calls[0][1])
    assert sorted(o["kind"] for o in applied["items"]) == ["ConfigMap", "Job", "NetworkPolicy"]


def test_exit_zero_in_pod_is_refuted(repo):
    assert executor(FakeKubectl(exit_code=0, logs="ok")).run_repro(repo, "true", "boom").status is Status.REFUTED


def test_image_pull_failure_is_a_setup_failure(repo):
    fake = FakeKubectl(pod_state={"waiting": {"reason": "ImagePullBackOff", "message": "not found"}})
    evidence = executor(fake, deadline=0).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "ImagePullBackOff" in evidence.detail
    assert fake.verbs()[-1] == "delete"


def test_pod_that_never_finishes_is_unverified_and_cleaned_up(repo):
    fake = FakeKubectl(pod_state={"running": {}})
    evidence = executor(fake, deadline=0).run_repro(repo, "sleep 999", "boom", timeout=1)
    assert evidence.status is Status.UNVERIFIED
    assert "did not finish" in evidence.detail
    assert fake.verbs()[-1] == "delete"


def test_apply_failure_is_unverified_and_still_cleans_up(repo):
    fake = FakeKubectl(apply_fails=True)
    evidence = executor(fake).run_repro(repo, "true", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "forbidden" in evidence.detail
    assert fake.verbs()[-1] == "delete"


def test_kube_context_and_namespace_are_passed(repo):
    fake = FakeKubectl()
    executor(fake, context="kind-ci").run_repro(repo, "true", "boom")
    for args, _ in fake.calls:
        assert "--context" in args and args[args.index("--context") + 1] == "kind-ci"
        assert args[args.index("-n") + 1] == "claimcheck"


def test_runtime_class_shows_in_source(repo):
    evidence = executor(FakeKubectl(), runtime_class="gvisor").run_repro(repo, "true", "boom")
    assert "claimcheck/gvisor" in evidence.source


def test_oversized_repo_is_unverified_without_touching_the_cluster(repo, monkeypatch):
    monkeypatch.setattr(k8s, "MAX_REPO_BYTES", 10)
    fake = FakeKubectl()
    evidence = executor(fake).run_repro(repo, "true", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "too large" in evidence.detail
    assert fake.calls == []


# --- network preflight: a NetworkPolicy only works if the cluster's CNI enforces it ----------


def test_open_network_refuses_to_run_the_repro(repo):
    fake = FakeKubectl(exit_code=1, logs=["CONNECTED 1.1.1.1 53", "RuntimeError: boom"])
    evidence = executor(fake, require_network_isolation=True).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "does not enforce NetworkPolicy" in evidence.detail
    assert fake.verbs().count("apply") == 1  # only the probe ran; the untrusted command never did


def test_enforced_network_runs_the_repro_and_says_so(repo):
    fake = FakeKubectl(exit_code=1, logs=[k8s.PROBE_OK, "RuntimeError: boom"])
    evidence = executor(fake, require_network_isolation=True).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.CONFIRMED
    assert "(egress blocked, verified)" in evidence.source
    assert fake.verbs().count("apply") == 2


def test_preflight_runs_once_per_executor(repo):
    fake = FakeKubectl(exit_code=1, logs=[k8s.PROBE_OK, "boom", "boom"])
    ex = executor(fake, require_network_isolation=True)
    ex.run_repro(repo, "python3 poc.py", "boom")
    ex.run_repro(repo, "python3 poc.py", "boom")
    assert fake.verbs().count("apply") == 3


def test_probe_that_cannot_start_counts_as_not_enforced(repo):
    fake = FakeKubectl(pod_state={"waiting": {"reason": "ErrImagePull"}})
    evidence = executor(fake, require_network_isolation=True, deadline=0).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "does not enforce NetworkPolicy" in evidence.detail


def test_skipping_the_preflight_is_visible_in_the_source(repo):
    evidence = executor(FakeKubectl()).run_repro(repo, "python3 poc.py", "boom")
    assert "(egress not verified)" in evidence.source


def test_probe_script_checks_api_and_internet():
    assert "1.1.1.1" in k8s.PROBE_SCRIPT and "KUBERNETES_SERVICE_HOST" in k8s.PROBE_SCRIPT
