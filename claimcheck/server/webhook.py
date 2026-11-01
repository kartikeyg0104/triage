"""GitHub webhook signatures and the parts of an event we record.

Everything in the payload is untrusted text from whoever opened the issue or PR. It is
parsed into plain data here and is never passed to a shell or used to pick a command.
"""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass

from ..extract import regex_extract

PREFIX = "sha256="
PAYLOAD_KEY = {"issues": "issue", "pull_request": "pull_request"}  # X-GitHub-Event -> payload field
MAX_PENDING = 100
MAX_PER_MINUTE = 60
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def intake_limit(value: int | None, env_name: str, default: int) -> int:
    """Reject unusable caps at startup so configuration cannot silently disable them."""
    if value is None:
        try:
            value = int(os.environ.get(env_name, str(default)))
        except ValueError:
            raise ValueError(f"{env_name} must be a positive integer") from None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{env_name} must be a positive integer")
    return value


def verify_signature(secret: str, body: bytes, header: str | bytes | None) -> bool:
    """Check X-Hub-Signature-256 against the raw body. An empty secret never verifies:
    anyone can compute an HMAC with an empty key."""
    if not secret or not header:
        return False
    if isinstance(header, str):
        if not header.isascii():  # compare_digest raises on non-ASCII str; it can never match a hex digest anyway
            return False
        header = header.encode()
    expected = (PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()).encode()
    return hmac.compare_digest(expected, header)


@dataclass(frozen=True)
class IssueEvent:
    event: str
    action: str
    repo: str
    number: int
    text: str

    @property
    def source(self) -> str:
        return f"github:{self.event}:{self.repo}#{self.number}"

    def request(self) -> dict:
        return {"event": self.event, "action": self.action, "repo": self.repo, "number": self.number,
                "text": self.text, "claims": [c.target for c in regex_extract(self.text).claims]}


def parse_event(event: str, payload: object) -> IssueEvent:
    """Pull the issue or PR out of a payload; ValueError if it does not have the expected shape."""
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")
    item, repo = payload.get(PAYLOAD_KEY[event]), payload.get("repository")
    if not isinstance(item, dict) or not isinstance(repo, dict):
        raise ValueError(f"payload has no {PAYLOAD_KEY[event]} or repository")
    number, name = item.get("number"), repo.get("full_name")
    if not isinstance(number, int) or isinstance(number, bool) or not isinstance(name, str) or not name:
        raise ValueError("payload has no usable number or repository full_name")
    if REPOSITORY.fullmatch(name) is None or any(part in (".", "..") for part in name.split("/")):
        raise ValueError("repository full_name must be owner/repo")
    text = f"{item.get('title') or ''}\n\n{item.get('body') or ''}"
    return IssueEvent(event, str(payload.get("action") or ""), name, number, text)
