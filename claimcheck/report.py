"""Render a verdict as markdown or JSON."""

import json

from .models import Status, Verdict

ICON = {Status.CONFIRMED: "✓", Status.REFUTED: "✗", Status.UNVERIFIED: "!"}
EXIT_CODE = {"supported": 0, "refuted": 1, "unverified": 2}


def to_markdown(verdict: Verdict, model: str | None) -> str:
    lines = ["# claimcheck report", "", f"**Verdict: {verdict.outcome}**", ""]
    lines.append(f"Claim extraction: {model if model else 'regex fallback (no model used)'}")
    lines += ["", "| | Claim | Evidence | Source |", "|---|---|---|---|"]
    for e in verdict.evidence:
        lines.append(f"| {ICON[e.status]} {e.status.value} | {e.claim.text} | {e.detail} | `{e.source}` |")
    if not verdict.evidence:
        lines.append("| | no checkable claims found | | |")
    return "\n".join(lines) + "\n"


def to_json(verdict: Verdict, model: str | None) -> str:
    return json.dumps({
        "verdict": verdict.outcome,
        "extraction_model": model,
        "evidence": [
            {"kind": e.claim.kind, "claim": e.claim.text, "status": e.status.value, "detail": e.detail, "source": e.source}
            for e in verdict.evidence
        ],
    }, indent=2) + "\n"
