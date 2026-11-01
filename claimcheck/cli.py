"""Command line: claimcheck check --repo PATH --report FILE [--diff FILE] [--repro CMD --expect TEXT [--base REF --head REF]]."""

import argparse
import subprocess
import sys
from pathlib import Path

from . import checks, extract, k8s, report, revisions, sandbox, versions
from .models import Claim, Evidence, Status, Verdict


def git_revision(repo: Path) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def run_check(repo: Path, report_text: str, diff: str | None, repro: str | None, expect: str, use_model: bool,
              run_repro=sandbox.run_repro, base: str | None = None, head: str | None = None,
              model_name: str | None = None):
    if not use_model:
        extraction = extract.regex_extract(report_text)
    elif model_name:
        extraction = extract.extract_claims(report_text, model=model_name)
    else:
        extraction = extract.extract_claims(report_text)
    verdict = Verdict(revision=git_revision(repo), report_truncated=extraction.truncated)
    for claim in extraction.claims:
        verdict.evidence.append(checks.check_symbol(repo, claim.target))
    verdict.evidence.extend(versions.check_versions(repo, report_text))
    if extraction.note:
        verdict.evidence.append(Evidence(Claim("extraction", "Model claim extraction was available"),
                                         Status.UNVERIFIED, extraction.note))
    if diff is not None:
        verdict.evidence.append(checks.check_pr_tests(diff))
    if repro and base is not None and head is not None:
        verdict.evidence.append(revisions.verify_fix(repo, base, head, repro, expect, run_repro))
    elif repro:
        verdict.evidence.append(run_repro(repo, repro, expect))
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
    check.add_argument("--base", help="git ref where the repro should fail (checks a fix claim; needs --head and --repro)")
    check.add_argument("--head", help="git ref that claims the fix; the repro must exit 0 there")
    check.add_argument("--json", action="store_true")
    check.add_argument("--sarif", type=Path, metavar="PATH",
                       help="also write a SARIF 2.1.0 file (e.g. for GitHub code scanning); stdout is unchanged")
    check.add_argument("--no-model", action="store_true", help="skip the model, use the regex extractor")
    check.add_argument("--model-name", metavar="NAME",
                       help=f"Ollama model to extract claims with (default: $CLAIMCHECK_MODEL, else {extract.MODEL}); "
                            "the server comes from $OLLAMA_HOST")
    check.add_argument("--executor", choices=["local", "kubernetes"], default="local",
                       help="where the repro runs: a local no-network process, or a locked-down Kubernetes Job")
    check.add_argument("--k8s-namespace", default="claimcheck")
    check.add_argument("--k8s-image", default="python:3.12-slim")
    check.add_argument("--k8s-runtime-class", help="e.g. gvisor; only if the cluster has that RuntimeClass")
    check.add_argument("--kube-context", help="kubectl context to use")
    check.add_argument("--k8s-impersonate", metavar="USER",
                       help="run every kubectl call as USER (kubectl --as), e.g. "
                            "system:serviceaccount:claimcheck:claimcheck-executor from deploy/k8s")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.repo.is_dir():
        print(f"error: repo not found: {args.repo}", file=sys.stderr)
        return 2
    if (args.base is None) != (args.head is None):
        print("error: --base and --head must be given together", file=sys.stderr)
        return 2
    if args.base is not None and not args.repro:
        print("error: --base/--head need --repro: the command to run on both revisions", file=sys.stderr)
        return 2
    diff = args.diff.read_text() if args.diff else None
    run_repro = sandbox.run_repro
    if args.executor == "kubernetes":
        run_repro = k8s.KubernetesExecutor(namespace=args.k8s_namespace, image=args.k8s_image,
                                           runtime_class=args.k8s_runtime_class, context=args.kube_context,
                                           impersonate=args.k8s_impersonate).run_repro
    verdict, model = run_check(args.repo, args.report.read_text(), diff, args.repro, args.expect, not args.no_model,
                               run_repro, base=args.base, head=args.head, model_name=args.model_name)
    print((report.to_json if args.json else report.to_markdown)(verdict, model), end="")
    if args.sarif:
        args.sarif.write_text(report.to_sarif(verdict, model))
    return report.EXIT_CODE[verdict.outcome]


if __name__ == "__main__":
    sys.exit(main())
