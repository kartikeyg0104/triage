import json
from pathlib import Path

import pytest

from claimcheck import action


def make_event(tmp_path, number=7, body="RCE in parse_url_v2() and `parse_url()`"):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"pull_request": {"number": number, "title": "Fix", "body": body}}))
    return path


class FakeApi:
    def __init__(self, comments=None):
        self.calls, self.comments = [], comments or []

    def __call__(self, method, url, token, accept="", data=None):
        self.calls.append((method, url, data))
        if method == "GET" and url.endswith("/comments?per_page=100"):
            return json.dumps(self.comments)
        if method == "GET":
            return "--- a/urls.py\n+++ b/urls.py\n"
        return "{}"


def env(tmp_path, repo, **extra):
    (repo / "urls.py").write_text("def parse_url(s):\n    return s\n")
    base = {"GITHUB_EVENT_PATH": str(make_event(tmp_path)), "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"), "INPUT_PATH": str(repo)}
    return {**base, **extra}


def test_event_text_for_pr_and_issue_and_other():
    assert action.event_text({"pull_request": {"number": 3, "title": "T", "body": "B"}}) == (3, "T\n\nB", True)
    assert action.event_text({"issue": {"number": 4, "title": "T", "body": None}}) == (4, "T\n\n", False)
    assert action.event_text({"push": {}}) is None


