"""Command line: claimcheck check --repo PATH --report FILE [--diff FILE] [--repro CMD --expect TEXT]."""

import argparse
import sys
from pathlib import Path

from . import checks, extract, report, sandbox
from .models import Verdict


def run_check(repo: Path, report_text: str, diff: str | None, repro: str | None, expect: str, use_model: bool):
    extraction = extract.extract_claims(report_text) if use_model else extract.regex_extract(report_text)
    verdict = Verdict()
    for claim in extraction.claims:
        verdict.evidence.append(checks.check_symbol(repo, claim.target))
    if diff is not None:
        verdict.evidence.append(checks.check_pr_tests(diff))
    if repro:
        verdict.evidence.append(sandbox.run_repro(repo, repro, expect))
    return verdict, extraction.model


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claimcheck")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="test the claims in a report")
    check.add_argument("--repo", required=True, type=Path)
    check.add_argument("--report", required=True, type=Path)
    check.add_argument("--diff", type=Path, help="unified diff of the PR being claimed about")
    check.add_argument("--repro", help="command that should reproduce the failure (run in the sandbox)")
    check.add_argument("--expect", default="", help="text the failing command should print")
    check.add_argument("--json", action="store_true")
    check.add_argument("--no-model", action="store_true", help="skip the model, use the regex extractor")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.repo.is_dir():
        print(f"error: repo not found: {args.repo}", file=sys.stderr)
        return 2
    diff = args.diff.read_text() if args.diff else None
    verdict, model = run_check(args.repo, args.report.read_text(), diff, args.repro, args.expect, not args.no_model)
    print((report.to_json if args.json else report.to_markdown)(verdict, model), end="")
    return report.EXIT_CODE[verdict.outcome]


if __name__ == "__main__":
    sys.exit(main())
