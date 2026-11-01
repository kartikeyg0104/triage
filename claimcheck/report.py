"""Render a verdict as markdown, JSON or SARIF."""

import json
import re

from . import __version__
from .extract import MAX_REPORT_CHARS
from .models import Claim, Evidence, Status, Verdict

ICON = {Status.CONFIRMED: "✓", Status.REFUTED: "✗", Status.UNVERIFIED: "!"}
EXIT_CODE = {"supported": 0, "refuted": 1, "unverified": 2}
TRUNCATION_NOTE = (f"Report truncated to {MAX_REPORT_CHARS // 1024} KiB before the model; "
                   "claims after the cut were found by regex only")


def _markdown_cell(text: str, code: bool = False) -> str:
    """Keep untrusted text from creating table columns or rows; inside a code span, also from closing it."""
    cell = " ".join(text.splitlines()).replace("\\", "\\\\").replace("|", "\\|")
    return cell.replace("`", "'") if code else cell


def to_markdown(verdict: Verdict, model: str | None) -> str:
    lines = ["# claimcheck report", "", f"**Verdict: {verdict.outcome}**", ""]
    lines.append(f"Claim extraction: {model if model else 'regex fallback (no model used)'}")
    if verdict.report_truncated:
        lines.append(f"**{TRUNCATION_NOTE}.**")
    lines.append(f"Tested revision: {'`' + verdict.revision + '`' if verdict.revision else 'unknown (not a git checkout)'}")
    lines += ["", "| | Claim | Evidence | Basis | Source |", "|---|---|---|---|---|"]
    for e in verdict.evidence:
        lines.append(f"| {ICON[e.status]} {e.status.value} | {_markdown_cell(e.claim.text)} | "
                     f"{_markdown_cell(e.detail)} | {_markdown_cell(e.basis)} | `{_markdown_cell(e.source, code=True)}` |")
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


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Duplicate JSON keys would make the saved evidence ambiguous."""
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f"duplicate report field: {key}")
        fields[key] = value
    return fields


def _report_fields(value: object, expected: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"unknown report schema: {name} must contain {', '.join(sorted(expected))}")
    return value


def from_json(text: str) -> tuple[Verdict, str | None]:
    """Validate the saved report shape before restoring evidence; never infer missing values."""
    data = _report_fields(json.loads(text, object_pairs_hook=_unique_fields),
                          {"verdict", "extraction_model", "revision", "report_truncated", "evidence"}, "report")
    if not isinstance(data["verdict"], str) or data["verdict"] not in EXIT_CODE:
        raise ValueError("invalid report verdict")
    for key in ("extraction_model", "revision"):
        if data[key] is not None and not isinstance(data[key], str):
            raise ValueError(f"invalid report {key}: expected a string or null")
    if not isinstance(data["report_truncated"], bool):
        raise ValueError("invalid report report_truncated: expected a boolean")
    if not isinstance(data["evidence"], list):
        raise ValueError("invalid report evidence: expected a list")
    evidence = []
    for value in data["evidence"]:
        row = _report_fields(value, {"kind", "claim", "status", "basis", "detail", "source"}, "evidence row")
        if not all(isinstance(field, str) for field in row.values()):
            raise ValueError("invalid report evidence: every field must be a string")
        if row["basis"] not in {"observed", "inferred"}:
            raise ValueError("invalid report evidence basis")
        evidence.append(Evidence(Claim(row["kind"], row["claim"]), Status(row["status"]),
                                 row["detail"], row["source"], row["basis"]))
    verdict = Verdict(evidence, revision=data["revision"] or "", report_truncated=data["report_truncated"])
    if verdict.outcome != data["verdict"]:
        raise ValueError("report verdict does not match its evidence")
    return verdict, data["extraction_model"]


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
