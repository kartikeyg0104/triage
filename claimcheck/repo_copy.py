"""Copy an untrusted repo without letting it reach files outside itself.

A repo can contain `leak -> ~/.ssh/id_rsa`. Following that link while copying would put a
host secret in front of the untrusted repro command, whose output is published as evidence.
So symlinks are never followed during the walk:

- A link whose fully resolved target is inside the repo is recreated as a *relative* link to
  the same place in the copy, so it cannot point back at the original checkout. Links to
  directories are kept as links and never walked, which also rules out copy loops.
- A link that resolves outside the repo (absolute target, `..` escape, a chain or a directory
  link that leads out) or that cannot be resolved (a cycle) is dropped and reported.
- A dangling link that would land inside the repo is kept; it can only ever point into the copy.
- FIFOs, sockets and devices are skipped and reported.

Hard links are copied as ordinary files: a hard link has no "target" to inspect, and its
content is a file the repo owner put in the repo. Git checkouts never contain hard links.

Size caps keep a huge repo from filling the host disk: a single oversized file is skipped and
reported; going over the total-size or entry-count cap aborts the copy with RepoTooLarge.

`walk` only decides; `copy_repo` applies the decisions. Anything that packs a repo (a tarball
for a remote executor, say) should consume `walk` the same way.
"""

import errno
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

IGNORED = frozenset({".git", "node_modules", "__pycache__", ".venv"})

DIR = "dir"
FILE = "file"
LINK = "link"
OUTSIDE_LINK = "outside-link"
BROKEN_LINK = "broken-link"
SPECIAL = "special"
LARGE_FILE = "large-file"
SKIPPED_KINDS = frozenset({OUTSIDE_LINK, BROKEN_LINK, SPECIAL, LARGE_FILE})

# Caps so a huge repo cannot fill the host disk during the copy (see copy_repo).
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILES = 20_000
MAX_FILE_BYTES = 50 * 1024 * 1024


class Decision(NamedTuple):
    """What to do with one entry. `path` is relative to the repo root. `target` is the
    relative link text to write for LINK, and the original link text for a dropped link."""
    path: str
    kind: str
    target: str = ""


def walk(src: str | os.PathLike[str], ignored: frozenset[str] = IGNORED) -> Iterator[Decision]:
    """Yield a decision for every entry under `src`, parents before children."""
    root = os.path.realpath(src)
    yield from _walk_dir(root, "", ignored)


def _walk_dir(root: str, rel_dir: str, ignored: frozenset[str]) -> Iterator[Decision]:
    with os.scandir(os.path.join(root, rel_dir)) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for entry in children:
        if entry.name in ignored:
            continue
        rel = os.path.join(rel_dir, entry.name)
        if entry.is_symlink():
            yield _link_decision(root, rel)
        elif entry.is_dir(follow_symlinks=False):
            yield Decision(rel, DIR)
            yield from _walk_dir(root, rel, ignored)
        elif entry.is_file(follow_symlinks=False):
            yield Decision(rel, FILE)
        else:
            yield Decision(rel, SPECIAL)


def _link_decision(root: str, rel: str) -> Decision:
    """Resolve the whole chain on the real filesystem, then judge only where it ends."""
    link = os.path.join(root, rel)
    text = os.readlink(link)
    try:
        resolved = os.path.realpath(link, strict=True)
    except FileNotFoundError:
        resolved = os.path.realpath(link)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            resolved = os.path.realpath(link)
        else:
            return Decision(rel, BROKEN_LINK, text)
    if not _inside(root, resolved):
        return Decision(rel, OUTSIDE_LINK, text)
    return Decision(rel, LINK, os.path.relpath(resolved, os.path.dirname(link)))


def _inside(root: str, path: str) -> bool:
    return os.path.commonpath([root, path]) == root


class RepoTooLarge(Exception):
    """The repo exceeds the total-size or entry-count cap. The partial copy has been removed,
    so the caller must not run anything: the repro is unverified, not run on part of a repo."""


def copy_repo(src: str | os.PathLike[str], dst: str | os.PathLike[str],
              ignored: frozenset[str] = IGNORED, *,
              max_total_bytes: int | None = None, max_files: int | None = None,
              max_file_bytes: int | None = None) -> list[Decision]:
    """Copy `src` to a new directory `dst` following `walk`. Returns what was left out.

    File permissions are preserved except setuid, setgid and sticky bits.
    A single file over `max_file_bytes` is skipped and reported. Going over `max_total_bytes`
    (bytes of copied files) or `max_files` (directories, files and links created) removes `dst`
    and raises RepoTooLarge. Unset limits use the module defaults, read at call time."""
    max_total_bytes = MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
    max_files = MAX_FILES if max_files is None else max_files
    max_file_bytes = MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes
    root = Path(os.path.realpath(src))
    dst = Path(dst)
    dst.mkdir()
    try:
        return _copy(root, dst, ignored, max_total_bytes, max_files, max_file_bytes)
    except RepoTooLarge:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def _copy(root: Path, dst: Path, ignored: frozenset[str],
          max_total_bytes: int, max_files: int, max_file_bytes: int) -> list[Decision]:
    skipped = []
    total = entries = 0
    for decision in walk(root, ignored):
        if decision.kind not in (DIR, FILE, LINK):
            skipped.append(decision)
            continue
        size = 0
        if decision.kind == FILE:
            size = os.lstat(root / decision.path).st_size
            if size > max_file_bytes:
                skipped.append(Decision(decision.path, LARGE_FILE))
                continue
        entries += 1
        total += size
        if entries > max_files:
            raise RepoTooLarge(f"repo too large to copy safely: more than {max_files} files and directories")
        if total > max_total_bytes:
            raise RepoTooLarge(f"repo too large to copy safely: more than {_size(max_total_bytes)} of files")
        out = dst / decision.path
        if decision.kind == DIR:
            out.mkdir()
        elif decision.kind == FILE:
            shutil.copy2(root / decision.path, out, follow_symlinks=False)
            out.chmod(out.stat().st_mode & 0o777)
        else:
            os.symlink(decision.target, out)
    return skipped


def _size(n: int) -> str:
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{n} bytes"


def describe_skipped(skipped: list[Decision]) -> str:
    """One honest sentence for the evidence, or '' when nothing was left out."""
    parts = []
    outside = sum(1 for d in skipped if d.kind == OUTSIDE_LINK)
    broken = sum(1 for d in skipped if d.kind == BROKEN_LINK)
    special = sum(1 for d in skipped if d.kind == SPECIAL)
    large = sum(1 for d in skipped if d.kind == LARGE_FILE)
    if outside:
        parts.append(f"{_count(outside, 'symlink')} pointing outside the repo {_was(outside)} not copied")
    if broken:
        parts.append(f"{_count(broken, 'symlink')} that could not be resolved {_was(broken)} not copied")
    if special:
        parts.append(f"{_count(special, 'special file')} (FIFO, socket or device) {_was(special)} skipped")
    if large:
        parts.append(f"{_count(large, 'file')} over the per-file size limit {_was(large)} not copied")
    return "; ".join(parts)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _was(n: int) -> str:
    return "was" if n == 1 else "were"
