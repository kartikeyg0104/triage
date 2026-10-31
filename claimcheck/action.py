"""GitHub Action entry point: check the claims in a PR or issue and post the evidence.

The PR/issue text is untrusted. It is read from the event payload file and handed to the
checks as data. It is never put in a shell command, and a repro command found in it is never run.
"""

import json
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import report
from .cli import run_check
from .models import Status

MARKER = "<!-- claimcheck -->"
API = "https://api.github.com"
REPORT_NAME = "claimcheck-report.json"
OUTPUT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


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


def write_report(json_text: str, env: dict) -> Path:
    """Write the JSON report under $RUNNER_TEMP (or the system temp dir) and return its path."""
    path = Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / REPORT_NAME
    path.write_text(json_text)
    return path


def write_outputs(outputs: dict, output_file: str | None) -> None:
    """Append step outputs to $GITHUB_OUTPUT in the single-line name=value form.

    Names must be fixed identifiers chosen by this code, never PR text. Values are flattened to one
    line, so nothing in a value can start a new output line or a name<<DELIMITER block.
    """
    lines = []
    for name, value in outputs.items():
        if not OUTPUT_NAME.fullmatch(name):
            raise ValueError(f"unsafe output name: {name!r}")
        lines.append(f"{name}={' '.join(str(value).splitlines())}\n")
    if output_file:
        with open(output_file, "a") as out:
            out.writelines(lines)


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
    report_path = write_report(report.to_json(verdict, model), env)
    counts = {status.value: sum(e.status == status for e in verdict.evidence) for status in Status}
    write_outputs({"verdict": verdict.outcome, **counts, "report-json": str(report_path)}, env.get("GITHUB_OUTPUT"))
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
