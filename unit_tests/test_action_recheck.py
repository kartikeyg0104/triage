"""Maintainer rechecks use the issue or PR report, never the comment as a command."""

import json
import urllib.error
from pathlib import Path

import pytest

from claimcheck import action


HEAD = "b" * 40
PR_BODY = "The fix changes `parse_url_v2()`."
DIFF = "--- a/urls.py\n+++ b/urls.py\n@@ -1 +1 @@\n-old\n+new\n"


class FakeApi:
    def __init__(self):
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.comment = action.MARKER + "\nold report"
        self.pr = {"number": 7, "title": "Fix", "body": PR_BODY,
                   "base": {"sha": "a" * 40}, "head": {"sha": HEAD}}

    def __call__(self, method: str, url: str, token: str, accept: str = "", data=None) -> str:
        self.calls.append((method, url, accept, data))
        if method == "GET" and url == f"{action.API}/repos/o/r/pulls/7":
            return DIFF if accept == "application/vnd.github.diff" else json.dumps(self.pr)
        if method == "GET" and url == f"{action.API}/repos/o/r/issues/7/comments?per_page=100":
            return json.dumps([{"id": 99, "body": self.comment},
                               {"id": 100, "body": "/claimcheck"}])
        if method == "PATCH" and url == f"{action.API}/repos/o/r/issues/comments/99":
            self.comment = data["body"]
            return "{}"
        if method == "POST" and url == f"{action.API}/repos/o/r/check-runs":
            return json.dumps({"id": 555})
        raise AssertionError(f"unexpected API call: {method} {url}")


@pytest.fixture
def context(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "urls.py").write_text("def parse_url(s):\n    return s\n")
    env = {"GITHUB_EVENT_NAME": "issue_comment", "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
           "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t", "INPUT_PATH": str(repo),
           "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(tmp_path / "outputs"),
           "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    return env, FakeApi()


def comment_event(env: dict, association: str | None = "MEMBER", body: str | None = "/claimcheck",
                  is_pr: bool = False, event_action: str = "created") -> dict:
    issue = {"number": 7, "title": "Bug", "body": "`parse_url()` crashes.",
             "author_association": "OWNER"}
    if is_pr:
        issue["pull_request"] = {"url": "https://untrusted.invalid/pulls/999"}
    comment = {"body": body}
    if association is not None:
        comment["author_association"] = association
    event = {"action": event_action, "issue": issue, "comment": comment,
             "sender": {"author_association": "OWNER"}}
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(event))
    return event


def read_report(env: dict) -> dict:
    return json.loads((Path(env["RUNNER_TEMP"]) / action.REPORT_NAME).read_text())


def assert_ignored(env: dict, api: FakeApi) -> None:
    assert api.calls == []
    for path in (env["GITHUB_OUTPUT"], env["GITHUB_STEP_SUMMARY"],
                 str(Path(env["RUNNER_TEMP"]) / action.REPORT_NAME)):
        assert not Path(path).exists()


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
@pytest.mark.parametrize("body", ["/claimcheck", " /claimcheck ", "\t\n/claimcheck\r\n"])
@pytest.mark.parametrize("is_pr", [False, True], ids=["issue", "pr"])
def test_maintainers_recheck_and_update_the_existing_comment(context, association, body, is_pr):
    env, api = context
    event = comment_event(env, association, body, is_pr)
    event["issue"]["author_association"] = "NONE"
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(event))
    assert action.run(env, api) == 0
    writes = [(method, url) for method, url, _, _ in api.calls if method != "GET"]
    expected = [("PATCH", f"{action.API}/repos/o/r/issues/comments/99")]
    if is_pr:
        expected.append(("POST", f"{action.API}/repos/o/r/check-runs"))
    assert writes == expected
    assert api.comment.startswith(action.MARKER + "\n")
    assert "old report" not in api.comment and "/claimcheck" not in api.comment
    evidence = read_report(env)["evidence"]
    symbols = [e for e in evidence if e["kind"] == "symbol"]
    assert [e["claim"] for e in symbols] == (["parse_url_v2() exists"] if is_pr else ["parse_url() exists"])
    assert [e["status"] for e in symbols] == (["refuted"] if is_pr else ["confirmed"])
    assert any(e["kind"] == "pr_tests" for e in evidence) == is_pr
    assert Path(env["GITHUB_STEP_SUMMARY"]).read_text() == api.comment.removeprefix(action.MARKER + "\n")
    assert "verdict=" in Path(env["GITHUB_OUTPUT"]).read_text()
    if is_pr:
        checks = [data for _, url, _, data in api.calls if url.endswith("/check-runs")]
        assert checks[0]["head_sha"] == HEAD
        assert checks[0]["output"]["summary"] in api.comment


