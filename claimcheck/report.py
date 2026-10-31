"""Render a verdict as markdown or JSON."""

import json

from .models import Status, Verdict

ICON = {Status.CONFIRMED: "✓", Status.REFUTED: "✗", Status.UNVERIFIED: "!"}
EXIT_CODE = {"supported": 0, "refuted": 1, "unverified": 2}


def to_markdown(verdict: Verdict, model: str | None) -> str:
    lines = ["# claimcheck report", "", f"**Verdict: {verdict.outcome}**", ""]
    lines.append(f"Claim extraction: {model if model else 'regex fallback (no model used)'}")
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
        "evidence": [
            {"kind": e.claim.kind, "claim": e.claim.text, "status": e.status.value, "basis": e.basis,
             "detail": e.detail, "source": e.source}
            for e in verdict.evidence
        ],
    }, indent=2) + "\n"
