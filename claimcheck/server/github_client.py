"""Post the worker's evidence back to the GitHub issue or PR it came from (stdlib only).

One comment per issue/PR, found again by a hidden marker and updated in place. The token is
only ever put in the Authorization header: it is not logged, stored, or included in an error.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

from .. import report
from ..models import Claim, Evidence, Status, Verdict

MARKER = "<!-- claimcheck-service -->"
DEFAULT_API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 20
# The source the webhook stores: github:<event>:<owner/repo>#<number> (webhook.IssueEvent.source).
_SOURCE = re.compile(r"github:(?:issues|pull_request):(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)")


class GitHubError(Exception):
    """A write-back that did not happen. The message never contains the token."""


def parse_source(source: str) -> tuple[str, int] | None:
    """(owner/repo, number) of a webhook investigation, or None. The repo comes from an untrusted
    payload, so anything that is not a plain `owner/name` (e.g. `..`) is refused."""
    match = _SOURCE.fullmatch(source)
    if match is None or any(part in (".", "..") for part in match["repo"].split("/")):
        return None
    return match["repo"], int(match["number"])


def api_base(env: Mapping[str, str]) -> str:
    """CLAIMCHECK_GITHUB_API (default https://api.github.com): http(s), a host, no credentials."""
    value = env.get("CLAIMCHECK_GITHUB_API") or DEFAULT_API
    message = "CLAIMCHECK_GITHUB_API must be an http(s) URL with a host and valid port, no credentials, query or fragment"
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or "@" in parsed.netloc \
                or parsed.query or parsed.fragment or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError
        parsed.port  # urlsplit defers port validation until this property is read
    except ValueError:
        raise ValueError(message) from None
    return value.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would resend the Authorization header to wherever a redirect points."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(method: str, url: str, token: str, data=None) -> str:
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, body, {"Authorization": f"Bearer {token}",
                                                 "Accept": "application/vnd.github+json",
                                                 "X-GitHub-Api-Version": "2022-11-28",
                                                 "Content-Type": "application/json"}, method=method)
    with _OPENER.open(request, timeout=30) as response:
        return response.read().decode()


def result_markdown(result: dict) -> str:
    """`report.to_markdown` of a stored result (the JSON `report.to_json` wrote)."""
    evidence = [Evidence(Claim(row.get("kind", ""), row.get("claim", "")), Status(row["status"]), row.get("detail", ""),
                         row.get("source") or "", row.get("basis", "observed")) for row in result.get("evidence", [])]
    return report.to_markdown(Verdict(evidence, result.get("revision") or ""), result.get("extraction_model"))


class CommentWriter:
    def __init__(self, token: str, api: str = DEFAULT_API, http: Callable[..., str] = _request):
        self._token, self.api, self._http = token, api.rstrip("/"), http

    def __repr__(self) -> str:  # never show the token
        return f"CommentWriter(api={self._scrub(self.api)!r})"

    def post(self, source: str, markdown: str) -> str:
        """Update the marked comment on the source issue/PR, or create it: "updated" or "created"."""
        target = parse_source(source)
        if target is None:
            raise GitHubError("the investigation's source is not a GitHub issue or pull request")
        repo, number = target
        body = MARKER + "\n" + markdown
        try:
            existing = self._find(repo, number)
            if existing is not None:
                self._call("PATCH", f"/repos/{repo}/issues/comments/{existing}", {"body": body})
                return "updated"
            self._call("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
            return "created"
        except GitHubError:
            raise
        except Exception as error:  # e.g. a response that is not the JSON we expect
            raise GitHubError(self._scrub(f"unexpected GitHub API response: {type(error).__name__}: {error}")) from None

    def _find(self, repo: str, number: int) -> int | None:
        for page in range(1, MAX_PAGES + 1):
            comments = json.loads(self._call("GET", f"/repos/{repo}/issues/{number}/comments"
                                                    f"?per_page={PER_PAGE}&page={page}"))
            if not isinstance(comments, list):
                raise GitHubError("GitHub API returned an invalid comments list")
            for comment in comments:
                if MARKER in (comment.get("body") or ""):
                    comment_id = comment["id"]
                    if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id < 1:
                        raise GitHubError("GitHub API returned an invalid comment id")
                    return comment_id
            if len(comments) < PER_PAGE:
                return None
        raise GitHubError("GitHub API comment pagination limit reached; refusing to create a duplicate")

    def _call(self, method: str, path: str, data=None) -> str:
        what = f"GitHub API {method} {path.split('?')[0]}"
        try:
            return self._http(method, self.api + path, self._token, data=data)
        except urllib.error.HTTPError as error:
            error.close()
            raise GitHubError(self._scrub(f"{what}: HTTP {error.code}")) from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise GitHubError(self._scrub(f"{what}: network error ({type(reason).__name__})")) from None
        except Exception as error:
            raise GitHubError(self._scrub(f"{what}: {type(error).__name__}: {error}")) from None

    def _scrub(self, message: str) -> str:
        return message.replace(self._token, "[token]") if self._token else message


def from_env(env: Mapping[str, str]) -> CommentWriter | None:
    """A writer when CLAIMCHECK_GITHUB_TOKEN is set, else None. ValueError for a bad API URL."""
    token = env.get("CLAIMCHECK_GITHUB_TOKEN") or ""
    if not token:
        return None
    return CommentWriter(token, api_base(env))
