"""Render a verdict as markdown, JSON or SARIF."""

import json
import re

from . import __version__
from .extract import MAX_REPORT_CHARS
from .models import Status, Verdict

ICON = {Status.CONFIRMED: "✓", Status.REFUTED: "✗", Status.UNVERIFIED: "!"}
EXIT_CODE = {"supported": 0, "refuted": 1, "unverified": 2}
TRUNCATION_NOTE = (f"Report truncated to {MAX_REPORT_CHARS // 1024} KiB before the model; "
                   "claims after the cut were found by regex only")


def to_markdown(verdict: Verdict, model: str | None) -> str:
    lines = ["# claimcheck report", "", f"**Verdict: {verdict.outcome}**", ""]
    lines.append(f"Claim extraction: {model if model else 'regex fallback (no model used)'}")
    if verdict.report_truncated:
        lines.append(f"**{TRUNCATION_NOTE}.**")
    lines.append(f"Tested revision: {'`' + verdict.revision + '`' if verdict.revision else 'unknown (not a git checkout)'}")
    lines += ["", "| | Claim | Evidence | Basis | Source |", "|---|---|---|---|---|"]
    for e in verdict.evidence:
        lines.append(f"| {ICON[e.status]} {e.status.value} | {e.claim.text} | {e.detail} | {e.basis} | `{e.source}` |")
    if not verdict.evidence:
        lines.append("| | no checkable claims found | | | |")
    return "\n".join(lines) + "\n"


def to_json(verdict: Verdict, model: str | None) -> str:
    return json.dumps({
        "verdict": verdict.outcome,
        "extraction_model": model,
        "revision": verdict.revision or None,
        "report_truncated": verdict.report_truncated,
        "evidence": [
            {"kind": e.claim.kind, "claim": e.claim.text, "status": e.status.value, "basis": e.basis,
             "detail": e.detail, "source": e.source}
            for e in verdict.evidence
        ],
    }, indent=2) + "\n"


SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
INFORMATION_URI = "https://github.com/kartikeyg0104/triage"
SARIF_LEVEL = {Status.REFUTED: "error", Status.UNVERIFIED: "warning"}  # confirmed rows are not findings
RULES = {
    "symbol": "A symbol the report names must exist in the repository.",
    "pr_tests": "A pull request should change tests alongside its source change.",
    "repro": "The reproduction command must show the reported failure.",
    "fix": "The fix revision must make the failing reproduction pass.",
    "network": "The sandbox must block network egress before a repro is trusted.",
}
_FILE_LINE = re.compile(r"(?P<path>[A-Za-z0-9_.\-/]+):(?P<line>[1-9][0-9]*)")
UNKNOWN_REPOSITORY = "urn:claimcheck:tested-repository"


def _rule_id(kind: str) -> str:
    return f"claimcheck/{kind}"


def _safe_location(source: str) -> dict | None:
    """A physical location only for a repo-relative `path:line`: no absolute path, no `..`, no scheme."""
    match = _FILE_LINE.fullmatch(source)
    if not match:
        return None
    path = match["path"]
    parts = path.split("/")
    if path.startswith("/") or ".." in parts or "" in parts or "." in parts:
        return None
    return {"physicalLocation": {"artifactLocation": {"uri": path, "uriBaseId": "SRCROOT"},
                                 "region": {"startLine": int(match["line"])}}}


def to_sarif(verdict: Verdict, model: str | None, repository_uri: str | None = None) -> str:
    """SARIF 2.1.0: one result per refuted (error) or unverified (warning) evidence row."""
    kinds = list(RULES) + [e.claim.kind for e in verdict.evidence if e.claim.kind not in RULES]
    rules = [{"id": _rule_id(kind), "name": kind,
              "shortDescription": {"text": RULES.get(kind, f"claimcheck {kind} claim")},
              "helpUri": INFORMATION_URI} for kind in dict.fromkeys(kinds)]
    index = {rule["id"]: i for i, rule in enumerate(rules)}
    results = []
    for e in verdict.evidence:
        if e.status not in SARIF_LEVEL:
            continue
        rule_id = _rule_id(e.claim.kind)
        result = {"ruleId": rule_id, "ruleIndex": index[rule_id], "level": SARIF_LEVEL[e.status],
                  "message": {"text": f"{e.claim.text}: {e.detail}" if e.detail else e.claim.text},
                  "properties": {"status": e.status.value, "basis": e.basis, "source": e.source}}
        location = _safe_location(e.source)
        if location:
            result["locations"] = [location]
        results.append(result)
    run = {"tool": {"driver": {"name": "claimcheck", "version": __version__, "informationUri": INFORMATION_URI,
                               "rules": rules}},
           "results": results,
           "properties": {"verdict": verdict.outcome, "extractionModel": model,
                          "reportTruncated": verdict.report_truncated}}
    if verdict.revision:
        run["versionControlProvenance"] = [{"repositoryUri": repository_uri or UNKNOWN_REPOSITORY,
                                            "revisionId": verdict.revision}]
    return json.dumps({"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": [run]}, indent=2) + "\n"
