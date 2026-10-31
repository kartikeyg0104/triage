"""Unit tests for the Kubernetes executor. kubectl is faked; see integration/ for a real cluster."""

import io
import json
import subprocess
import tarfile
from pathlib import Path

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


# --- running as a least-privilege identity (kubectl --as) ------------------------------------

EXECUTOR_SA = "system:serviceaccount:claimcheck:claimcheck-executor"


def test_impersonate_adds_as_to_every_call_including_probe_and_cleanup(repo):
    fake = FakeKubectl(exit_code=1, logs=[k8s.PROBE_OK, "RuntimeError: boom"])
    evidence = executor(fake, require_network_isolation=True, impersonate=EXECUTOR_SA).run_repro(
        repo, "python3 poc.py", "boom")
    assert evidence.status is Status.CONFIRMED
    assert fake.verbs().count("apply") == 2 and fake.verbs().count("delete") == 2  # probe + repro
    assert {"apply", "get", "logs", "delete"} <= set(fake.verbs())
    for args, _ in fake.calls:
        assert args.count("--as") == 1, args
        assert args[args.index("--as") + 1] == EXECUTOR_SA


def test_impersonate_also_applies_when_apply_fails(repo):
    fake = FakeKubectl(apply_fails=True)
    executor(fake, impersonate=EXECUTOR_SA).run_repro(repo, "true", "boom")
    assert fake.verbs() == ["apply", "delete"]
    assert all(args[args.index("--as") + 1] == EXECUTOR_SA for args, _ in fake.calls)


def test_no_impersonation_by_default(repo):
    fake = FakeKubectl(exit_code=1, logs=[k8s.PROBE_OK, "boom"])
    executor(fake, require_network_isolation=True).run_repro(repo, "python3 poc.py", "boom")
    assert fake.calls and not [args for args, _ in fake.calls if "--as" in args]


# --- deploy/k8s: namespace, ServiceAccount, Role, RoleBinding --------------------------------

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "k8s"

# Exactly what the executor's kubectl calls need, per (apiGroup, resource):
#   apply -f - (client-side)              -> get (does it exist?), create; never patch: names are fresh
#   delete ... -l <run> --wait=true       -> list (resolve the selector), delete, watch (wait)
#   get pods -l <run> -o json             -> pods: list
#   logs job/<name>                       -> jobs: get, pods: list, pods/log: get
EXPECTED_RULES = {
    ("", "configmaps"): {"get", "create", "list", "delete", "watch"},
    ("networking.k8s.io", "networkpolicies"): {"get", "create", "list", "delete", "watch"},
    ("batch", "jobs"): {"get", "create", "list", "delete", "watch"},
    ("", "pods"): {"list"},
    ("", "pods/log"): {"get"},
}


@pytest.fixture(scope="module")
def manifests():
    yaml = pytest.importorskip("yaml", reason="PyYAML not installed; the cluster tests cover deploy/k8s")
    kustomization = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text())
    docs = []
    for resource in kustomization["resources"]:
        docs += [d for d in yaml.safe_load_all((DEPLOY / resource).read_text()) if d]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def test_namespace_enforces_restricted_pod_security(manifests):
    labels = manifests[("Namespace", "claimcheck")]["metadata"]["labels"]
    for mode in ("enforce", "audit", "warn"):
        assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"


def test_service_account_does_not_automount_its_token(manifests):
    sa = manifests[("ServiceAccount", "claimcheck-executor")]
    assert sa["metadata"]["namespace"] == "claimcheck"
    assert sa["automountServiceAccountToken"] is False


def test_role_is_namespaced_and_least_privilege(manifests):
    assert not [key for key in manifests if key[0] in ("ClusterRole", "ClusterRoleBinding")]
    role = manifests[("Role", "claimcheck-executor")]
    assert role["metadata"]["namespace"] == "claimcheck"
    granted = {}
    for rule in role["rules"]:
        assert not rule.get("resourceNames") and not rule.get("nonResourceURLs")
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                granted.setdefault((group, resource), set()).update(rule["verbs"])
    everything = {verb for verbs in granted.values() for verb in verbs}
    assert "*" not in everything and not [key for key in granted if "*" in key]
    assert not everything & {"escalate", "bind", "impersonate", "deletecollection", "update", "patch"}
    resources = {resource for _, resource in granted}
    assert not resources & {"secrets", "pods/exec", "pods/attach", "pods/portforward", "serviceaccounts/token",
                            "roles", "rolebindings", "serviceaccounts"}
    assert "create" not in granted.get(("", "pods"), set())  # pods come from the Job controller only
    assert granted == EXPECTED_RULES


def test_role_binding_grants_the_role_only_to_the_executor(manifests):
    binding = manifests[("RoleBinding", "claimcheck-executor")]
    assert binding["metadata"]["namespace"] == "claimcheck"
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "claimcheck-executor"}
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "claimcheck-executor", "namespace": "claimcheck"}]
