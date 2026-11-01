"""Unit tests for the Kubernetes executor. kubectl is faked; see integration/ for a real cluster."""

import io
import json
import os
import posixpath
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


def members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


def test_pack_repo_skips_git_and_vendored_dirs(repo):
    (repo / "venv").mkdir()
    (repo / "venv" / "pyvenv.cfg").write_text("x")
    data, skipped = k8s.pack_repo(repo)
    names = list(members(data))
    assert "repo/poc.py" in names
    assert not [n for n in names if ".git" in n or "node_modules" in n or "venv" in n]
    assert skipped == []


@pytest.fixture
def linky(tmp_path):
    """A repo next to a secret, with links that stay inside, links that escape, and a FIFO."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (root / "poc.py").write_text("print('hi')\n")
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)
    (tmp_path / "secret.txt").write_text("host secret\n")
    (root / "leak").symlink_to("/etc/hostname")  # absolute: inside the pod it would read the pod's file
    (root / "up").symlink_to("../secret.txt")  # escapes the repo dir
    (root / "sub" / "deep_up").symlink_to("../../secret.txt")
    (root / "alias").symlink_to("poc.py")  # inside, relative
    (root / "abs_inside").symlink_to(root / "poc.py")  # inside, but absolute on this machine
    (root / "sub" / "back").symlink_to("../poc.py")  # inside, through ..
    os.mkfifo(root / "pipe")
    return root


def escapes(member: tarfile.TarInfo) -> bool:
    """Would this link, unpacked under repo/, point at or above repo/'s parent?"""
    landed = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
    return posixpath.isabs(member.linkname) or not landed.startswith("repo/")


def test_pack_repo_never_stores_a_link_that_points_outside_the_repo(linky):
    data, _ = k8s.pack_repo(linky)
    links = {name: m.linkname for name, m in members(data).items() if m.issym() or m.islnk()}
    assert not [name for name, target in links.items() if posixpath.isabs(target)], links
    assert not [name for name, m in members(data).items() if (m.issym() or m.islnk()) and escapes(m)], links
    assert not {"repo/leak", "repo/up", "repo/sub/deep_up"} & set(links), links


def test_pack_repo_keeps_inside_links_as_relative_links(linky):
    entries = members(k8s.pack_repo(linky)[0])
    assert entries["repo/alias"].issym() and entries["repo/alias"].linkname == "poc.py"
    assert entries["repo/abs_inside"].issym() and entries["repo/abs_inside"].linkname == "poc.py"
    assert entries["repo/sub/back"].issym() and entries["repo/sub/back"].linkname == "../poc.py"


def test_pack_repo_stores_files_as_regular_files_and_skips_fifos(linky):
    data, skipped = k8s.pack_repo(linky)
    entries = members(data)
    assert "repo/pipe" not in entries
    assert not [m.name for m in entries.values() if not (m.isreg() or m.isdir() or m.issym())]
    assert entries["repo"].isdir() and entries["repo/sub"].isdir()
    poc = entries["repo/poc.py"]
    assert poc.isreg() and poc.size == len("print('hi')\n") and (poc.uid, poc.gid, poc.uname) == (0, 0, "")
    assert entries["repo/run.sh"].mode & 0o100  # the executable bit survives
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        assert tar.extractfile("repo/poc.py").read() == b"print('hi')\n"
    assert sorted((d.path, d.kind) for d in skipped) == [
        ("leak", "outside-link"), ("pipe", "special"), ("sub/deep_up", "outside-link"), ("up", "outside-link")]


def test_dropped_links_and_special_files_are_noted_in_the_evidence(linky):
    fake = FakeKubectl(exit_code=1, logs="RuntimeError: boom")
    evidence = executor(fake).run_repro(linky, "python3 poc.py", "boom")
    assert evidence.status is Status.CONFIRMED
    assert "3 symlinks pointing outside the repo were not copied" in evidence.detail
    assert "1 special file (FIFO, socket or device) was skipped" in evidence.detail


def test_no_note_when_nothing_was_left_out(repo):
    evidence = executor(FakeKubectl(exit_code=1, logs="RuntimeError: boom")).run_repro(repo, "python3 poc.py", "boom")
    assert "note:" not in evidence.detail


