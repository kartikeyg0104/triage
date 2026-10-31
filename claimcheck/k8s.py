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
"""

import base64
import dataclasses
import io
import json
import re
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import NamedTuple

from .models import Claim, Evidence, Status
from .sandbox import classify

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


def _meta(name: str) -> dict:
    return {"name": name, "labels": {"app.kubernetes.io/name": "claimcheck", RUN_LABEL: name}}


def pack_repo(repo: Path) -> bytes:
    def skip(info: tarfile.TarInfo):
        if set(Path(info.name).parts) & _SKIP:
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(repo, arcname="repo", filter=skip)
    return buffer.getvalue()


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


class KubernetesExecutor:
    def __init__(self, namespace: str = "claimcheck", image: str = "python:3.12-slim", runtime_class: str | None = None,
                 context: str | None = None, deadline: int | None = None, poll_interval: float = 2.0,
                 require_network_isolation: bool = True, impersonate: str | None = None, run=subprocess.run):
        """`impersonate` runs every kubectl call as that user (`kubectl --as`), for example
        `system:serviceaccount:claimcheck:claimcheck-executor` from deploy/k8s."""
        self.namespace, self.image, self.runtime_class = namespace, image, runtime_class
        self.context, self.deadline, self.poll_interval, self._run = context, deadline, poll_interval, run
        self.require_network_isolation, self.impersonate = require_network_isolation, impersonate
        self._isolated: bool | None = None
        self._probe_denied: RBACDenied | None = None

    def _kubectl(self, verb: str, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
        command = ["kubectl", verb, *args, "-n", self.namespace]
        if self.context:
            command += ["--context", self.context]
        if self.impersonate:
            command += ["--as", self.impersonate]
        return self._run(command, input=input, capture_output=True, text=True, timeout=120)

    def _kubectl_or_denied(self, verb: str, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
        """Like _kubectl, but an RBAC refusal raises instead of looking like an empty answer."""
        done = self._kubectl(verb, *args, input=input)
        if done.returncode != 0 and (denials := rbac_denials(done.stderr or "")):
            raise RBACDenied(denials)
        return done

    def _pod_state(self, name: str) -> dict:
        done = self._kubectl_or_denied("get", "pods", "-l", f"{RUN_LABEL}={name}", "-o", "json")
        if done.returncode != 0:
            return {}
        for pod in json.loads(done.stdout or "{}").get("items", []):
            for status in pod.get("status", {}).get("containerStatuses", []) or []:
                return status.get("state", {})
        return {}

    def network_isolated(self) -> bool:
        """Run the probe pod once under a deny-all policy. Cached for this executor, RBAC refusals too."""
        if self._isolated is None and self._probe_denied is None:
            with tempfile.TemporaryDirectory(prefix="claimcheck-probe-") as probe_dir:
                Path(probe_dir, "probe.py").write_text(PROBE_SCRIPT)
                claim = Claim("network", "the cluster blocks pod egress")
                try:
                    result = self._run_pod(Path(probe_dir), "python3 probe.py", PROBE_OK, 60, claim, "network probe")
                except RBACDenied as error:
                    self._probe_denied = error  # egress stays unknown: RBAC, not the network, stopped the probe
                    return False
            self._isolated = result.status is Status.CONFIRMED
        return bool(self._isolated)

    def _source(self, command: str) -> str:
        network = {True: "egress blocked, verified", False: "egress open", None: "egress not verified"}[self._isolated]
        return f"kubernetes[{self.namespace}/{self.runtime_class or 'default runtime'}] ({network}): {command}"

    def _rbac_failure(self, claim: Claim, error: RBACDenied, stage: str, command: str) -> Evidence:
        return Evidence(claim, Status.UNVERIFIED,
                        f"RBAC setup failure during the {stage}, so nothing was verified: kubectl was forbidden: {error}. "
                        f"Grant the missing permissions in the executor's Role (deploy/k8s) or run as an identity that has them",
                        self._source(command))

    def run_repro(self, repo: Path, command: str, expect: str, timeout: int = 30) -> Evidence:
        claim = Claim("repro", f"`{command}` reproduces the reported failure", command)
        if self.require_network_isolation and not self.network_isolated():
            if self._probe_denied is not None:
                return self._rbac_failure(claim, self._probe_denied, "network probe (the repro was not run)", command)
            return Evidence(claim, Status.UNVERIFIED,
                            "refused to run: this cluster does not enforce NetworkPolicy (a probe pod under a deny-all "
                            "policy could reach the network or could not run), so the pod would not be isolated",
                            self._source(command))
        try:
            return self._run_pod(repo, command, expect, timeout, claim, self._source(command))
        except RBACDenied as error:
            return self._rbac_failure(claim, error, "repro", command)

    def _run_pod(self, repo: Path, command: str, expect: str, timeout: int, claim: Claim, source: str) -> Evidence:
        """Raises RBACDenied if the identity may not run it; a refused cleanup is noted in the detail."""
        name = f"claimcheck-{uuid.uuid4().hex[:10]}"
        try:
            configmap = build_configmap(name, pack_repo(repo))
        except RepoTooLarge as error:
            return Evidence(claim, Status.UNVERIFIED, f"not run: {error} (too large for a ConfigMap)", source)
        objects = {"apiVersion": "v1", "kind": "List",
                   "items": [configmap, build_network_policy(name),
                             build_job(name, self.image, command, timeout, self.runtime_class)]}
        denied: RBACDenied | None = None
        try:
            evidence = self._execute(name, objects, claim, expect, timeout, source)
        except RBACDenied as error:
            denied = error
        finally:
            cleanup = self._kubectl("delete", "jobs,configmaps,networkpolicies", "-l", f"{RUN_LABEL}={name}",
                                    "--ignore-not-found", "--wait=true")
        left = rbac_denials(cleanup.stderr or "") if cleanup.returncode != 0 else []
        if denied is not None:
            raise RBACDenied(denied.denials + [d for d in left if d not in denied.denials])
        if left:
            note = (f"; cleanup was forbidden by RBAC ({describe_denials(left)}), so this run's objects "
                    f"(label {RUN_LABEL}={name}) may be left behind")
            evidence = dataclasses.replace(evidence, detail=evidence.detail + note)
        return evidence

    def _execute(self, name, objects, claim, expect, timeout, source) -> Evidence:
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
            if time.monotonic() >= end:
                return Evidence(claim, Status.UNVERIFIED, f"the pod did not finish in time (last state: {state or 'not created'})", source)
            time.sleep(self.poll_interval)
        exit_code = state["terminated"].get("exitCode", -1)
        logs = self._kubectl_or_denied("logs", f"job/{name}").stdout
        output = logs.replace("/work/src/repo", "<repo>").strip()
        return classify(claim, exit_code, output, expect, source)
