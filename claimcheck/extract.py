"""Pull checkable claims out of free-text reports.

Uses a local open-weight model (Gemma via Ollama). If Ollama is not reachable it falls back
to a regex extractor and reports that, so the model name in the result is always truthful.
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .models import Claim

MODEL = "gemma3:1b"
OLLAMA_URL = "http://localhost:11434/api/generate"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PROMPT = """You read a bug, security or pull request report and list what can be checked.
Reply with JSON only, in this shape:
{{"symbols": [function or class names the report says exist or are broken],
  "repro_command": "a shell command the report says reproduces the problem, or empty",
  "expected_error": "text the failing command should print, or empty"}}
Only include names that literally appear in the report.

REPORT:
{report}
"""


@dataclass
class Extraction:
    claims: list[Claim] = field(default_factory=list)
    repro_command: str = ""
    expected_error: str = ""
    model: str | None = None  # None means the regex fallback produced this


def _ollama(prompt: str, timeout: int = 120) -> str:
    body = json.dumps({"model": MODEL, "prompt": prompt, "format": "json", "stream": False,
                       "options": {"temperature": 0}}).encode()
    request = urllib.request.Request(OLLAMA_URL, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)["response"]


def _clean_symbols(names) -> list[str]:
    seen = []
    for name in names if isinstance(names, list) else []:
        name = str(name).strip().removesuffix("()")
        if _IDENT.match(name) and name not in seen:
            seen.append(name)
    return seen


def parse_model_output(raw: str, report: str) -> Extraction | None:
    """Validate what the model said. Names that are not in the report are dropped."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    symbols = [s for s in _clean_symbols(data.get("symbols")) if s in report]
    command = str(data.get("repro_command") or "").strip()
    expected = str(data.get("expected_error") or "").strip()
    return Extraction(
        claims=[Claim("symbol", f"{s}() exists", s) for s in symbols],
        repro_command=command if command in report else "",
        expected_error=expected if expected in report else "",
        model=MODEL,
    )


def _looks_like_code(name: str) -> bool:
    """A backticked plain word like `path` (or `URL`) is prose; `parse_url`, `Router` and
    `truncateSlug` look like code."""
    return "_" in name or not (name.islower() or name.isupper())


def regex_extract(report: str) -> Extraction:
    """No model: find `name()` calls and backticked names that look like code."""
    names = re.findall(r"`?([A-Za-z_][A-Za-z0-9_]*)\(\)`?", report)
    names += [n for n in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", report) if _looks_like_code(n)]
    return Extraction(claims=[Claim("symbol", f"{s}() exists", s) for s in _clean_symbols(names)])


def extract_claims(report: str, generate=_ollama) -> Extraction:
    try:
        raw = generate(PROMPT.format(report=report))
    except (urllib.error.URLError, OSError, TimeoutError, KeyError, ValueError):
        return regex_extract(report)
    extraction = parse_model_output(raw, report)
    if extraction is None:
        return regex_extract(report)
    # A small model can miss names that literally appear as `name()`; add those.
    known = {c.target for c in extraction.claims}
    extraction.claims += [c for c in regex_extract(report).claims if c.target not in known]
    return extraction
