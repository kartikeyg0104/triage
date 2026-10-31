import io
import json
import urllib.error
from pathlib import Path

import pytest

from claimcheck import action
from claimcheck.models import Claim, Evidence, Status, Verdict


def make_event(tmp_path, number=7, body="RCE in parse_url_v2() and `parse_url()`", head_sha=None,
               name="event.json"):
    path = tmp_path / name
    pr = {"number": number, "title": "Fix", "body": body}
    if head_sha:
        pr["head"] = {"sha": head_sha}
    path.write_text(json.dumps({"pull_request": pr}))
    return path


class FakeApi:
    def __init__(self, comments=None, fail=None):
        self.calls, self.comments, self.fail = [], comments or [], fail or {}

    def __call__(self, method, url, token, accept="", data=None):
        self.calls.append((method, url, data))
        for fragment, code in self.fail.items():
            if fragment in url:
                raise urllib.error.HTTPError(url, code, "denied", {}, io.BytesIO(b"{}"))
        if method == "POST" and url.endswith("/check-runs"):
            return json.dumps({"id": 555})
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


# --- Check Run (issue #48) ---------------------------------------------------------------------------

SHA = "a" * 40


def check_calls(api):
    return [c for c in api.calls if "/check-runs" in c[1]]


def run_pr(tmp_path, repo, api=None, **extra):
    api = api or FakeApi()
    event = make_event(tmp_path, head_sha=SHA, name="pr_event.json")
    code = action.run(env(tmp_path, repo, GITHUB_EVENT_PATH=str(event), **extra), api)
    return code, api


def ev(status, source="urls.py:1", detail="d", text="c"):
    return Evidence(Claim("symbol", text, text), status, detail, source)


@pytest.mark.parametrize("outcome,fail_on_refuted,expected", [
    ("supported", False, "success"), ("supported", True, "success"),
    ("refuted", True, "failure"), ("refuted", False, "neutral"),
    ("unverified", False, "neutral"), ("unverified", True, "neutral"),
])
def test_conclusion_mapping(outcome, fail_on_refuted, expected):
    assert action.check_conclusion(outcome, fail_on_refuted) == expected


