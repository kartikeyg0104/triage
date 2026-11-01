"""Untrusted evidence must stay inside its Markdown table cell."""

import re
from pathlib import Path

import pytest

from claimcheck import cli, report
from claimcheck.models import Claim, Evidence, Status, Verdict


def table_rows(markdown: str, count: int = 1) -> list[str]:
    rows = markdown.split("|---|---|---|---|---|\n", 1)[1].splitlines()
    assert len(rows) == count
    for row in rows:
        assert sum(len(slashes) % 2 == 0 for slashes in re.findall(r"(\\*)\|", row)) == 6
        # Only the source cell is a code span; its text can never close it early.
        assert row.rsplit(" | ", 1)[1].count("`") == 2
    return rows


@pytest.mark.parametrize("field", ["claim", "detail", "basis", "source"])
@pytest.mark.parametrize("text,escaped", [
    ("left|right", r"left\|right"),
    (r"left\|right", r"left\\\|right"),
    (r"left\\|right", r"left\\\\\|right"),
    ("left\nright", "left right"),
    ("left\r\nright", "left right"),
    ("left\rright", "left right"),
    ("`left`", "'left'"),
    ("```left```", "'''left'''"),
])
def test_hostile_text_stays_in_one_cell(field: str, text: str, escaped: str) -> None:
    values = {"claim": "f() exists", "detail": "found", "basis": "observed", "source": "a.py:1"}
    values[field] = text
    evidence = Evidence(Claim("symbol", values["claim"]), Status.UNVERIFIED,
                        values["detail"], values["source"], values["basis"])
    markdown = report.to_markdown(Verdict([evidence]), None)
    # Backticks are only neutralised inside the source code span; elsewhere inline code is fine.
    values[field] = escaped if field == "source" or "`" not in text else text
    assert table_rows(markdown) == [
        f"| ! unverified | {values['claim']} | {values['detail']} | {values['basis']} | `{values['source']}` |"
    ]
    assert "**Verdict: unverified**" in markdown


def test_combined_injection_keeps_evidence_rows_and_raw_data() -> None:
    text = "`claim`|extra\n| forged | row |\r\n```"
    evidence = Evidence(Claim("repro", text), Status.UNVERIFIED, text, text)
    verdict = Verdict([evidence, Evidence(Claim("symbol", "f() exists"), Status.CONFIRMED, "found", "a.py:1")])
    saved = report.to_json(verdict, None)
    rows = table_rows(report.to_markdown(verdict, None), 2)
    assert "'claim'\\|extra \\| forged \\| row \\| '''" in rows[0]
    assert rows[1] == "| ✓ confirmed | f() exists | found | observed | `a.py:1` |"
    assert report.to_json(verdict, None) == saved
    assert evidence.claim.text == evidence.detail == evidence.source == text


def test_plain_cells_keep_the_existing_format() -> None:
    verdict = Verdict([Evidence(Claim("symbol", "f() exists"), Status.CONFIRMED, "found", "a.py:1")])
    assert table_rows(report.to_markdown(verdict, None)) == [
        "| ✓ confirmed | f() exists | found | observed | `a.py:1` |"
    ]


def test_empty_report_keeps_the_placeholder_row() -> None:
    assert "| | no checkable claims found | | | |\n" in report.to_markdown(Verdict(), None)


def test_cli_render_contains_hostile_saved_evidence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = "`hostile`|extra\n| forged | row |"
    verdict = Verdict([Evidence(Claim("repro", text), Status.UNVERIFIED, text, text)])
    saved = tmp_path / "report.json"
    saved.write_text(report.to_json(verdict, None), encoding="utf-8")
    assert cli.main(["render", str(saved), "--format", "markdown"]) == 2
    captured = capsys.readouterr()
    row = table_rows(captured.out)[0]
    assert "'hostile'\\|extra \\| forged \\| row \\|" in row
    assert captured.err == ""
