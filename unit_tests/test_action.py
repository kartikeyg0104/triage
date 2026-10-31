import json

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
    assert "no test covers the change" in post[2]["body"]


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
