"""Run a reproduction command as a one-shot, locked-down Kubernetes Job.

The repo travels in a ConfigMap (so the pod needs no network), a deny-all NetworkPolicy is
created for the pod, and every object is deleted afterwards. The pod gets no service account
token, no inherited environment, a read-only root filesystem and no Linux capabilities.

A NetworkPolicy only isolates the pod if the cluster's CNI enforces it (kind's default CNI does
not). So before the first run the executor starts a probe pod under the same policy; if the probe
can reach the API server or the internet, it refuses to run anything.

A RuntimeClass such as gVisor can be requested, but this module does not check that the
cluster really has it; the report names the runtime that was asked for.

If RBAC refuses a kubectl call (the identity lacks a permission), the run stops and is reported as
an RBAC setup failure naming the refused verb and resource, never as a network problem or a verdict.

ResourceQuota and LimitRange admission denials are setup failures too. Job FailedCreate events
are checked while waiting for a pod; this needs permission to list events. Without that permission,
pod polling still works, but a controller admission denial may only be reported as a timeout.
"""

import base64
import dataclasses
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import NamedTuple

from .models import Claim, Evidence, Status
from .repo_copy import DIR, FILE, LINK, Decision, describe_skipped, walk
from .sandbox import _with_note, classify

RUN_LABEL = "claimcheck.dev/run"
MAX_REPO_BYTES = 900_000  # ConfigMaps are capped at 1 MiB
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv"}
_FATAL_WAITING = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CreateContainerConfigError",
                  "CreateContainerError", "RunContainerError"}

# Unpack the repo into the writable work dir, then run the user's command, which arrives as "$0"
# so it is never pasted into this script.
_ENTRYPOINT = 'set -e; mkdir -p /work/src; tar -xzf /input/repo.tgz -C /work/src; cd /work/src/repo; exec sh -c "$0"'


PROBE_OK = "CLAIMCHECK-EGRESS-BLOCKED"
PROBE_SCRIPT = f"""import os, socket, sys
api = (os.environ.get("KUBERNETES_SERVICE_HOST", "10.96.0.1"), int(os.environ.get("KUBERNETES_SERVICE_PORT", "443")))
for host, port in [api, ("1.1.1.1", 53), ("8.8.8.8", 443)]:
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        print("CONNECTED", host, port)
        sys.exit(1)
    except OSError:
        pass
print("{PROBE_OK}")
sys.exit(1)
"""


class RepoTooLarge(ValueError):
    pass


# The API server's RBAC refusal, as kubectl prints it:
#   ... is forbidden: User "X" cannot get resource "pods/log" in API group "" in the namespace "ns"
_FORBIDDEN = re.compile(r'is forbidden: User "(?P<user>[^"]*)" cannot (?P<verb>\S+) resource "(?P<resource>[^"]+)"'
                        r'(?: in API group "(?P<group>[^"]*)")?(?: in the namespace "(?P<namespace>[^"]+)")?')


class Denial(NamedTuple):
    user: str
    verb: str
    resource: str  # kubectl's spelling: "configmaps", "jobs.batch", "pods/log"
    namespace: str | None  # None: cluster scope


def rbac_denials(stderr: str) -> list[Denial]:
    """Every RBAC refusal in kubectl's stderr. Other errors, such as Pod Security's, are not RBAC."""
    found = []
    for match in _FORBIDDEN.finditer(stderr):
        name, slash, subresource = match["resource"].partition("/")
        resource = (f"{name}.{match['group']}" if match["group"] else name) + slash + subresource
        denial = Denial(match["user"], match["verb"], resource, match["namespace"])
        if denial not in found:
            found.append(denial)
    return found


def describe_denials(denials: list[Denial]) -> str:
    by_scope: dict[tuple[str, str | None], list[str]] = {}
    for d in denials:
        by_scope.setdefault((d.user, d.namespace), []).append(f"{d.verb} {d.resource}")
    return "; ".join(f'User "{user}" cannot {", ".join(actions)} '
                     + (f'in namespace "{namespace}"' if namespace else "at the cluster scope")
                     for (user, namespace), actions in by_scope.items())


class RBACDenied(Exception):
    """A kubectl call was refused by RBAC; the message names what was refused."""

    def __init__(self, denials: list[Denial]):
        super().__init__(describe_denials(denials))
        self.denials = denials


