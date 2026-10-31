"""Run a reproduction command with no network, no secrets and a throwaway copy of the repo.

Isolation is real or the run is refused: if the OS gives us no way to block the network,
the claim stays UNVERIFIED instead of running untrusted code unprotected. The sandbox does not
block file reads, so the copy itself must not reach outside the repo (see repo_copy).

The repro also runs under resource limits (CPU seconds, file size, open files, processes and,
where the OS honours it, address space), so it cannot fork-bomb, eat memory or fill the disk.
A run stopped by a limit says nothing about the bug, so it is UNVERIFIED, never a verdict.
"""

import functools
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from .models import Claim, Evidence, Status
from .repo_copy import copy_repo, describe_skipped

_MAC_PROFILE = "(version 1)(allow default)(deny network*)"


def detect_isolation() -> tuple[str, list[str]] | None:
    """Return (name, command prefix) for a working no-network wrapper, or None."""
    candidates = []
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        candidates.append(("sandbox-exec", ["sandbox-exec", "-p", _MAC_PROFILE]))
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        candidates.append(("unshare-net", ["unshare", "-rn"]))
    for name, prefix in candidates:
        try:
            ok = subprocess.run(prefix + ["true"], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            return name, prefix
    return None


def _clean_env(workdir: str) -> dict[str, str]:
    """Only what a build needs. No tokens, no cloud credentials, no inherited HOME."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": workdir, "LANG": "C.UTF-8"}


# Ceilings for the repro's process tree: generous for a build or a test run, small for a host.
# Tests lower them with monkeypatch.setitem.
LIMITS = {
    "cpu_seconds": 60,          # per process: SIGXCPU, then SIGKILL two seconds later
    "memory_bytes": 4 << 30,    # address space, only where the OS enforces RLIMIT_AS
    "file_bytes": 1 << 30,      # largest file the repro may write: SIGXFSZ beyond it
    "open_files": 1024,
    "extra_processes": 256,     # RLIMIT_NPROC counts all of this user's processes, so the
                                # ceiling is "what the user runs now + this", never below it
}


@functools.cache
def memory_limit_enforced() -> tuple[bool, str]:
    """Probe whether RLIMIT_AS stops an allocation here. macOS refuses to lower it at all."""
    probe = ("import resource, sys\n"
             "try:\n    resource.setrlimit(resource.RLIMIT_AS, (256 << 20, 256 << 20))\n"
             "except (ValueError, OSError) as e:\n    print('setrlimit(RLIMIT_AS) rejected:', e); sys.exit(2)\n"
             "try:\n    bytearray(1 << 30)\n"
             "except MemoryError:\n    sys.exit(0)\n"
             "print('a 1 GiB allocation succeeded under a 256 MiB RLIMIT_AS'); sys.exit(3)\n")
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"probe failed: {e}"
    if proc.returncode == 0:
        return True, "RLIMIT_AS enforced"
    return False, (proc.stdout + proc.stderr).strip() or f"probe exit {proc.returncode}"


def _user_process_count() -> int | None:
    try:
        out = subprocess.run(["ps", "-A", "-o", "uid="], capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.split().count(str(os.getuid()))


def _capped(which: int, wanted: int) -> int:
    """Never raise a limit the host already set lower."""
    return min([wanted] + [v for v in resource.getrlimit(which) if v != resource.RLIM_INFINITY])


def _plan_limits() -> tuple[list[tuple[int, int, int]], list[str], list[str]]:
    """Return (setrlimit calls, how to show them, skipped limits and why). Planned in the parent
    so a limit we cannot apply is recorded instead of silently missing."""
    cpu = _capped(resource.RLIMIT_CPU, LIMITS["cpu_seconds"])
    fsize = _capped(resource.RLIMIT_FSIZE, LIMITS["file_bytes"])
    nofile = _capped(resource.RLIMIT_NOFILE, LIMITS["open_files"])
    plan = [(resource.RLIMIT_CPU, cpu, _capped(resource.RLIMIT_CPU, cpu + 2)),
            (resource.RLIMIT_FSIZE, fsize, fsize),
            (resource.RLIMIT_NOFILE, nofile, nofile),
            (resource.RLIMIT_CORE, 0, 0)]
    shown = [f"cpu {cpu}s", f"file {fsize >> 20} MiB", f"open files {nofile}"]
    skipped = []
    enforced, reason = memory_limit_enforced()
    if enforced:
        memory = _capped(resource.RLIMIT_AS, LIMITS["memory_bytes"])
        plan.append((resource.RLIMIT_AS, memory, memory))
        shown.append(f"memory {memory >> 20} MiB")
    else:
        skipped.append(f"memory limit not enforced on this OS: {reason}")
    if os.geteuid() == 0:
        skipped.append("process limit skipped: RLIMIT_NPROC does not apply to root")
    elif (running := _user_process_count()) is None:
        skipped.append("process limit skipped: could not count this user's processes")
    else:
        procs = _capped(resource.RLIMIT_NPROC, running + LIMITS["extra_processes"])
        plan.append((resource.RLIMIT_NPROC, procs, procs))
        shown.append(f"processes {procs} for this user ({running} already running)")
    return plan, shown, skipped


def _apply_limits(plan: list[tuple[int, int, int]]) -> None:
    """preexec_fn: runs in the child between fork and exec, so only the repro is limited."""
    for which, soft, hard in plan:
        resource.setrlimit(which, (soft, hard))


def _kill_group(proc: subprocess.Popen) -> None:
    """The repro runs in its own session: take down whatever it left behind."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_repro(repo: Path, command: str, expect: str, timeout: int = 30) -> Evidence:
    """Run `command` in a copy of `repo`. The bug is 'reproduced' if the run fails (non-zero)
    and, when `expect` is given, its output contains that text."""
    claim = Claim("repro", f"`{command}` reproduces the reported failure", command)
    isolation = detect_isolation()
    if isolation is None:
        return Evidence(claim, Status.UNVERIFIED, "no network isolation available on this machine; refused to run", "sandbox")
    name, prefix = isolation
    source = f"{name}: {command}"
    plan, shown, skipped = _plan_limits()
    # Said when the run was cut short, where a missing limit matters; a normal run's report stays as it was.
    limits = "limits: " + ", ".join(shown) + "".join(f"; {s}" for s in skipped)
    with tempfile.TemporaryDirectory(prefix="claimcheck-") as work:
        target = Path(work) / "repo"
        note = describe_skipped(copy_repo(repo, target))
        try:
            proc = subprocess.Popen(
                prefix + ["sh", "-c", command],
                cwd=target,
                env=_clean_env(work),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                preexec_fn=functools.partial(_apply_limits, plan),
            )
        except subprocess.SubprocessError as e:
            return _with_note(Evidence(claim, Status.UNVERIFIED, f"could not apply resource limits ({e}); refused to run", source), note)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()
            return _with_note(Evidence(claim, Status.UNVERIFIED, f"timed out after {timeout}s; {limits}", source), note)
        finally:
            _kill_group(proc)
    output = (stdout + stderr).replace(str(target.resolve()), "<repo>").replace(str(target), "<repo>").strip()
    if hit := limit_hit(proc.returncode, output):
        return _with_note(Evidence(claim, Status.UNVERIFIED,
                                   f"stopped by a resource limit ({hit}), not a reproduction: exit {proc.returncode}; "
                                   f"{limits}; {_tail(output)}", source, "observed"), note)
    return _with_note(classify(claim, proc.returncode, output, expect, source), note)


_LIMIT_SIGNALS = {signal.SIGXCPU: "CPU time, SIGXCPU", signal.SIGXFSZ: "file size, SIGXFSZ", signal.SIGKILL: "SIGKILL"}
_LIMIT_MARKERS = ("Resource temporarily unavailable", "MemoryError", "Cannot allocate memory", "std::bad_alloc",
                  "File too large", "Too many open files", "Cputime limit exceeded", "Filesize limit exceeded")


def limit_hit(returncode: int, output: str) -> str | None:
    """What stopped the run, if it looks like one of our limits did. A signal shows up as -N
    from a direct child or 128+N through the shell."""
    for sig, what in _LIMIT_SIGNALS.items():
        if returncode in (-sig, 128 + sig):
            return what
    return next((marker for marker in _LIMIT_MARKERS if marker in output), None)


def _with_note(evidence: Evidence, note: str) -> Evidence:
    """Say what the copy left out, so a run without those files is not mistaken for a full one."""
    return replace(evidence, detail=f"{evidence.detail} (note: {note})") if note else evidence


_SETUP_MARKERS = ("command not found", "ModuleNotFoundError", "ImportError", "Cannot find module",
                  "No such file or directory", "Permission denied", "not recognized as")


def classify(claim: Claim, returncode: int, output: str, expect: str, source: str) -> Evidence:
    """A non-zero exit alone proves nothing: the command may simply be broken here."""
    shown = _tail(output)
    if returncode in (126, 127) or any(marker in output for marker in _SETUP_MARKERS):
        return Evidence(claim, Status.UNVERIFIED, f"setup or environment failure, not a reproduction: exit {returncode}; {shown}", source, "observed")
    if returncode == 0:
        return Evidence(claim, Status.REFUTED, f"not reproduced: the command succeeded (exit 0); {shown}", source, "observed")
    if not expect:
        return Evidence(claim, Status.UNVERIFIED,
                        f"the command failed (exit {returncode}) but no expected failure text was given, so this does not show the reported bug; {shown}",
                        source, "observed")
    if expect in output:
        return Evidence(claim, Status.CONFIRMED, f"reproduced: exit {returncode} and the output contains the expected text; {shown}", source, "observed")
    return Evidence(claim, Status.UNVERIFIED, f"the command failed (exit {returncode}) with a different message than expected; {shown}", source, "observed")


def _tail(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text[-limit:] if text else "no output"
