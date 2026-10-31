"""python -m claimcheck.server.worker --db PATH --repo-root PATH --repos FILE [--once] [--poll S] [--stale-after S]

Checks recorded webhook events. It claims `received` investigations, runs the static checks
(regex extractor, no model, no repro) against the local checkout mapped to the event's GitHub
repo, and stores the result. Nothing from the event text is executed and nothing is fetched
from GitHub. Any number of workers can share one SQLite file: each row is claimed by one.
"""

import argparse
import json
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .. import report
from ..cli import run_check
from .paths import inside_root
from .store import Investigation, Store

DEFAULT_POLL = 2.0
DEFAULT_STALE_AFTER = 600.0
MAX_ATTEMPTS = 3


def _no_repro(*args, **kwargs):
    raise RuntimeError("the worker does not run repro commands")


def _print_line(line: str) -> None:
    print(line, flush=True)


def load_repo_map(path: Path, root: Path) -> dict[str, Path]:
    """Read `{"owner/repo": "dir under root"}`. Anything malformed or outside root is a ValueError,
    so a bad map stops the worker before it reads a single file."""
    root = root.resolve()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read repo map {path}: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"repo map {path} must be a JSON object of \"owner/repo\": \"relative/path\"")
    repos = {}
    for name, relative in data.items():
        if not isinstance(relative, str) or not relative:
            raise ValueError(f"repo map entry {name!r} must be a non-empty relative path")
        checkout = inside_root(root, relative)
        if checkout is None:
            raise ValueError(f"repo map entry {name!r}: {relative!r} is not a directory inside the repo root {root}")
        repos[name.casefold()] = checkout  # GitHub repo names are case-insensitive
    return repos


def investigate(request: dict, root: Path, repos: dict[str, Path], check=run_check) -> tuple[str, dict | None, str | None]:
    """(status, result, error) for one recorded event."""
    repo, text = request.get("repo"), request.get("text")
    if not isinstance(repo, str) or not isinstance(text, str):
        return "failed", None, "the recorded event has no repo or text"
    checkout = repos.get(repo.casefold())
    if checkout is None:
        return "skipped", None, f"no local checkout is configured for {repo}"
    path = inside_root(root, checkout)  # checked again: the directory or a symlink may have changed since startup
    if path is None:
        return "failed", None, f"the checkout for {repo} is no longer a directory inside the repo root"
    try:
        verdict, model = check(path, text, None, repro=None, expect="", use_model=False, run_repro=_no_repro)
        return "completed", json.loads(report.to_json(verdict, model)), None
    except Exception as error:
        return "failed", None, f"{type(error).__name__}: {error}"


def process_one(store: Store, root: Path, repos: dict[str, Path], worker_id: str,
                stale_after: float = DEFAULT_STALE_AFTER, check=run_check) -> Investigation | None:
    """Claim one row, check it and store the outcome; None when there is nothing to do."""
    claimed = store.claim(worker_id, stale_after, MAX_ATTEMPTS)
    if claimed is None:
        return None
    status, result, error = investigate(claimed.request, root.resolve(), repos, check)
    store.finish(claimed.id, worker_id, status, result, error)
    return store.get(claimed.id)


def run(store: Store, root: Path, repos: dict[str, Path], worker_id: str, *, once: bool = False,
        poll: float = DEFAULT_POLL, stale_after: float = DEFAULT_STALE_AFTER, stop: threading.Event | None = None,
        log: Callable[[str], None] = _print_line) -> int:
    """Process rows until the queue is empty (`once`) or `stop` is set, logging one JSON line per
    row. A row in progress is always finished first. Returns how many rows were processed."""
    stop = stop or threading.Event()
    processed = 0
    while not stop.is_set():
        done = process_one(store, root, repos, worker_id, stale_after)
        if done is None:
            if once:
                break
            stop.wait(poll)
            continue
        processed += 1
        log(json.dumps({"id": done.id, "status": done.status, "worker": done.worker}))
    return processed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m claimcheck.server.worker")
    parser.add_argument("--db", required=True, type=Path, help="the server's SQLite file")
    parser.add_argument("--repo-root", required=True, type=Path, help="every checkout must be inside this directory")
    parser.add_argument("--repos", required=True, type=Path,
                        help='JSON file {"owner/repo": "path/under/repo-root"}; events for other repos are skipped')
    parser.add_argument("--once", action="store_true", help="exit when no row is waiting instead of polling")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL, help="seconds between polls of an empty queue")
    parser.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER,
                        help="seconds after which another worker's unfinished row is reclaimed")
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}:{os.getpid()}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.repo_root.is_dir():
        print(f"error: repo root not found: {args.repo_root}", file=sys.stderr)
        return 2
    try:
        repos = load_repo_map(args.repos, args.repo_root)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        print(f"worker {args.worker_id}: {len(repos)} repo(s) mapped", file=sys.stderr)
        run(Store(args.db), args.repo_root, repos, args.worker_id, once=args.once, poll=args.poll,
            stale_after=args.stale_after, stop=stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
