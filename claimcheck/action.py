"""GitHub Action entry point: check the claims in a PR or issue and post the evidence.

The PR/issue text is untrusted. It is read from the event payload file and handed to the
checks as data. It is never put in a shell command, and a repro command found in it is never run.
"""

import json
import os
import re
import secrets
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from . import report
from .cli import run_check
from .models import Evidence, Status, Verdict

MARKER = "<!-- claimcheck -->"
API = "https://api.github.com"
REPORT_NAME = "claimcheck-report.json"
OUTPUT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

CHECK_NAME = "claimcheck"
SUMMARY_LIMIT = 60000  # GitHub rejects check run output.summary over 65535 characters
ANNOTATIONS_PER_REQUEST = 50  # GitHub's per-request limit for check run annotations
MAX_ANNOTATIONS = 500  # total cap; anything beyond is noted in the summary
SHA = re.compile(r"[0-9a-f]{40}")
SOURCE = re.compile(r"([A-Za-z0-9_./-]+):([1-9][0-9]{0,8})")
LEVEL = {Status.CONFIRMED: "notice", Status.UNVERIFIED: "warning", Status.REFUTED: "warning"}


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


def head_sha(event: dict) -> str | None:
    """The PR head commit from the event payload, only if it is a full hex SHA."""
    sha = ((event.get("pull_request") or {}).get("head") or {}).get("sha")
    return sha if isinstance(sha, str) and SHA.fullmatch(sha) else None


def check_conclusion(outcome: str, fail_on_refuted: bool) -> str:
    if outcome == "supported":
        return "success"
    if outcome == "refuted" and fail_on_refuted:
        return "failure"
    return "neutral"


def truncate_summary(text: str, limit: int = SUMMARY_LIMIT) -> str:
    """Keep the summary under GitHub's limit, cutting on a line boundary so no table row is half shown."""
    if len(text) <= limit:
        return text
    note = "\n\n_Report truncated to fit the check run summary; the full report is in the PR comment and job summary._\n"
    body = text[:limit - len(note)]
    if "\n" in body:
        body = body[:body.rindex("\n")]
    return body + note


def annotation_for(evidence: Evidence, root: Path, workspace: Path | None = None) -> dict | None:
    """An annotation for evidence whose source is a repo-relative file:line that exists under root, else None.

    Rejects absolute paths, '..', backslashes and anything resolving (e.g. via a symlink) outside root.
    """
    match = SOURCE.fullmatch(evidence.source or "")
    if not match:
        return None
    rel = PurePosixPath(match.group(1))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts == (".",):
        return None
    root = root.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    path = target.relative_to(root)
    if workspace is not None and root.is_relative_to(workspace.resolve()):
        path = root.relative_to(workspace.resolve()) / path
    line = int(match.group(2))
    return {"path": path.as_posix(), "start_line": line, "end_line": line,
            "annotation_level": LEVEL[evidence.status],
            "title": f"{evidence.status.value}: {evidence.claim.text}"[:255],
            "message": evidence.detail[:4000] or evidence.status.value}


def create_check_run(repo: str, sha: str, verdict: Verdict, markdown: str, fail_on_refuted: bool,
                     root: Path, token: str, http=_request, workspace: Path | None = None) -> str:
    """Create a completed check run on sha; annotations beyond the first 50 are sent in PATCH batches."""
    annotations = [a for a in (annotation_for(e, root, workspace) for e in verdict.evidence) if a]
    dropped = max(0, len(annotations) - MAX_ANNOTATIONS)
    annotations = annotations[:MAX_ANNOTATIONS]
    if dropped:
        markdown = f"_{dropped} more annotation(s) not shown; see the table below._\n\n" + markdown
    counts = ", ".join(f"{sum(e.status == s for e in verdict.evidence)} {s.value}" for s in Status)
    output = {"title": f"claimcheck: {verdict.outcome} ({counts})", "summary": truncate_summary(markdown)}
    batches = [annotations[i:i + ANNOTATIONS_PER_REQUEST]
               for i in range(0, len(annotations), ANNOTATIONS_PER_REQUEST)] or [[]]
    created = json.loads(http("POST", f"{API}/repos/{repo}/check-runs", token, data={
        "name": CHECK_NAME, "head_sha": sha, "status": "completed",
        "conclusion": check_conclusion(verdict.outcome, fail_on_refuted),
        "output": {**output, "annotations": batches[0]},
    }))
    for batch in batches[1:]:
        http("PATCH", f"{API}/repos/{repo}/check-runs/{int(created['id'])}", token,
             data={"output": {**output, "annotations": batch}})
    return "created"


def print_untrusted(text: str) -> None:
    """Print text that holds PR-derived content with workflow commands (::warning file=... etc.) disabled."""
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}")
    print(text)
    print(f"::{token}::")


def run(env: dict, http=_request) -> int:
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    found = event_text(event)
    if not found:
        print("claimcheck: no pull request or issue in this event; nothing to check")
        return 0
    number, text, is_pr = found
    repo_name, token = env["GITHUB_REPOSITORY"], env.get("GITHUB_TOKEN", "")
    diff = fetch_diff(repo_name, number, token, http) if is_pr and token else None
    root = Path(env.get("INPUT_PATH") or ".")
    verdict, model = run_check(root, text, diff, env.get("INPUT_REPRO") or None,
                               env.get("INPUT_EXPECT", ""), use_model=False)
    fail_on_refuted = env.get("INPUT_FAIL_ON_REFUTED") == "true"
    markdown = report.to_markdown(verdict, model)
    report_path = write_report(report.to_json(verdict, model), env)
    counts = {status.value: sum(e.status == status for e in verdict.evidence) for status in Status}
    write_outputs({"verdict": verdict.outcome, **counts, "report-json": str(report_path)}, env.get("GITHUB_OUTPUT"))
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(markdown)
    if env.get("INPUT_COMMENT", "true") == "true" and token:
        print(f"claimcheck: comment {post_comment(repo_name, number, MARKER + chr(10) + markdown, token, http)}")
    sha = head_sha(event)
    if env.get("INPUT_CHECK_RUN", "true") == "true" and token and sha:
        workspace = Path(env["GITHUB_WORKSPACE"]) if env.get("GITHUB_WORKSPACE") else None
        try:
            result = create_check_run(repo_name, sha, verdict, markdown, fail_on_refuted, root, token, http, workspace)
            print(f"claimcheck: check run {result}")
        except urllib.error.HTTPError as error:
            if error.code not in (403, 404):
                raise
            print(f"::warning::claimcheck: could not create the check run (HTTP {error.code}); "
                  "grant the workflow 'checks: write' permission to get one")
    print_untrusted(markdown)
    return 1 if fail_on_refuted and verdict.outcome == "refuted" else 0


if __name__ == "__main__":
    sys.exit(run(dict(os.environ)))