def test_configmap_carries_the_tarball_and_is_size_capped(repo):
    cm = k8s.build_configmap("run-1", k8s.pack_repo(repo)[0])
    assert "repo.tgz" in cm["binaryData"]
    with pytest.raises(k8s.RepoTooLarge):
        k8s.build_configmap("run-1", b"x" * (k8s.MAX_REPO_BYTES + 1))


def test_all_objects_carry_the_run_label(repo):
    for obj in (k8s.build_job("run-1", "img", "true", timeout=5), k8s.build_network_policy("run-1"),
                k8s.build_configmap("run-1", k8s.pack_repo(repo)[0])):
        assert obj["metadata"]["labels"][k8s.RUN_LABEL] == "run-1"


# --- executor against a fake kubectl -------------------------------------------------------


class FakeKubectl:
    """Records kubectl calls and answers like a cluster where the pod ends with `exit_code`."""

    def __init__(self, exit_code=1, logs="Traceback\nRuntimeError: boom", pod_state=None, apply_fails=False,
                 forbid=None, events=None, event_error=None):
        # logs may be a list: one entry per pod run, in order (e.g. network probe, then the repro)
        # forbid maps a kubectl verb to the stderr it fails with, e.g. an RBAC "is forbidden" message
        self.calls, self.exit_code = [], exit_code
        self.logs = list(logs) if isinstance(logs, list) else None
        self.fixed_logs = logs
        self.pod_state = pod_state
        self.apply_fails = apply_fails
        self.forbid = forbid or {}
        self.events, self.event_error = events or [], event_error

    def __call__(self, args, input=None, capture_output=True, text=True, timeout=None):
        self.calls.append((args, input))
        verb = args[args.index("kubectl") + 1] if "kubectl" in args else args[1]
        if verb in self.forbid:
            return subprocess.CompletedProcess(args, 1, "", self.forbid[verb])
        if verb == "apply":
            if self.apply_fails:
                return subprocess.CompletedProcess(args, 1, "", "error: forbidden")
            return subprocess.CompletedProcess(args, 0, "applied", "")
        if verb == "get":
            if args[2] == "events":
                if self.event_error:
                    return subprocess.CompletedProcess(args, 1, "", self.event_error)
                applied = next(data for call, data in reversed(self.calls) if call[1] == "apply")
                name = json.loads(applied)["items"][0]["metadata"]["name"]
                events = json.loads(json.dumps(self.events).replace('"CURRENT_JOB"', json.dumps(name)))
                return subprocess.CompletedProcess(args, 0, json.dumps({"items": events}), "")
            state = self.pod_state
            if state is None:
                state = {"terminated": {"exitCode": self.exit_code, "reason": "Error"}}
            if isinstance(state, list):
                state = state.pop(0)
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


# --- RBAC denials are setup failures, not NetworkPolicy problems or verdicts -------------------

DEFAULT_SA = "system:serviceaccount:claimcheck:default"

# Verbatim from kubectl v1.36 on kind, as system:serviceaccount:claimcheck:default.
APPLY_FORBIDDEN = f"""Error from server (Forbidden): error when retrieving current configuration of:
Resource: "/v1, Resource=configmaps", GroupVersionKind: "/v1, Kind=ConfigMap"
Name: "claimcheck-abc", Namespace: "claimcheck"
from server for: "STDIN": configmaps "claimcheck-abc" is forbidden: User "{DEFAULT_SA}" cannot get resource "configmaps" in API group "" in the namespace "claimcheck"
Error from server (Forbidden): error when retrieving current configuration of:
Resource: "networking.k8s.io/v1, Resource=networkpolicies", GroupVersionKind: "networking.k8s.io/v1, Kind=NetworkPolicy"
Name: "claimcheck-abc", Namespace: "claimcheck"
from server for: "STDIN": networkpolicies.networking.k8s.io "claimcheck-abc" is forbidden: User "{DEFAULT_SA}" cannot get resource "networkpolicies" in API group "networking.k8s.io" in the namespace "claimcheck"
"""
GET_PODS_FORBIDDEN = (f'Error from server (Forbidden): pods is forbidden: User "{DEFAULT_SA}" cannot list resource '
                      '"pods" in API group "" in the namespace "claimcheck"')
