import json
import urllib.error

from claimcheck import extract

REPORT = "RCE in parse_url_v2(). Run `python poc.py` and you get 'SegmentationFault'. Also see `Router`."


def fake(payload):
    return lambda prompt: json.dumps(payload)


def test_model_output_becomes_claims():
    result = extract.extract_claims(REPORT, fake({
        "symbols": ["parse_url_v2()", "Router"], "repro_command": "python poc.py", "expected_error": "SegmentationFault"}))
    assert [c.target for c in result.claims] == ["parse_url_v2", "Router"]
    assert result.repro_command == "python poc.py"
    assert result.expected_error == "SegmentationFault"
    assert result.model == extract.MODEL


def test_names_not_in_report_are_dropped():
    result = extract.extract_claims(REPORT, fake({"symbols": ["parse_url_v2", "invented_name"]}))
    assert "invented_name" not in {c.target for c in result.claims}
    assert "parse_url_v2" in {c.target for c in result.claims}


def test_invented_command_is_dropped():
    result = extract.extract_claims(REPORT, fake({"symbols": [], "repro_command": "rm -rf /"}))
    assert result.repro_command == ""


def test_non_identifiers_are_dropped():
    result = extract.extract_claims(REPORT, fake({"symbols": ["parse_url_v2; rm -rf", "Router"]}))
    targets = {c.target for c in result.claims}
    assert not any(";" in t or " " in t for t in targets)
    assert "Router" in targets


def test_bad_json_falls_back_to_regex():
    result = extract.extract_claims(REPORT, lambda prompt: "not json")
    assert result.model is None
    assert {c.target for c in result.claims} == {"parse_url_v2", "Router"}


def test_unreachable_ollama_falls_back_and_says_so():
    def down(prompt):
        raise urllib.error.URLError("refused")

    result = extract.extract_claims(REPORT, down)
    assert result.model is None
    assert "parse_url_v2" in {c.target for c in result.claims}


def test_json_list_instead_of_object_falls_back():
    assert extract.extract_claims(REPORT, lambda p: "[1, 2]").model is None


def test_model_misses_are_filled_in_by_regex():
    result = extract.extract_claims(REPORT, fake({"symbols": ["Router"]}))
    assert {c.target for c in result.claims} == {"Router", "parse_url_v2"}
    assert result.model == extract.MODEL
