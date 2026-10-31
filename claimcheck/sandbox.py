"""Run a reproduction command with no network, no secrets and a throwaway copy of the repo.

Isolation is real or the run is refused: if the OS gives us no way to block the network,
the claim stays UNVERIFIED instead of running untrusted code unprotected. The sandbox does not
block file reads, so the copy itself must not reach outside the repo (see repo_copy).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from .models import Claim, Evidence, Status
from .repo_copy import RepoTooLarge, copy_repo, describe_skipped

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


def run_repro(repo: Path, command: str, expect: str, timeout: int = 30) -> Evidence:
    """Run `command` in a copy of `repo`. The bug is 'reproduced' if the run fails (non-zero)
    and, when `expect` is given, its output contains that text."""
    claim = Claim("repro", f"`{command}` reproduces the reported failure", command)
    isolation = detect_isolation()
    if isolation is None:
        return Evidence(claim, Status.UNVERIFIED, "no network isolation available on this machine; refused to run", "sandbox")
    name, prefix = isolation
    with tempfile.TemporaryDirectory(prefix="claimcheck-") as work:
        target = Path(work) / "repo"
        try:
            note = describe_skipped(copy_repo(repo, target))
        except RepoTooLarge as exc:
            return Evidence(claim, Status.UNVERIFIED, str(exc), "sandbox")
        try:
            proc = subprocess.run(
                prefix + ["sh", "-c", command],
                cwd=target,
                env=_clean_env(work),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return _with_note(Evidence(claim, Status.UNVERIFIED, f"timed out after {timeout}s", f"{name}: {command}"), note)
    output = (proc.stdout + proc.stderr).replace(str(target.resolve()), "<repo>").replace(str(target), "<repo>").strip()
    return _with_note(classify(claim, proc.returncode, output, expect, f"{name}: {command}"), note)


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
