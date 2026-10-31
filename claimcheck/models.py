"""Shared data model: claims, the evidence found for them, and the overall verdict."""

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Claim:
    """One checkable statement pulled out of a report, e.g. a symbol that must exist."""

    kind: str  # "symbol", "pr_tests", "repro"
    text: str
    target: str = ""


@dataclass(frozen=True)
class Evidence:
    claim: Claim
    status: Status
    detail: str
    source: str = ""  # file:line or command that produced the evidence
    basis: str = "observed"  # "observed": we saw it happen; "inferred": we concluded it from absence or from a heuristic


@dataclass
class Verdict:
    evidence: list[Evidence] = field(default_factory=list)
    revision: str = ""  # git commit of the repo that was tested, if it is a git checkout

    @property
    def outcome(self) -> str:
        """Conservative: any refuted claim wins, and nothing is 'supported' unless all are confirmed."""
        if not self.evidence:
            return "unverified"
        statuses = {e.status for e in self.evidence}
        if Status.REFUTED in statuses:
            return "refuted"
        if statuses == {Status.CONFIRMED}:
            return "supported"
        return "unverified"
