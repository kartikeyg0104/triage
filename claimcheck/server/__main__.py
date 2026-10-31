"""python -m claimcheck.server --db PATH --repo-root PATH [--host 127.0.0.1] [--port 8080]

The webhook secret comes from CLAIMCHECK_WEBHOOK_SECRET, not a flag, so it stays out of the
process list. Without it the webhook endpoint refuses every request.

The API token comes from CLAIMCHECK_API_TOKEN, also not a flag. With it, the investigations API
and the dashboard need `Authorization: Bearer <token>`. Without it they are open, so the server
only binds a loopback host (127.0.0.0/8, ::1, localhost) and refuses to start on any other.
"""

import argparse
import ipaddress
import os
import sys
from pathlib import Path

import uvicorn

from .app import create_app

SECRET_ENV = "CLAIMCHECK_WEBHOOK_SECRET"
TOKEN_ENV = "CLAIMCHECK_API_TOKEN"


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # any other hostname: it could resolve anywhere
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m claimcheck.server")
    parser.add_argument("--db", required=True, type=Path, help="SQLite file, created if missing")
    parser.add_argument("--repo-root", required=True, type=Path,
                        help="investigations may only read repos inside this directory")
    parser.add_argument("--host", default="127.0.0.1",
                        help=f"without {TOKEN_ENV} only a loopback host is allowed")
    parser.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.repo_root.is_dir():
        print(f"error: repo root not found: {args.repo_root}", file=sys.stderr)
        return 2
    token = os.environ.get(TOKEN_ENV, "")
    if not token and not is_loopback(args.host):
        print(f"error: {TOKEN_ENV} is not set, so the investigations API and dashboard would be open to anyone "
              f"who can reach {args.host}; set {TOKEN_ENV} or bind a loopback host (127.0.0.1, ::1, localhost)",
              file=sys.stderr)
        return 2
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        print(f"warning: {SECRET_ENV} is not set; the webhook endpoint will refuse every request", file=sys.stderr)
    uvicorn.run(create_app(args.db, args.repo_root, secret, api_token=token), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