@pytest.mark.parametrize("association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER",
                                        "MANNEQUIN", "NONE", None, "owner", "UNKNOWN",
                                        "OWNER\n::error::untrusted"])
@pytest.mark.parametrize("is_pr", [False, True], ids=["issue", "pr"])
def test_other_associations_are_ignored_with_a_log_line(context, capsys, association, is_pr):
    env, api = context
    comment_event(env, association, is_pr=is_pr)
    assert action.run(env, api) == 0
    assert_ignored(env, api)
    output = capsys.readouterr().out
    assert "claimcheck:" in output and "ignor" in output.lower()
    assert "::error::untrusted" not in output


@pytest.mark.parametrize("body", ["", " \n\t", None, "/claimcheck now", "/claimchecker", "/Claimcheck",
                                 "please /claimcheck", "`/claimcheck`", "/claimcheck\n/claimcheck",
                                 action.MARKER + "\nreport", "/claimcheck; touch MARKER",
                                 "/claimcheck $(touch MARKER)", "/claimcheck `touch MARKER`",
                                 "/claimcheck\n::error::COMMENT_INJECTION"])
@pytest.mark.parametrize("is_pr", [False, True], ids=["issue", "pr"])
def test_other_comment_bodies_are_ignored(context, capsys, monkeypatch, body, is_pr):
    env, api = context
    seen = []
    real = action.run_check

    def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(action, "run_check", spy)
    comment_event(env, body=body, is_pr=is_pr)
    assert action.run(env, api) == 0
    assert seen == []
    assert_ignored(env, api)
    output = capsys.readouterr().out
    assert "MARKER" not in output and "COMMENT_INJECTION" not in output
    assert not list(Path(env["RUNNER_TEMP"]).rglob("MARKER"))


@pytest.mark.parametrize("event_action", ["edited", "deleted"])
def test_only_created_comments_trigger_a_recheck(context, event_action):
    env, api = context
    comment_event(env, event_action=event_action)
    assert action.run(env, api) == 0
    assert_ignored(env, api)


def test_issue_edit_is_used_on_the_next_recheck(context):
    env, api = context
    event = comment_event(env)
    event["issue"]["body"] = "`parse_url_v2()` crashes."
    path = Path(env["GITHUB_EVENT_PATH"])
    path.write_text(json.dumps(event))
    action.run(env, api)
    assert "parse_url_v2() exists" in api.comment
    event["issue"]["body"] = "`parse_url()` crashes."
    path.write_text(json.dumps(event))
    action.run(env, api)
    assert "parse_url_v2" not in api.comment and "parse_url() exists" in api.comment
    assert [method for method, url, _, _ in api.calls if "/issues/comments/99" in url] == ["PATCH", "PATCH"]


@pytest.mark.parametrize("is_pr", [False, True], ids=["issue", "pr"])
def test_comment_never_becomes_report_text_or_repro(context, monkeypatch, is_pr):
    env, api = context
    comment_event(env, body="\n /claimcheck \t", is_pr=is_pr)
    seen, repros = [], []
    real = action.run_check

    def spy(root, text, diff, repro, expect, **kwargs):
        seen.append((text, diff, repro, expect))
        return real(root, text, diff, repro, expect, **kwargs)

    def executor(*args, **kwargs):
        repros.append((args, kwargs))
        raise AssertionError("comment must never run a repro")

    monkeypatch.setattr(action, "run_check", spy)
    assert action.run(env, api, run_repro=executor) == 0
    assert seen == [(f"Fix\n\n{PR_BODY}" if is_pr else "Bug\n\n`parse_url()` crashes.",
                     DIFF if is_pr else None, None, "")]
    assert repros == []


@pytest.mark.parametrize("code", [403, 404])
def test_pr_lookup_failure_produces_no_verdict(context, code):
    env, api = context
    comment_event(env, is_pr=True)

    def denied(method, url, token, **kwargs):
        raise urllib.error.HTTPError(url, code, "denied", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        action.run(env, denied)
    assert_ignored(env, api)


def test_workflow_listens_for_created_issue_comments():
    yaml = pytest.importorskip("yaml")
    path = Path(__file__).parent.parent / ".github/workflows/claimcheck.yml"
    workflow = yaml.safe_load(path.read_text())
    triggers = workflow.get("on") or workflow.get(True)
    assert triggers.get("issue_comment") == {"types": ["created"]}
    for step in workflow["jobs"]["claims"]["steps"]:
        assert "${{" not in step.get("run", "")
        assert "github.event.comment.body" not in json.dumps(step)