class AdmissionDenied(Exception):
    """Keep admission's constraint and quota name, rather than guessing why no pod exists."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def admission_denial(message: str) -> AdmissionDenied | None:
    """Recognize admission's quota and LimitRange messages, not every Forbidden response."""
    if re.search(r'\b(?:exceeded|failed) quota:\s*[\w.-]+', message):
        return AdmissionDenied("ResourceQuota", message.strip())
    if re.search(r'\b(?:maximum|minimum) \S+ usage per (?:Container|Pod|PersistentVolumeClaim) is\b'
                 r'|\b\S+ max limit to request ratio per (?:Container|Pod) is\b', message):
        return AdmissionDenied("LimitRange", message.strip())
    return None


def _meta(name: str) -> dict:
    return {"name": name, "labels": {"app.kubernetes.io/name": "claimcheck", RUN_LABEL: name}}


def _entry(name: str, kind: bytes, st: os.stat_result | None = None) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = stat.S_IMODE(st.st_mode) if st else 0o777
    info.mtime = int(st.st_mtime) if st else 0
    return info  # uid/gid 0 and no user or group names: nothing about this machine


def pack_repo(repo: Path) -> tuple[bytes, list[Decision]]:
    """The repo as a gzipped tar under `repo/`, built from repo_copy.walk like the local sandbox's
    copy: files as regular files, links inside the repo as relative links, and links that leave it
    (absolute, `..`), unresolvable links and special files left out. Returns (tarball, left out)."""
    root = Path(os.path.realpath(repo))
    skipped = []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.addfile(_entry("repo", tarfile.DIRTYPE, os.lstat(root)))
        for decision in walk(root, frozenset(_SKIP)):
            name, path = f"repo/{decision.path}", root / decision.path
            if decision.kind == DIR:
                tar.addfile(_entry(name, tarfile.DIRTYPE, os.lstat(path)))
            elif decision.kind == FILE:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))  # never a link swapped in later
                with os.fdopen(fd, "rb") as handle:
                    st = os.fstat(fd)
                    info = _entry(name, tarfile.REGTYPE, st)
                    info.size = st.st_size
                    tar.addfile(info, handle)
            elif decision.kind == LINK:
                info = _entry(name, tarfile.SYMTYPE)
                info.linkname = decision.target
                tar.addfile(info)
            else:
                skipped.append(decision)
    return buffer.getvalue(), skipped


def build_configmap(name: str, tarball: bytes) -> dict:
    if len(tarball) > MAX_REPO_BYTES:
        raise RepoTooLarge(f"repo archive is {len(tarball)} bytes; the limit is {MAX_REPO_BYTES}")
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta(name),
            "binaryData": {"repo.tgz": base64.b64encode(tarball).decode()}}


def build_network_policy(name: str) -> dict:
    """No ingress or egress rules with both policy types listed means: deny everything."""
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": _meta(name),
            "spec": {"podSelector": {"matchLabels": {RUN_LABEL: name}}, "policyTypes": ["Ingress", "Egress"]}}


def build_job(name: str, image: str, command: str, timeout: int, runtime_class: str | None = None) -> dict:
    pod = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "repro",
            "image": image,
            "command": ["sh", "-c", _ENTRYPOINT],
            "args": [command],
            "env": [{"name": "HOME", "value": "/work"}, {"name": "LANG", "value": "C.UTF-8"},
                    {"name": "PATH", "value": "/usr/local/bin:/usr/bin:/bin"}],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "privileged": False, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                          "limits": {"cpu": "500m", "memory": "256Mi"}},
            "volumeMounts": [{"name": "input", "mountPath": "/input", "readOnly": True},
                             {"name": "work", "mountPath": "/work"},
                             {"name": "tmp", "mountPath": "/tmp"}],
        }],
        "volumes": [{"name": "input", "configMap": {"name": name}},
                    {"name": "work", "emptyDir": {"sizeLimit": "64Mi"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}}],
    }
    if runtime_class:
        pod["runtimeClassName"] = runtime_class
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": _meta(name),
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": timeout, "ttlSecondsAfterFinished": 300,
                     "template": {"metadata": {"labels": _meta(name)["labels"]}, "spec": pod}}}


def _image_pinned(image: str) -> bool:
    """A digest marker alone is not a pin: require a complete SHA-256 reference."""
    return re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image) is not None