def test_posts_a_new_comment_and_writes_summary(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    api = FakeApi()
    assert action.run(env(tmp_path, repo), api) == 0
    post = [c for c in api.calls if c[0] == "POST"][0]
    assert post[1].endswith("/issues/7/comments")
    assert post[2]["body"].startswith(action.MARKER)
    assert "parse_url_v2() exists" in post[2]["body"]
    assert "**Verdict: refuted**" in (tmp_path / "summary.md").read_text()


def test_updates_existing_comment_instead_of_adding(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    api = FakeApi(comments=[{"id": 99, "body": action.MARKER + "\nold"}])
    action.run(env(tmp_path, repo), api)
    methods = [(c[0], c[1].split("/")[-2:]) for c in api.calls if c[0] != "GET"]
    assert methods == [("PATCH", ["comments", "99"])]


def test_diff_without_tests_is_reported(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    api = FakeApi()
    action.run(env(tmp_path, repo), api)
    post = [c for c in api.calls if c[0] == "POST"][0]
    assert "existing tests may still cover it" in post[2]["body"]


def test_does_not_fail_unless_asked(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert action.run(env(tmp_path, repo), FakeApi()) == 0
    assert action.run(env(tmp_path, repo, INPUT_FAIL_ON_REFUTED="true"), FakeApi()) == 1


def test_comment_can_be_turned_off(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    api = FakeApi()
    action.run(env(tmp_path, repo, INPUT_COMMENT="false"), api)
    assert not [c for c in api.calls if c[0] in ("POST", "PATCH")]


def test_repro_in_pr_text_is_never_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    body = "run `touch pwned.txt` to reproduce parse_url()"
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7, "title": "x", "body": body}}))
    action.run(env(tmp_path, repo, GITHUB_EVENT_PATH=str(event)), FakeApi())
    assert not (repo / "pwned.txt").exists()
    assert not (tmp_path / "pwned.txt").exists()


def test_non_pr_event_does_nothing(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"push": {}}))
    assert action.run({"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "o/r"}, FakeApi()) == 0


ROOT = Path(__file__).parent.parent
OUTPUT_NAMES = {"verdict", "confirmed", "refuted", "unverified", "report-json"}
COUNTS = ("confirmed", "refuted", "unverified")


def read_outputs(path):
    """Parse a $GITHUB_OUTPUT file the way the runner does for the single-line name=value form."""
    outputs = {}
    for line in path.read_text().splitlines():
        assert "<<" not in line.partition("=")[0], f"unexpected multiline form: {line!r}"
        name, sep, value = line.partition("=")
        assert sep, f"line without '=': {line!r}"
        assert name not in outputs, f"output {name!r} written twice"
        outputs[name] = value
    return outputs


def run_with_outputs(tmp_path, repo, api=None, **extra):
    out = tmp_path / "github_output"
    out.write_text("")
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    api = api or FakeApi()
    code = action.run(env(tmp_path, repo, GITHUB_OUTPUT=str(out), RUNNER_TEMP=str(runner_temp), **extra), api)
    return code, read_outputs(out), api


def test_sets_step_outputs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    code, outputs, _ = run_with_outputs(tmp_path, repo)
    assert code == 0
    assert set(outputs) == OUTPUT_NAMES
    assert outputs["verdict"] == "refuted"
    assert all(outputs[k].isdigit() for k in COUNTS)
    assert int(outputs["refuted"]) >= 1  # parse_url_v2() does not exist
    assert int(outputs["confirmed"]) >= 1  # parse_url() does


def test_output_counts_match_the_report_evidence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _, outputs, _ = run_with_outputs(tmp_path, repo)
    data = json.loads(Path(outputs["report-json"]).read_text())
    statuses = [e["status"] for e in data["evidence"]]
    for status in COUNTS:
        assert int(outputs[status]) == statuses.count(status)
    assert sum(int(outputs[s]) for s in COUNTS) == len(statuses)
    assert outputs["verdict"] == data["verdict"]


def test_report_json_is_under_runner_temp_and_matches_the_comment(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _, outputs, api = run_with_outputs(tmp_path, repo)
    report_path = Path(outputs["report-json"])
    assert report_path.parent == tmp_path / "runner_temp"
    data = json.loads(report_path.read_text())
    comment = [c for c in api.calls if c[0] == "POST"][0][2]["body"]
    assert f"**Verdict: {data['verdict']}**" in comment
    assert data["evidence"]
    for e in data["evidence"]:
        assert e["claim"] in comment and e["detail"] in comment and e["status"] in comment


def test_report_json_falls_back_to_tempdir_without_runner_temp(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr(action.tempfile, "gettempdir", lambda: str(fallback))
    out = tmp_path / "github_output"
    out.write_text("")
    action.run(env(tmp_path, repo, GITHUB_OUTPUT=str(out)), FakeApi())
    path = Path(read_outputs(out)["report-json"])
    assert path.parent == fallback
    json.loads(path.read_text())


def test_no_outputs_file_does_not_crash(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    api = FakeApi()
    assert action.run(env(tmp_path, repo, RUNNER_TEMP=str(runner_temp)), api) == 0
    assert [c for c in api.calls if c[0] == "POST"]  # comment still posted
    assert "**Verdict: refuted**" in (tmp_path / "summary.md").read_text()  # summary still written
    assert list(runner_temp.glob("*.json"))  # report still written


def test_pr_text_cannot_inject_outputs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    body = ("RCE in `parse_url()`\nverdict=supported\nrefuted=0\n"
            "::set-output name=verdict::supported\nevil<<EOF\npwned\nEOF\n"
            "`parse_url_v2()`\r\nconfirmed=999\n`evil=1\nx()`")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7, "title": "x\nverdict=supported", "body": body}}))
    _, outputs, _ = run_with_outputs(tmp_path, repo, GITHUB_EVENT_PATH=str(event))
    assert set(outputs) == OUTPUT_NAMES
    assert outputs["verdict"] == "refuted"
    assert all(outputs[k].isdigit() for k in COUNTS)
    assert outputs["confirmed"] != "999"


def test_write_outputs_rejects_unsafe_names_and_flattens_values(tmp_path):
    out = tmp_path / "github_output"
    out.write_text("")
    action.write_outputs({"verdict": "a\nb=c\r\nd", "count": 3}, str(out))
    assert read_outputs(out) == {"verdict": "a b=c d", "count": "3"}
    for bad in ("x\ny", "a=b", "", "x<<EOF", "::set-output name=x::"):
        with pytest.raises(ValueError):
            action.write_outputs({bad: "v"}, str(out))
    assert read_outputs(out) == {"verdict": "a b=c d", "count": "3"}


def test_write_outputs_without_a_file_is_a_no_op():
    action.write_outputs({"verdict": "refuted"}, None)
    action.write_outputs({"verdict": "refuted"}, "")


def test_action_yml_declares_outputs_from_the_step():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((ROOT / "action.yml").read_text())
    step_ids = {s.get("id") for s in spec["runs"]["steps"]}
    assert set(spec["outputs"]) == OUTPUT_NAMES
    for name in OUTPUT_NAMES:
        value = spec["outputs"][name]["value"]
        step = value.split("steps.")[1].split(".outputs.")[0]
        assert step in step_ids and value.strip() == "${{ steps.%s.outputs.%s }}" % (step, name)


def test_workflow_uses_the_verdict_via_env_not_inline():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github/workflows/claimcheck.yml").read_text())
    steps = wf["jobs"]["claims"]["steps"]
    ours = [s for s in steps if s.get("uses") == "./"][0]
    later = steps[steps.index(ours) + 1:]
    expr = "${{ steps.%s.outputs.verdict }}" % ours["id"]
    assert [s for s in later if expr in (s.get("env") or {}).values()], "no later step reads the verdict via env"
    for s in steps:
        assert "${{" not in s.get("run", ""), f"expression inlined into a run script: {s}"