LOGS_FORBIDDEN = (f'Error from server (Forbidden): pods "claimcheck-abc-x" is forbidden: User "{DEFAULT_SA}" cannot get '
                  'resource "pods/log" in API group "" in the namespace "claimcheck"')
DELETE_FORBIDDEN = (f'Error from server (Forbidden): jobs.batch is forbidden: User "{DEFAULT_SA}" cannot list resource '
                    '"jobs" in API group "batch" in the namespace "claimcheck"')
IMPERSONATE_FORBIDDEN = ('Error from server (Forbidden): users "x" is forbidden: User "dev" cannot impersonate resource '
                         '"users" in API group "" at the cluster scope')


def test_rbac_denials_are_parsed_from_kubectl_errors():
    denials = k8s.rbac_denials(APPLY_FORBIDDEN)
    assert [(d.user, d.verb, d.resource, d.namespace) for d in denials] == [
        (DEFAULT_SA, "get", "configmaps", "claimcheck"),
        (DEFAULT_SA, "get", "networkpolicies.networking.k8s.io", "claimcheck")]
    [log] = k8s.rbac_denials(LOGS_FORBIDDEN)
    assert (log.verb, log.resource) == ("get", "pods/log")
    [cluster] = k8s.rbac_denials(IMPERSONATE_FORBIDDEN)
    assert (cluster.user, cluster.verb, cluster.resource, cluster.namespace) == ("dev", "impersonate", "users", None)


@pytest.mark.parametrize("stderr", ["", "error: forbidden", "Unable to connect to the server: dial tcp: i/o timeout",
                                    'pods "p" is forbidden: violates PodSecurity "restricted:latest": privileged'])
def test_other_errors_are_not_rbac_denials(stderr):
    assert k8s.rbac_denials(stderr) == []


def test_rbac_denied_probe_is_an_rbac_failure_not_a_network_policy_problem(repo):
    fake = FakeKubectl(forbid={"apply": APPLY_FORBIDDEN})
    ex = executor(fake, require_network_isolation=True, impersonate=DEFAULT_SA)
    evidence = ex.run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "RBAC setup failure" in evidence.detail and "network probe" in evidence.detail
    assert "NetworkPolicy" not in evidence.detail
    assert f'User "{DEFAULT_SA}" cannot get configmaps, get networkpolicies.networking.k8s.io' in evidence.detail
    assert 'namespace "claimcheck"' in evidence.detail
    assert "(egress not verified)" in evidence.source  # RBAC stopped the probe, so egress is unknown, not open
    assert fake.verbs().count("apply") == 1  # the untrusted command never ran
    assert ex.network_isolated() is False


def test_rbac_denied_probe_is_not_retried_for_every_repro(repo):
    fake = FakeKubectl(forbid={"apply": APPLY_FORBIDDEN})
    ex = executor(fake, require_network_isolation=True)
    for _ in range(2):
        assert "RBAC setup failure" in ex.run_repro(repo, "python3 poc.py", "boom").detail
    assert fake.verbs().count("apply") == 1


def test_rbac_denied_apply_of_the_repro_names_the_permission(repo):
    stderr = (f'Error from server (Forbidden): error when creating "STDIN": jobs.batch is forbidden: User "{DEFAULT_SA}" '
              'cannot create resource "jobs" in API group "batch" in the namespace "claimcheck"')
    fake = FakeKubectl(forbid={"apply": stderr})
    evidence = executor(fake).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "RBAC setup failure" in evidence.detail and "repro" in evidence.detail
    assert "cannot create jobs.batch" in evidence.detail
    assert fake.verbs()[-1] == "delete"


def test_rbac_denied_pod_polling_fails_at_once_instead_of_timing_out(repo):
    fake = FakeKubectl(forbid={"get": GET_PODS_FORBIDDEN})
    evidence = executor(fake, deadline=1).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "RBAC setup failure" in evidence.detail and "cannot list pods" in evidence.detail
    assert "did not finish" not in evidence.detail
    assert fake.verbs().count("get") == 1 and fake.verbs()[-1] == "delete"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_rbac_denied_logs_never_become_a_verdict(repo, exit_code):
    """Without logs an exit 0 would look like 'not reproduced'; it is a setup problem."""
    fake = FakeKubectl(exit_code=exit_code, forbid={"logs": LOGS_FORBIDDEN})
    evidence = executor(fake).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "RBAC setup failure" in evidence.detail and "cannot get pods/log" in evidence.detail


