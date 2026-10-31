"""Scope cluster cleanup checks to the runs a test itself started.

The namespaces are shared (other test sessions, other agents, leftovers of a crashed run), so
"no run objects in the namespace" is not something a test can assert. Instead, wrap the
executor's injectable `run` callable, record the run names in every `kubectl apply` it sends
(each List item carries the label claimcheck.dev/run=<name>), and check that exactly those
objects are gone afterwards.
"""

import json
import subprocess
import threading

from claimcheck import k8s

KINDS = "jobs,configmaps,networkpolicies"


class RecordingRun:
    """A drop-in for subprocess.run that remembers the run names the executor applies. Thread-safe."""

    def __init__(self, run=subprocess.run):
        self._run = run
        self._lock = threading.Lock()
        self._names: list[str] = []

    def __call__(self, command, *args, input=None, **kw):
        if "apply" in command and input:
            items = json.loads(input).get("items", [])
            names = {item["metadata"]["labels"][k8s.RUN_LABEL] for item in items
                     if k8s.RUN_LABEL in item.get("metadata", {}).get("labels", {})}
            with self._lock:
                self._names += sorted(n for n in names if n not in self._names)
        return self._run(command, *args, input=input, **kw)

    @property
    def names(self) -> list[str]:
        with self._lock:
            return list(self._names)


def left_behind(kubectl, namespace: str, names: list[str]) -> list[str]:
    """The objects of these runs that still exist. Fails loudly if it cannot tell."""
    assert names, "the executor applied no run objects, so there is nothing to check"
    selector = f"{k8s.RUN_LABEL} in ({','.join(names)})"
    done = kubectl("get", KINDS, "-n", namespace, "-l", selector, "-o", "name")
    assert done.returncode == 0, done.stderr
    return done.stdout.split()