def test_creates_check_run_on_the_pr_head_sha(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    code, api = run_pr(tmp_path, repo)
    assert code == 0
    calls = check_calls(api)
    assert len(calls) == 1
    method, url, payload = calls[0]
    assert (method, url) == ("POST", "https://api.github.com/repos/o/r/check-runs")
    assert payload["name"] == "claimcheck"
    assert payload["head_sha"] == SHA
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "neutral"  # refuted, fail-on-refuted not set
    assert "refuted" in payload["output"]["title"]
    assert "**Verdict: refuted**" in payload["output"]["summary"]
    assert "| Claim | Evidence |" in payload["output"]["summary"]


def test_check_run_conclusion_failure_when_fail_on_refuted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    code, api = run_pr(tmp_path, repo, INPUT_FAIL_ON_REFUTED="true")
    assert code == 1
    assert check_calls(api)[0][2]["conclusion"] == "failure"


def test_check_run_annotates_repo_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _, api = run_pr(tmp_path, repo)
    annotations = check_calls(api)[0][2]["output"]["annotations"]
    assert annotations, "the confirmed parse_url() definition should be annotated"
    a = annotations[0]
    assert a["path"] == "urls.py" and a["start_line"] == a["end_line"] == 1
    assert a["annotation_level"] == "notice"
    assert "parse_url" in a["message"]
    # the refuted claim's source is the repo name, not a file:line, so it gets no annotation
    assert all(x["path"] == "urls.py" for x in annotations)


def test_annotation_levels_by_status(tmp_path):
    (tmp_path / "urls.py").write_text("x\n")
    levels = {s: action.annotation_for(ev(s), tmp_path)["annotation_level"] for s in Status}
    assert levels == {Status.CONFIRMED: "notice", Status.UNVERIFIED: "warning", Status.REFUTED: "warning"}


@pytest.mark.parametrize("source", [
    "../x.py:1", "/etc/passwd:1", "sub/../../x.py:1", "sub/../urls.py:1", "urls.py", "urls.py:0", "urls.py:-1",
    "urls.py:abc", "missing.py:1", "diff", "repo", "", ":1", "C:\\x.py:1", "sub\\..\\x.py:1",
    "urls.py:1\n", "sub:1", "./urls.py:1x",
])
def test_annotation_path_validation_rejects(tmp_path, source):
    (tmp_path / "urls.py").write_text("x\n")
    (tmp_path / "sub").mkdir()
    (tmp_path.parent / "x.py").write_text("x\n")
    assert action.annotation_for(ev(Status.CONFIRMED, source), tmp_path) is None


def test_annotation_rejects_symlink_out_of_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.py").write_text("s\n")
    (root / "link.py").symlink_to(tmp_path / "secret.py")
    assert action.annotation_for(ev(Status.CONFIRMED, "link.py:1"), root) is None


def test_annotation_accepts_nested_repo_file(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x\n" * 5)
    a = action.annotation_for(ev(Status.UNVERIFIED, "pkg/mod.py:3"), tmp_path)
    assert (a["path"], a["start_line"], a["end_line"], a["annotation_level"]) == ("pkg/mod.py", 3, 3, "warning")


def test_annotation_path_is_relative_to_the_workspace(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "m.py").write_text("x\n")
    a = action.annotation_for(ev(Status.CONFIRMED, "m.py:1"), sub, workspace=tmp_path)
    assert a["path"] == "sub/m.py"


def test_annotation_title_and_message_are_bounded(tmp_path):
    (tmp_path / "urls.py").write_text("x\n")
    a = action.annotation_for(ev(Status.CONFIRMED, detail="d" * 100_000, text="t" * 1000), tmp_path)
    assert len(a["message"]) <= 4000 and len(a["title"]) <= 255


def test_at_most_50_annotations_per_request_rest_patched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(120):
        (repo / f"f{i}.py").write_text("x\n")
    verdict = Verdict([ev(Status.CONFIRMED, f"f{i}.py:1") for i in range(120)])
    api = FakeApi()
    result = action.create_check_run("o/r", SHA, verdict, "# report", False, repo, "t", api)
    assert result == "created"
    calls = check_calls(api)
    assert [c[0] for c in calls] == ["POST", "PATCH", "PATCH"]
    assert calls[1][1] == calls[2][1] == "https://api.github.com/repos/o/r/check-runs/555"
    sizes = [len(c[2]["output"]["annotations"]) for c in calls]
    assert sizes == [50, 50, 20]
    assert all(c[2]["output"]["title"] and c[2]["output"]["summary"] for c in calls)
    paths = [a["path"] for c in calls for a in c[2]["output"]["annotations"]]
    assert sorted(paths) == sorted(f"f{i}.py" for i in range(120))


def test_annotation_total_is_capped_with_a_note(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    n = action.MAX_ANNOTATIONS + 10
    for i in range(n):
        (repo / f"f{i}.py").write_text("x\n")
    verdict = Verdict([ev(Status.CONFIRMED, f"f{i}.py:1") for i in range(n)])
    api = FakeApi()
    action.create_check_run("o/r", SHA, verdict, "# report", False, repo, "t", api)
    calls = check_calls(api)
    assert all(len(c[2]["output"]["annotations"]) <= 50 for c in calls)
    assert sum(len(c[2]["output"]["annotations"]) for c in calls) == action.MAX_ANNOTATIONS
    assert "10 more" in calls[0][2]["output"]["summary"]


@pytest.mark.parametrize("code", [403, 404])
def test_missing_checks_permission_warns_and_continues(tmp_path, capsys, code):
    repo = tmp_path / "repo"
    repo.mkdir()
    api = FakeApi(fail={"/check-runs": code})
    rc, _ = run_pr(tmp_path, repo, api)
    assert rc == 0
    out = capsys.readouterr().out
    assert "::warning::" in out and "checks: write" in out
    assert [c for c in api.calls if c[1].endswith("/issues/7/comments") and c[0] == "POST"]  # comment still posted


def test_missing_permission_does_not_mask_fail_on_refuted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, _ = run_pr(tmp_path, repo, FakeApi(fail={"/check-runs": 403}), INPUT_FAIL_ON_REFUTED="true")
    assert rc == 1


def test_other_check_run_errors_are_not_swallowed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(urllib.error.HTTPError):
        run_pr(tmp_path, repo, FakeApi(fail={"/check-runs": 422}))


def test_check_run_input_off_makes_no_call(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _, api = run_pr(tmp_path, repo, INPUT_CHECK_RUN="false")
    assert not check_calls(api)


def test_no_check_run_without_head_sha_or_token(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    api = FakeApi()
    action.run(env(tmp_path, repo), api)  # event has no pull_request.head.sha
    assert not check_calls(api)
    _, api = run_pr(tmp_path, repo, GITHUB_TOKEN="")
    assert not check_calls(api)


def test_no_check_run_for_issue_events(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    event = tmp_path / "issue.json"
    event.write_text(json.dumps({"issue": {"number": 7, "title": "x", "body": "parse_url()", "head": {"sha": SHA}}}))
    api = FakeApi()
    action.run(env(tmp_path, repo, GITHUB_EVENT_PATH=str(event)), api)
    assert not check_calls(api)


def test_head_sha_must_look_like_a_sha():
    assert action.head_sha({"pull_request": {"head": {"sha": SHA}}}) == SHA
    for bad in ("main", "../x", "a" * 39, "g" * 40, "", None, 5):
        assert action.head_sha({"pull_request": {"head": {"sha": bad}}}) is None
    assert action.head_sha({"issue": {"head": {"sha": SHA}}}) is None
    assert action.head_sha({"pull_request": {}}) is None


def test_summary_is_truncated_below_the_limit():
    short = "# report\n| a | b |\n"
    assert action.truncate_summary(short) == short
    long = "".join(f"| row {i} | {'x' * 50} |\n" for i in range(5000))
    out = action.truncate_summary(long)
    assert len(out) <= action.SUMMARY_LIMIT < 65535
    assert "truncated" in out
    body = out.rsplit("\n\n", 1)[0]
    assert long.startswith(body) and body.endswith("|")  # cut on a line boundary, no half row
    assert len(action.truncate_summary("y" * 200_000)) <= action.SUMMARY_LIMIT


def test_check_run_summary_is_truncated(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    api = FakeApi()
    action.create_check_run("o/r", SHA, Verdict([]), "z" * 100_000, False, repo, "t", api)
    assert len(check_calls(api)[0][2]["output"]["summary"]) <= action.SUMMARY_LIMIT


def test_pr_text_never_reaches_an_annotation_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    body = "`../../etc/passwd:1` and `/etc/passwd:1` and parse_url() and `evil.py:1`"
    event = tmp_path / "pr.json"
    event.write_text(json.dumps({"pull_request": {"number": 7, "title": "x", "body": body, "head": {"sha": SHA}}}))
    api = FakeApi()
    action.run(env(tmp_path, repo, GITHUB_EVENT_PATH=str(event)), api)
    calls = check_calls(api)
    assert calls
    for call in calls:
        for a in call[2]["output"].get("annotations", []):
            assert (repo / a["path"]).resolve().is_relative_to(repo.resolve())
            assert (repo / a["path"]).is_file()


def test_printed_report_cannot_issue_workflow_commands(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_pr(tmp_path, repo)
    lines = capsys.readouterr().out.splitlines()
    start = [i for i, line in enumerate(lines) if line.startswith("::stop-commands::")]
    assert start, "report is printed without stopping workflow commands"
    token = lines[start[0]].split("::")[2]
    assert len(token) >= 16
    assert start[0] < lines.index("# claimcheck report") < lines.index(f"::{token}::")


def test_action_yml_check_run_input():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((ROOT / "action.yml").read_text())
    assert spec["inputs"]["check-run"]["default"] == "true"
    step = [s for s in spec["runs"]["steps"] if s.get("id") == "check"][0]
    assert step["env"]["INPUT_CHECK_RUN"] == "${{ inputs.check-run }}"
    assert "${{" not in step["run"]


def test_workflow_grants_checks_write():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github/workflows/claimcheck.yml").read_text())
    assert wf["permissions"]["checks"] == "write"