def test_rbac_denied_cleanup_keeps_the_result_but_says_objects_may_be_left(repo):
    fake = FakeKubectl(exit_code=1, logs="RuntimeError: boom", forbid={"delete": DELETE_FORBIDDEN})
    evidence = executor(fake).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.CONFIRMED
    assert "cleanup" in evidence.detail and "cannot list jobs.batch" in evidence.detail
    assert "left behind" in evidence.detail


def test_rbac_denied_cleanup_is_reported_with_an_rbac_failure(repo):
    fake = FakeKubectl(forbid={"apply": APPLY_FORBIDDEN, "delete": DELETE_FORBIDDEN})
    evidence = executor(fake).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "cannot get configmaps, get networkpolicies.networking.k8s.io, list jobs.batch" in evidence.detail


def test_unparsed_apply_errors_keep_the_generic_setup_failure(repo):
    evidence = executor(FakeKubectl(apply_fails=True)).run_repro(repo, "true", "boom")
    assert evidence.detail.startswith("setup failure: could not create the Job")
    assert "RBAC" not in evidence.detail


# --- admission failures must not hide behind a pod deadline --------------------------------

QUOTA_MESSAGE = ('pods "claimcheck-abc-x" is forbidden: exceeded quota: tiny-pods, '
                 'requested: count/pods=1, used: count/pods=0, limited: count/pods=0')
LIMIT_MESSAGE = ('pods "claimcheck-abc-x" is forbidden: maximum cpu usage per Container is 50m, '
                 'but limit is 500m')


def failed_create(message, name="CURRENT_JOB", kind="Job", reason="FailedCreate"):
    return {"involvedObject": {"kind": kind, "name": name}, "reason": reason, "message": message}


@pytest.mark.parametrize("message, category, constraint", [
    (QUOTA_MESSAGE, "ResourceQuota", "tiny-pods"),
    ('pods "p" is forbidden: failed quota: required-resources: must specify requests.ephemeral-storage',
     "ResourceQuota", "required-resources"),
    (LIMIT_MESSAGE, "LimitRange", "maximum cpu"),
    ('pods "p" is forbidden: minimum memory usage per Container is 128Mi, but request is 64Mi',
     "LimitRange", "minimum memory"),
    ('pods "p" is forbidden: cpu max limit to request ratio per Container is 2, but provided ratio is 5.000000',
     "LimitRange", "ratio"),
])
@pytest.mark.parametrize("stage", ["apply", "events"])
def test_admission_denials_name_the_constraint_and_stop_before_sleep(repo, monkeypatch, message, category, constraint, stage):
    def sleep(_):
        pytest.fail("admission denial waited for the deadline")

    monkeypatch.setattr(k8s.time, "sleep", sleep)
    fake = FakeKubectl(pod_state={}, events=[failed_create(message)],
                       forbid={"apply": f'Error from server (Forbidden): error when creating "STDIN": {message}'}
                       if stage == "apply" else {})
    evidence = executor(fake, deadline=60).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert f"{category} setup failure" in evidence.detail
    assert constraint in evidence.detail and message in evidence.detail
    assert "did not finish" not in evidence.detail
    assert fake.verbs()[-1] == "delete" and "logs" not in fake.verbs()
    if stage == "events":
        [args] = [args for args, _ in fake.calls if args[1:3] == ["get", "events"]]
        selector = args[args.index("--field-selector") + 1]
        applied = json.loads(fake.calls[0][1])["items"][0]["metadata"]["name"]
        assert f"involvedObject.name={applied}" in selector
        assert "involvedObject.kind=Job" in selector and "reason=FailedCreate" in selector


@pytest.mark.parametrize("stage", ["apply", "events"])
def test_admission_denied_probe_keeps_egress_unknown_and_does_not_run_repro(repo, stage):
    fake = FakeKubectl(pod_state={}, events=[failed_create(QUOTA_MESSAGE)],
                       forbid={"apply": QUOTA_MESSAGE} if stage == "apply" else {})
    ex = executor(fake, require_network_isolation=True, deadline=0)
    for _ in range(2):
        evidence = ex.run_repro(repo, "python3 poc.py", "boom")
        assert evidence.status is Status.UNVERIFIED
        assert "ResourceQuota setup failure" in evidence.detail and "network probe" in evidence.detail
        assert "tiny-pods" in evidence.detail
        assert "NetworkPolicy" not in evidence.detail
        assert "egress not verified" in evidence.source
    assert fake.verbs().count("apply") == 1 and fake.verbs()[-1] == "delete"


