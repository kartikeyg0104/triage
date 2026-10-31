"""Check a fix claim across two revisions: the repro must fail on base and pass on head.

Each revision is checked out as a detached git worktree in a temp dir, so the user's checkout,
index and working tree are never touched, and uncommitted edits cannot leak into either side.
The worktrees are always removed afterwards. The repro itself runs in whatever executor is given
(local sandbox or Kubernetes), which judges each side exactly as it judges a single repro. The two
sides run concurrently, so the executor must be safe to call from two threads at once (the local
sandbox copies each side to its own temp dir; the Kubernetes executor gives each Job a unique name).
"""

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .models import Claim, Evidence, Status

RunRepro = Callable[..., Evidence]


class SetupFailure(Exception):
    pass


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    command = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))


def resolve(repo: Path, ref: str) -> str | None:
    """Full SHA of the commit `ref` names, or None. A ref starting with '-' could be read as an option."""
    if not ref or ref.startswith("-"):
        return None
    done = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return done.stdout.strip() if done.returncode == 0 else None


@contextmanager
def worktree(repo: Path, sha: str) -> Iterator[Path]:
    """Hooks are disabled: checking out a revision should not run anything.

    `git worktree prune` is repo-wide and would also drop the user's own stale worktrees (say, on an
    unmounted drive), so it is only the fallback for when removing ours failed."""
    parent = Path(tempfile.mkdtemp(prefix="claimcheck-rev-")).resolve()
    path = parent / sha[:12]
    try:
        done = _git(repo, "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", "--quiet", str(path), sha)
        if done.returncode != 0:
            raise SetupFailure(f"could not check out {sha[:12]}: {done.stderr.strip()}")
        yield path
    finally:
        _git(repo, "worktree", "remove", "--force", str(path))
        shutil.rmtree(parent, ignore_errors=True)
        if f"worktree {path}" in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
            _git(repo, "worktree", "prune")


def _run_side(tree: Path | SetupFailure, prefix: str, claim: Claim, command: str, expect: str,
              run_repro: RunRepro, timeout: int) -> Evidence:
    """`tree` is this side's own worktree, or the failure that kept it from being checked out."""
    if isinstance(tree, SetupFailure):
        return Evidence(claim, Status.UNVERIFIED, f"setup failure: {tree}")
    target = tree / prefix
    if not target.is_dir():
        return Evidence(claim, Status.UNVERIFIED,
                        f"setup failure: {prefix.rstrip('/')} does not exist at this revision")
    return run_repro(target, command, expect, timeout)


def _run_both(repo: Path, prefix: str, shas: dict[str, str], claim: Claim, command: str, expect: str,
              run_repro: RunRepro, timeout: int) -> dict[str, Evidence]:
    """Both worktrees are created one after the other (concurrent `git worktree add` on one repo can race
    on .git/worktrees), then both repros run at once, then both worktrees are removed. If a side raises,
    the other side is still waited for before anything is removed, and the first error is re-raised."""
    with ExitStack() as stack:
        trees: dict[str, Path | SetupFailure] = {}
        for side, sha in shas.items():
            try:
                trees[side] = stack.enter_context(worktree(repo, sha))
            except SetupFailure as error:
                trees[side] = error
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="claimcheck-rev") as pool:
            futures = {side: pool.submit(_run_side, trees[side], prefix, claim, command, expect, run_repro, timeout)
                       for side in shas}
        # leaving the pool waited for both sides, so neither worktree is removed while its repro runs
        return {side: future.result() for side, future in futures.items()}


def _decide(base: Evidence, head: Evidence) -> tuple[Status, str]:
    """The executor's per-side status: CONFIRMED = reproduced, REFUTED = exit 0, UNVERIFIED = anything else."""
    if base.status is Status.CONFIRMED and head.status is Status.REFUTED:
        return Status.CONFIRMED, "fix confirmed: the repro fails with the expected text on base and exits 0 on head"
    if base.status is Status.CONFIRMED and head.status is Status.CONFIRMED:
        return Status.REFUTED, "not fixed: head still fails with the expected text"
    if base.status is Status.REFUTED:
        return Status.UNVERIFIED, "bug not reproduced on base (the repro exits 0 there), so there is no failure for head to fix"
    if base.status is Status.UNVERIFIED:
        return Status.UNVERIFIED, "inconclusive: no clean reproduction on base"
    return Status.UNVERIFIED, "inconclusive: base reproduces the bug but head gave no clean result"


def verify_fix(repo: Path, base: str, head: str, command: str, expect: str, run_repro: RunRepro,
               timeout: int = 30) -> Evidence:
    """CONFIRMED only if base fails with `expect` and head exits 0; REFUTED only if both fail with it."""
    claim = Claim("fix", f"`{head}` fixes the failure `{command}` shows on `{base}`", command)
    refs = {"base": base, "head": head}
    prefix = _git(repo, "rev-parse", "--show-prefix")
    if prefix.returncode != 0:
        return Evidence(claim, Status.UNVERIFIED, f"setup failure: not a git checkout: {prefix.stderr.strip()}",
                        f"base {base} → head {head}")
    shas = {side: resolve(repo, ref) for side, ref in refs.items()}
    source = " → ".join(f"{side} {shas[side][:12] if shas[side] else refs[side]}" for side in refs)
    for side in refs:
        if shas[side] is None:
            return Evidence(claim, Status.UNVERIFIED, f"setup failure: {side} ref {refs[side]!r} does not name a commit here",
                            source)
    runs = _run_both(repo, prefix.stdout.strip(), shas, claim, command, expect, run_repro, timeout)
    status, headline = _decide(runs["base"], runs["head"])
    sides = ". ".join(f"{side} {shas[side]}: {runs[side].detail}" for side in refs)
    executor = runs["head"].source or runs["base"].source
    return Evidence(claim, status, f"{headline}. {sides}", f"{source}; {executor}" if executor else source)
