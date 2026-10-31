"""Read the fields of a submitted GitHub issue form out of the issue body.

GitHub renders a form submission as markdown: one ``### <Label>`` heading per
field, a blank line, then the value. Empty optional fields are written as
``_No response_`` and fields with ``render: shell`` are wrapped in a fenced
code block. The body is untrusted text from anyone who can open an issue, so
this module only parses it: it never runs anything, and every value it returns
is a plain string, stripped of control characters and length-capped.
"""

import re
import unicodedata
from dataclasses import dataclass, fields

# Form field id -> the label GitHub renders as the section heading. The ids and
# labels in .github/ISSUE_TEMPLATE/*.yml must match these (a test checks it).
FIELDS = {
    "symbol": "Affected function or class",
    "repro_command": "Repro command",
    "expected_error": "Expected error text",
    "version": "Version or commit",
    "description": "Description",
}
REQUIRED_FIELDS = frozenset({"symbol", "repro_command", "version", "description"})

MAX_FIELD_CHARS = 4000
MAX_BODY_CHARS = 65536  # GitHub's own limit on an issue body

_NO_RESPONSE = "_No response_"
_HEADING = re.compile(r"^### (.+?)\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_LABEL_TO_ID = {label: field_id for field_id, label in FIELDS.items()}


@dataclass(frozen=True)
class IssueForm:
    symbol: str = ""
    repro_command: str = ""
    expected_error: str = ""
    version: str = ""
    description: str = ""

    def is_empty(self) -> bool:
        return not any(getattr(self, f.name) for f in fields(self))


def parse_issue_form(body: str | None) -> IssueForm:
    """Parse a rendered issue-form body. Unknown or repeated sections are ignored."""
    text = (body or "")[:MAX_BODY_CHARS].replace("\r\n", "\n").replace("\r", "\n")
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence = ""
    for line in text.split("\n"):
        if fence:
            if line.strip().startswith(fence) and not line.strip().strip(fence[0]):
                fence = ""
        else:
            opener = _FENCE.match(line)
            heading = _HEADING.match(line)
            if opener:
                fence = opener.group(1)
            elif heading:
                field_id = _LABEL_TO_ID.get(heading.group(1).strip())
                # First occurrence wins, so text typed into a later field cannot override an earlier one.
                current = None if field_id is None or field_id in sections else sections.setdefault(field_id, [])
                continue
        if current is not None:
            current.append(line)
    return IssueForm(**{field_id: _clean(lines) for field_id, lines in sections.items()})


def _clean(lines: list[str]) -> str:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if len(lines) >= 2 and (opener := _FENCE.match(lines[0])) and lines[-1].strip() == opener.group(1):
        lines = lines[1:-1]
    value = "\n".join(lines).strip()
    if value == _NO_RESPONSE:
        return ""
    value = "".join(ch for ch in value if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    return value[:MAX_FIELD_CHARS]
