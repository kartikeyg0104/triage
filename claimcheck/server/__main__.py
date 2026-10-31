"""python -m claimcheck.server --db PATH --repo-root PATH [--host 127.0.0.1] [--port 8080]

The webhook secret comes from CLAIMCHECK_WEBHOOK_SECRET, not a flag, so it stays out of the
process list. Without it the webhook endpoint refuses every request.
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app

SECRET_ENV = "CLAIMCHECK_WEBHOOK_SECRET"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m claimcheck.server")
    parser.add_argument("--db", required=True, type=Path, help="SQLite file, created if missing")
    parser.add_argument("--repo-root", required=True, type=Path,
                        help="investigations may only read repos inside this directory")
    parser.add_argument("--host", default="127.0.0.1",
                        help="the investigations API has no authentication: keep it on localhost or behind a proxy")
    parser.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.repo_root.is_dir():
        print(f"error: repo root not found: {args.repo_root}", file=sys.stderr)
        return 2
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        print(f"warning: {SECRET_ENV} is not set; the webhook endpoint will refuse every request", file=sys.stderr)
    uvicorn.run(create_app(args.db, args.repo_root, secret), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
