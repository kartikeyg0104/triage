from pathlib import Path

import pytest

from claimcheck.issue_form import FIELDS, MAX_FIELD_CHARS, REQUIRED_FIELDS, IssueForm, parse_issue_form

TEMPLATES = Path(__file__).resolve().parent.parent / ".github" / "ISSUE_TEMPLATE"

# Exactly what GitHub writes into the issue body for a submitted form.
RENDERED = (
    "### Affected function or class\n\n"
    "parse_header\n\n"
    "### Repro command\n\n"
    "```shell\n"
    "python -c \"from pkg import parse_header; parse_header('x')\"\n"
    "```\n\n"
    "### Expected error text\n\n"
    "ValueError: bad header\n\n"
    "### Version or commit\n\n"
    "v1.2.3 (abc1234)\n\n"
    "### Description\n\n"
    "Calling parse_header with one byte crashes.\n"
    "It happens every time.\n"
)


def test_parses_realistic_rendered_body():
    form = parse_issue_form(RENDERED)
    assert form == IssueForm(
        symbol="parse_header",
        repro_command="python -c \"from pkg import parse_header; parse_header('x')\"",
        expected_error="ValueError: bad header",
        version="v1.2.3 (abc1234)",
        description="Calling parse_header with one byte crashes.\nIt happens every time.",
    )


def test_crlf_line_endings():
    assert parse_issue_form(RENDERED.replace("\n", "\r\n")) == parse_issue_form(RENDERED)


def test_no_response_means_empty():
    body = (
        "### Affected function or class\n\nfoo\n\n"
        "### Expected error text\n\n_No response_\n\n"
        "### Version or commit\n\n_No response_\n"
    )
    form = parse_issue_form(body)
    assert form.symbol == "foo"
    assert form.expected_error == ""
    assert form.version == ""


def test_missing_sections_are_empty():
    form = parse_issue_form("### Affected function or class\n\nfoo\n")
    assert form == IssueForm(symbol="foo")


def test_free_text_body_gives_empty_form():
    form = parse_issue_form("parse_header() crashes, please fix")
    assert form == IssueForm()
    assert form.is_empty()
    assert not parse_issue_form(RENDERED).is_empty()


def test_empty_and_none_body():
    assert parse_issue_form("") == IssueForm()
    assert parse_issue_form(None) == IssueForm()


def test_extra_sections_are_ignored_and_end_the_previous_value():
    body = (
        "### Affected function or class\n\nfoo\n\n"
        "### Operating system\n\nmacOS\n\n"
        "### Version or commit\n\n1.0\n"
    )
    form = parse_issue_form(body)
    assert form.symbol == "foo"
    assert form.version == "1.0"
    assert "macOS" not in repr(form)


def test_fence_is_stripped_for_any_language_tag():
    body = "### Expected error text\n\n```text\nTraceback (most recent call last):\n  KeyError: 'x'\n```\n"
    assert parse_issue_form(body).expected_error == "Traceback (most recent call last):\n  KeyError: 'x'"


def test_fenced_no_response_is_empty():
    body = "### Repro command\n\n```shell\n_No response_\n```\n"
    assert parse_issue_form(body).repro_command == ""


def test_heading_inside_fence_is_part_of_the_value():
    body = "### Repro command\n\n```shell\necho '\n### Version or commit\n'\n```\n\n### Version or commit\n\n2.0\n"
    form = parse_issue_form(body)
    assert form.repro_command == "echo '\n### Version or commit\n'"
    assert form.version == "2.0"


def test_first_occurrence_wins_so_description_cannot_override_fields():
    body = RENDERED + "\n### Repro command\n\nrm -rf /\n"
    assert parse_issue_form(body).repro_command.startswith("python -c")


def test_values_are_stripped():
    body = "### Affected function or class\n\n   foo   \n\n\n"
    assert parse_issue_form(body).symbol == "foo"


def test_oversized_values_are_capped():
    body = "### Description\n\n" + "a" * (MAX_FIELD_CHARS * 3) + "\n"
    assert len(parse_issue_form(body).description) == MAX_FIELD_CHARS


def test_oversized_body_does_not_hang_and_is_capped():
    body = RENDERED + ("### Junk\n\nx\n\n" * 50_000)
    form = parse_issue_form(body)
    assert form.symbol == "parse_header"


def test_returns_only_strings_and_never_runs_content(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    body = f"### Repro command\n\n```shell\ntouch {marker}\n```\n"
    import subprocess

    def boom(*a, **k):
        raise AssertionError("parser must not run anything")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    form = parse_issue_form(body)
    assert form.repro_command == f"touch {marker}"
    assert not marker.exists()
    assert all(isinstance(getattr(form, f), str) for f in FIELDS)


def test_control_characters_are_removed():
    body = "### Affected function or class\n\nfoo\x00\x1b[31mbar\n"
    assert parse_issue_form(body).symbol == "foo[31mbar"


@pytest.mark.parametrize("name", ["bug_report.yml", "security_claim.yml"])
def test_forms_match_parser(name):
    yaml = pytest.importorskip("yaml")
    form = yaml.safe_load((TEMPLATES / name).read_text())
    for key in ("name", "description", "title", "labels", "body"):
        assert form.get(key), key
    inputs = [f for f in form["body"] if f["type"] in ("input", "textarea")]
    ids = [f["id"] for f in inputs]
    assert ids == list(FIELDS)
    for f in inputs:
        assert f["id"].isidentifier() and f["id"] == f["id"].lower()
        assert f["attributes"]["label"] == FIELDS[f["id"]]
        required = f.get("validations", {}).get("required", False)
        assert required == (f["id"] in REQUIRED_FIELDS), f["id"]
    repro = next(f for f in inputs if f["id"] == "repro_command")
    assert repro["type"] == "textarea" and repro["attributes"]["render"] == "shell"


def test_config_disables_blank_issues_and_links_private_reporting():
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((TEMPLATES / "config.yml").read_text())
    assert config["blank_issues_enabled"] is False
    urls = [link["url"] for link in config["contact_links"]]
    assert "https://github.com/kartikeyg0104/triage/security/advisories/new" in urls
    for link in config["contact_links"]:
        assert link["name"] and link["about"]
