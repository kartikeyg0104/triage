"""GitHub Action entry point: check the claims in a PR or issue and post the evidence.

The PR/issue text is untrusted. It is read from the event payload file and handed to the
checks as data. It is never put in a shell command, and a repro command found in it is never run.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

from . import report
from .cli import run_check

MARKER = "<!-- claimcheck -->"
API = "https://api.github.com"


def event_text(event: dict) -> tuple[int, str, bool] | None:
    """Return (number, text, is_pr) for a pull_request or issues event."""
    for key, is_pr in (("pull_request", True), ("issue", False)):
        item = event.get(key)
        if item:
            return item["number"], f"{item.get('title') or ''}\n\n{item.get('body') or ''}", is_pr
    return None


def _request(method: str, url: str, token: str, accept: str = "application/vnd.github+json", data=None):
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, body, {"Authorization": f"Bearer {token}", "Accept": accept,
                                                 "X-GitHub-Api-Version": "2022-11-28"}, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def fetch_diff(repo: str, number: int, token: str, http=_request) -> str:
    return http("GET", f"{API}/repos/{repo}/pulls/{number}", token, accept="application/vnd.github.diff")


def post_comment(repo: str, number: int, body: str, token: str, http=_request) -> str:
    """Update our earlier comment if there is one, otherwise create it."""
    comments = json.loads(http("GET", f"{API}/repos/{repo}/issues/{number}/comments?per_page=100", token))
    for comment in comments:
        if MARKER in comment.get("body", ""):
            http("PATCH", f"{API}/repos/{repo}/issues/comments/{comment['id']}", token, data={"body": body})
            return "updated"
    http("POST", f"{API}/repos/{repo}/issues/{number}/comments", token, data={"body": body})
    return "created"


def run(env: dict, http=_request) -> int:
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    found = event_text(event)
    if not found:
        print("claimcheck: no pull request or issue in this event; nothing to check")
        return 0
    number, text, is_pr = found
    repo_name, token = env["GITHUB_REPOSITORY"], env.get("GITHUB_TOKEN", "")
    diff = fetch_diff(repo_name, number, token, http) if is_pr and token else None
    verdict, model = run_check(
        Path(env.get("INPUT_PATH") or "."), text, diff, env.get("INPUT_REPRO") or None,
        env.get("INPUT_EXPECT", ""), use_model=False,
    )
    markdown = report.to_markdown(verdict, model)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(markdown)
    if env.get("INPUT_COMMENT", "true") == "true" and token:
        print(f"claimcheck: comment {post_comment(repo_name, number, MARKER + chr(10) + markdown, token, http)}")
    print(markdown)
    refuted_fails = env.get("INPUT_FAIL_ON_REFUTED") == "true" and verdict.outcome == "refuted"
    return 1 if refuted_fails else 0


if __name__ == "__main__":
    sys.exit(run(dict(os.environ)))
