import json

import pytest

import claimcheck
from claimcheck import report
from claimcheck.models import Claim, Evidence, Status, Verdict

SHA = "0123456789abcdef0123456789abcdef01234567"


def sarif(verdict, model=None):
    return json.loads(report.to_sarif(verdict, model))


def ev(status, kind="symbol", source="", detail="detail", basis="observed", text="f() exists"):
    return Evidence(Claim(kind, text), status, detail, source, basis)


def test_required_sarif_fields_are_present():
    data = sarif(Verdict([ev(Status.REFUTED)]))
    assert data["$schema"].startswith("https://")
    assert "sarif" in data["$schema"]
    assert data["version"] == "2.1.0"
    assert len(data["runs"]) == 1
    driver = data["runs"][0]["tool"]["driver"]
    assert driver["name"] == "claimcheck"
    assert driver["version"] == claimcheck.__version__
    assert driver["informationUri"] == "https://github.com/kartikeyg0104/triage"
    rule_ids = {rule["id"] for rule in driver["rules"]}
    assert {"claimcheck/symbol", "claimcheck/pr_tests", "claimcheck/repro", "claimcheck/fix"} <= rule_ids


def test_output_is_valid_json_text():
    text = report.to_sarif(Verdict([ev(Status.UNVERIFIED, detail='quote " and \\ backslash')]), "gemma3:1b")
    assert text.endswith("\n")
    json.loads(text)


def test_levels_map_refuted_to_error_unverified_to_warning_and_omit_confirmed():
    verdict = Verdict([
        ev(Status.CONFIRMED, text="a() exists"),
        ev(Status.REFUTED, text="b() exists"),
        ev(Status.UNVERIFIED, kind="pr_tests", text="tests changed"),
    ])
    results = sarif(verdict)["runs"][0]["results"]
    assert [(r["ruleId"], r["level"]) for r in results] == [("claimcheck/symbol", "error"), ("claimcheck/pr_tests", "warning")]
    assert all("a() exists" not in r["message"]["text"] for r in results)


def test_all_confirmed_gives_no_results():
    assert sarif(Verdict([ev(Status.CONFIRMED)]))["runs"][0]["results"] == []


def test_message_and_properties():
    result = sarif(Verdict([ev(Status.REFUTED, detail="not found", basis="inferred", text="g() exists")]))["runs"][0]["results"][0]
    assert "g() exists" in result["message"]["text"]
    assert "not found" in result["message"]["text"]
    assert result["properties"]["basis"] == "inferred"
    assert result["properties"]["status"] == "refuted"


def test_every_result_rule_is_declared():
    verdict = Verdict([ev(Status.REFUTED, kind=k) for k in ("symbol", "pr_tests", "repro", "fix", "network")])
    run = sarif(verdict)["runs"][0]
    declared = {rule["id"] for rule in run["tool"]["driver"]["rules"]}
    assert {r["ruleId"] for r in run["results"]} <= declared


def test_safe_repo_relative_file_line_gets_a_location():
    result = sarif(Verdict([ev(Status.REFUTED, source="src/urls.py:42")]))["runs"][0]["results"][0]
    location = result["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "src/urls.py"
    assert location["region"]["startLine"] == 42


@pytest.mark.parametrize("source", [
    "/etc/passwd:1", "../outside.py:3", "src/../../x.py:2", "C:\\win\\x.py:1", "\\\\host\\share\\x.py:1",
    "diff", "", "python3 poc.py", "kubernetes[ns/gvisor]: x", "urls.py", "urls.py:0", "urls.py:abc",
    "base..head: x.py:1", "file:///etc/passwd:1", "~/x.py:1",
])
def test_unsafe_or_non_file_sources_get_no_location(source):
    result = sarif(Verdict([ev(Status.UNVERIFIED, source=source)]))["runs"][0]["results"][0]
    assert "locations" not in result


def test_revision_is_recorded_as_version_control_provenance():
    run = sarif(Verdict([ev(Status.REFUTED)], revision=SHA))["runs"][0]
    assert run["versionControlProvenance"][0]["revisionId"] == SHA
    assert run["versionControlProvenance"][0]["repositoryUri"]


def test_unknown_revision_has_no_provenance():
    assert "versionControlProvenance" not in sarif(Verdict([ev(Status.REFUTED)]))["runs"][0]
