"""Issue #73: every renderer says when the model saw only the first 16 KiB of the report."""

import json

from claimcheck import report
from claimcheck.models import Claim, Evidence, Status, Verdict

TRUNCATION_LINE = "Report truncated to 16 KiB before the model; claims after the cut were found by regex only"


def verdict(truncated):
    v = Verdict([Evidence(Claim("symbol", "f() exists", "f"), Status.CONFIRMED, "found", "a.py:1")])
    v.report_truncated = truncated
    return v


def test_verdict_is_not_truncated_by_default():
    assert Verdict().report_truncated is False


def test_markdown_says_when_the_report_was_truncated():
    assert TRUNCATION_LINE in report.to_markdown(verdict(True), "gemma3:1b")


def test_markdown_is_silent_when_not_truncated():
    assert "truncated" not in report.to_markdown(verdict(False), "gemma3:1b").lower()


def test_json_has_report_truncated_true():
    assert json.loads(report.to_json(verdict(True), "gemma3:1b"))["report_truncated"] is True


def test_json_has_report_truncated_false():
    assert json.loads(report.to_json(verdict(False), None))["report_truncated"] is False


def test_sarif_run_property_records_truncation():
    run = json.loads(report.to_sarif(verdict(True), "gemma3:1b"))["runs"][0]
    assert run["properties"]["reportTruncated"] is True


def test_sarif_run_property_is_false_when_not_truncated():
    run = json.loads(report.to_sarif(verdict(False), None))["runs"][0]
    assert run["properties"]["reportTruncated"] is False
