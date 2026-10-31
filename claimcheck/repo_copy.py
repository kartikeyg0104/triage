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
SKIPPED_KINDS = frozenset({OUTSIDE_LINK, BROKEN_LINK, SPECIAL})


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


def copy_repo(src: str | os.PathLike[str], dst: str | os.PathLike[str],
              ignored: frozenset[str] = IGNORED) -> list[Decision]:
    """Copy `src` to a new directory `dst` following `walk`. Returns what was left out."""
    root = Path(os.path.realpath(src))
    dst = Path(dst)
    dst.mkdir()
    skipped = []
    for decision in walk(root, ignored):
        out = dst / decision.path
        if decision.kind == DIR:
            out.mkdir()
        elif decision.kind == FILE:
            shutil.copy2(root / decision.path, out, follow_symlinks=False)
        elif decision.kind == LINK:
            os.symlink(decision.target, out)
        else:
            skipped.append(decision)
    return skipped


def describe_skipped(skipped: list[Decision]) -> str:
    """One honest sentence for the evidence, or '' when nothing was left out."""
    parts = []
    outside = sum(1 for d in skipped if d.kind == OUTSIDE_LINK)
    broken = sum(1 for d in skipped if d.kind == BROKEN_LINK)
    special = sum(1 for d in skipped if d.kind == SPECIAL)
    if outside:
        parts.append(f"{_count(outside, 'symlink')} pointing outside the repo {_was(outside)} not copied")
    if broken:
        parts.append(f"{_count(broken, 'symlink')} that could not be resolved {_was(broken)} not copied")
    if special:
        parts.append(f"{_count(special, 'special file')} (FIFO, socket or device) {_was(special)} skipped")
    return "; ".join(parts)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _was(n: int) -> str:
    return "was" if n == 1 else "were"
