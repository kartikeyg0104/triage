"""Run a reproduction command with no network, no secrets and a throwaway copy of the repo.

Isolation is real or the run is refused: if the OS gives us no way to block the network,
the claim stays UNVERIFIED instead of running untrusted code unprotected.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .models import Claim, Evidence, Status

_MAC_PROFILE = "(version 1)(allow default)(deny network*)"
_IGNORE = shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".venv")


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
        shutil.copytree(repo, target, ignore=_IGNORE)
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
            return Evidence(claim, Status.UNVERIFIED, f"timed out after {timeout}s", f"{name}: {command}")
    output = (proc.stdout + proc.stderr).replace(str(target.resolve()), "<repo>").replace(str(target), "<repo>").strip()
    source = f"{name}: {command}"
    if proc.returncode != 0 and expect in output:
        return Evidence(claim, Status.CONFIRMED, f"reproduced: exit {proc.returncode}; {_tail(output)}", source)
    return Evidence(claim, Status.REFUTED, f"not reproduced: exit {proc.returncode}; {_tail(output)}", source)


def _tail(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text[-limit:] if text else "no output"