class KubernetesExecutor:
    def __init__(self, namespace: str = "claimcheck", image: str = "python:3.12-slim", runtime_class: str | None = None,
                 context: str | None = None, deadline: int | None = None, poll_interval: float = 2.0,
                 require_network_isolation: bool = True, impersonate: str | None = None, run=subprocess.run,
                 require_digest: bool = False):
        """`impersonate` runs every kubectl call as that user (`kubectl --as`), for example
        `system:serviceaccount:claimcheck:claimcheck-executor` from deploy/k8s."""
        self.namespace, self.image, self.runtime_class = namespace, image, runtime_class
        self.context, self.deadline, self.poll_interval, self._run = context, deadline, poll_interval, run
        self.require_network_isolation, self.impersonate = require_network_isolation, impersonate
        self.require_digest = require_digest
        self._isolated: bool | None = None
        self._probe_denied: RBACDenied | AdmissionDenied | None = None

    def _kubectl(self, verb: str, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
        command = ["kubectl", verb, *args, "-n", self.namespace]
        if self.context:
            command += ["--context", self.context]
        if self.impersonate:
            command += ["--as", self.impersonate]
        return self._run(command, input=input, capture_output=True, text=True, timeout=120)

    def _kubectl_or_denied(self, verb: str, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
        """Setup refusals raise instead of looking like an empty answer."""
        done = self._kubectl(verb, *args, input=input)
        if done.returncode != 0:
            if denials := rbac_denials(done.stderr or ""):
                raise RBACDenied(denials)
            if denial := admission_denial(done.stderr or ""):
                raise denial
        return done

    def _pod_state(self, name: str) -> dict:
        done = self._kubectl_or_denied("get", "pods", "-l", f"{RUN_LABEL}={name}", "-o", "json")
        if done.returncode != 0:
            return {}
        for pod in json.loads(done.stdout or "{}").get("items", []):
            for status in pod.get("status", {}).get("containerStatuses", []) or []:
                return status.get("state", {})
        return {}

    def _check_admission_events(self, name: str) -> None:
        """The Job controller can be refused a pod even when applying the Job succeeded."""
        done = self._kubectl("get", "events", "--field-selector",
                             f"involvedObject.kind=Job,involvedObject.name={name},reason=FailedCreate", "-o", "json")
        if done.returncode != 0:
            return  # event access is optional; the shipped Role permits only pod polling
        for event in json.loads(done.stdout or "{}").get("items", []):
            obj = event.get("involvedObject", {})
            if obj.get("kind") == "Job" and obj.get("name") == name and event.get("reason") == "FailedCreate":
                if denial := admission_denial(event.get("message", "")):
                    raise denial

    def network_isolated(self) -> bool:
        """Run the probe once under a deny-all policy. Setup refusals are cached too."""
        if self._isolated is None and self._probe_denied is None:
            with tempfile.TemporaryDirectory(prefix="claimcheck-probe-") as probe_dir:
                Path(probe_dir, "probe.py").write_text(PROBE_SCRIPT)
                claim = Claim("network", "the cluster blocks pod egress")
                try:
                    result = self._run_pod(Path(probe_dir), "python3 probe.py", PROBE_OK, 60, claim, "network probe")
                except (RBACDenied, AdmissionDenied) as error:
                    self._probe_denied = error  # a setup refusal leaves egress unknown
                    return False
            self._isolated = result.status is Status.CONFIRMED
        return bool(self._isolated)

    def _source(self, command: str) -> str:
        network = {True: "egress blocked, verified", False: "egress open", None: "egress not verified"}[self._isolated]
        pinning = "image pinned by digest" if _image_pinned(self.image) else "image not pinned by digest"
        return (f"kubernetes[{self.namespace}/{self.runtime_class or 'default runtime'}] ({network}) "
                f"(image {self.image}; {pinning}): {command}")

    def _rbac_failure(self, claim: Claim, error: RBACDenied, stage: str, command: str) -> Evidence:
        return Evidence(claim, Status.UNVERIFIED,
                        f"RBAC setup failure during the {stage}, so nothing was verified: kubectl was forbidden: {error}. "
                        f"Grant the missing permissions in the executor's Role (deploy/k8s) or run as an identity that has them",
                        self._source(command))

    def _admission_failure(self, claim: Claim, error: AdmissionDenied, stage: str, command: str) -> Evidence:
        return Evidence(claim, Status.UNVERIFIED,
                        f"{error.category} setup failure during the {stage}, so nothing was verified: {error}",
                        self._source(command))

    def run_repro(self, repo: Path, command: str, expect: str, timeout: int = 30) -> Evidence:
        """Keep the image warning on every result, without changing its status or basis."""
        evidence = self._run_repro(repo, command, expect, timeout)
        if not _image_pinned(self.image):
            evidence = dataclasses.replace(evidence, detail=evidence.detail + "; image not pinned by digest")
        return evidence

    def _run_repro(self, repo: Path, command: str, expect: str, timeout: int) -> Evidence:
        claim = Claim("repro", f"`{command}` reproduces the reported failure", command)
        if self.require_digest and not _image_pinned(self.image):
            return Evidence(claim, Status.UNVERIFIED,
                            "refused to run: --k8s-require-digest requires an image pinned with @sha256: "
                            "and 64 hexadecimal digits", self._source(command))
        if self.require_network_isolation and not self.network_isolated():
            if isinstance(self._probe_denied, RBACDenied):
                return self._rbac_failure(claim, self._probe_denied, "network probe (the repro was not run)", command)
            if isinstance(self._probe_denied, AdmissionDenied):
                return self._admission_failure(claim, self._probe_denied, "network probe (the repro was not run)", command)
            return Evidence(claim, Status.UNVERIFIED,
                            "refused to run: this cluster does not enforce NetworkPolicy (a probe pod under a deny-all "
                            "policy could reach the network or could not run), so the pod would not be isolated",
                            self._source(command))
        try:
            return self._run_pod(repo, command, expect, timeout, claim, self._source(command))
        except RBACDenied as error:
            return self._rbac_failure(claim, error, "repro", command)
        except AdmissionDenied as error:
            return self._admission_failure(claim, error, "repro", command)

    def _run_pod(self, repo: Path, command: str, expect: str, timeout: int, claim: Claim, source: str) -> Evidence:
        """Setup refusals raise; cleanup still runs and any refused deletion is noted."""
        name = f"claimcheck-{uuid.uuid4().hex[:10]}"
        tarball, skipped = pack_repo(repo)
        left_out = describe_skipped(skipped)  # said in the evidence, so a run without them is not taken for a full one
        try:
            configmap = build_configmap(name, tarball)
        except RepoTooLarge as error:
            return _with_note(Evidence(claim, Status.UNVERIFIED, f"not run: {error} (too large for a ConfigMap)", source),
                              left_out)
        objects = {"apiVersion": "v1", "kind": "List",
                   "items": [configmap, build_network_policy(name),
                             build_job(name, self.image, command, timeout, self.runtime_class)]}
        denied: RBACDenied | AdmissionDenied | None = None
        try:
            evidence = self._execute(name, objects, claim, expect, timeout, source)
        except (RBACDenied, AdmissionDenied) as error:
            denied = error
        finally:
            cleanup = self._kubectl("delete", "jobs,configmaps,networkpolicies", "-l", f"{RUN_LABEL}={name}",
                                    "--ignore-not-found", "--wait=true")
        left = rbac_denials(cleanup.stderr or "") if cleanup.returncode != 0 else []
        if isinstance(denied, RBACDenied):
            raise RBACDenied(denied.denials + [d for d in left if d not in denied.denials])
        note = ""
        if left:
            note = (f"; cleanup was forbidden by RBAC ({describe_denials(left)}), so this run's objects "
                    f"(label {RUN_LABEL}={name}) may be left behind")
        if isinstance(denied, AdmissionDenied):
            raise AdmissionDenied(denied.category, str(denied) + note)
        evidence = dataclasses.replace(evidence, detail=evidence.detail + note)
        return _with_note(evidence, left_out)

    def _execute(self, name: str, objects: dict, claim: Claim, expect: str, timeout: int, source: str) -> Evidence:
        applied = self._kubectl_or_denied("apply", "-f", "-", input=json.dumps(objects))
        if applied.returncode != 0:
            return Evidence(claim, Status.UNVERIFIED, f"setup failure: could not create the Job: {applied.stderr.strip()}", source)
        end = time.monotonic() + (self.deadline if self.deadline is not None else timeout + 120)
        while True:
            state = self._pod_state(name)
            if "terminated" in state:
                break
            reason = state.get("waiting", {}).get("reason", "")
            if reason in _FATAL_WAITING:
                message = state["waiting"].get("message", "")
                return Evidence(claim, Status.UNVERIFIED, f"setup failure: pod could not start ({reason}) {message}".strip(), source)
            if not state:
                self._check_admission_events(name)
            if time.monotonic() >= end:
                return Evidence(claim, Status.UNVERIFIED, f"the pod did not finish in time (last state: {state or 'not created'})", source)
            time.sleep(self.poll_interval)
        exit_code = state["terminated"].get("exitCode", -1)
        logs = self._kubectl_or_denied("logs", f"job/{name}").stdout
        output = logs.replace("/work/src/repo", "<repo>").strip()
        return classify(claim, exit_code, output, expect, source)