@pytest.mark.parametrize("event", [
    failed_create(QUOTA_MESSAGE, name="another-job"),
    failed_create(QUOTA_MESSAGE, kind="Pod"),
    failed_create(QUOTA_MESSAGE, reason="FailedScheduling"),
    failed_create('pods "p" is forbidden: violates PodSecurity "restricted:latest": privileged'),
    failed_create("admission webhook denied the request"),
    failed_create(""),
])
def test_unrelated_events_are_not_quota_or_limit_range_denials(repo, event):
    fake = FakeKubectl(pod_state={}, events=[event])
    evidence = executor(fake, deadline=0).run_repro(repo, "true", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "did not finish" in evidence.detail
    assert fake.verbs()[-1] == "delete"


def test_admission_events_are_polled_again_until_the_controller_reports_them(repo, monkeypatch):
    fake = FakeKubectl(pod_state={})

    def sleep(_):
        assert not fake.events, "did not detect the admission event on the next poll"
        fake.events = [failed_create(QUOTA_MESSAGE)]

    monkeypatch.setattr(k8s.time, "sleep", sleep)
    evidence = executor(fake, deadline=60).run_repro(repo, "true", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "ResourceQuota setup failure" in evidence.detail
    assert len([args for args, _ in fake.calls if args[1:3] == ["get", "events"]]) == 2


@pytest.mark.parametrize("error", [
    'events is forbidden: User "restricted" cannot list resource "events" in API group "" in the namespace "claimcheck"',
    "Unable to connect to the server: dial tcp: i/o timeout",
])
def test_unreadable_events_preserve_the_pod_timeout(repo, error):
    fake = FakeKubectl(pod_state={}, event_error=error)
    evidence = executor(fake, deadline=0).run_repro(repo, "true", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "did not finish" in evidence.detail


def test_unreadable_events_do_not_prevent_a_pod_from_finishing(repo):
    fake = FakeKubectl(pod_state=[{}, {"terminated": {"exitCode": 1}}], event_error="error: forbidden")
    evidence = executor(fake).run_repro(repo, "true", "boom")
    assert evidence.status is Status.CONFIRMED


def test_completed_pods_do_not_consult_admission_events(repo):
    fake = FakeKubectl(events=[failed_create(QUOTA_MESSAGE)])
    evidence = executor(fake).run_repro(repo, "true", "boom")
    assert evidence.status is Status.CONFIRMED
    assert not [args for args, _ in fake.calls if args[1:3] == ["get", "events"]]


def test_admission_failure_still_reports_denied_cleanup(repo):
    fake = FakeKubectl(forbid={"apply": QUOTA_MESSAGE, "delete": DELETE_FORBIDDEN})
    evidence = executor(fake).run_repro(repo, "true", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "ResourceQuota setup failure" in evidence.detail and "tiny-pods" in evidence.detail
    assert "cleanup" in evidence.detail and "left behind" in evidence.detail


# --- deploy/k8s: namespace, ServiceAccount, Role, RoleBinding --------------------------------

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "k8s"

# Exactly what the executor's kubectl calls need, per (apiGroup, resource):
#   apply -f - (client-side)              -> get (does it exist?), create; never patch: names are fresh
#   delete ... -l <run> --wait=true       -> list (resolve the selector), delete, watch (wait)
#   get pods -l <run> -o json             -> pods: list
#   logs job/<name>                       -> jobs: get, pods: list, pods/log: get
#   get events (Job FailedCreate, #94)     -> events: get, list
EXPECTED_RULES = {
    ("", "configmaps"): {"get", "create", "list", "delete", "watch"},
    ("networking.k8s.io", "networkpolicies"): {"get", "create", "list", "delete", "watch"},
    ("batch", "jobs"): {"get", "create", "list", "delete", "watch"},
    ("", "pods"): {"list"},
    ("", "pods/log"): {"get"},
    ("", "events"): {"get", "list"},
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
