"""A read-only HTML dashboard over the stored investigations, rendered on the server.

Everything stored is untrusted: report text, webhook titles and bodies, and anything derived
from them. Every value goes through `html.escape` before it reaches the page, and the pages
carry a Content-Security-Policy that allows no script at all and only the one inline style
block below, by its hash. There are no forms and only GET routes, so nothing here changes data.
"""

import base64
import hashlib
import json
import re
from html import escape
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .store import Investigation, Store

PAGE_SIZE = 50
MAX_PAGE = 100_000
STATUSES = ("received", "processing", "completed", "skipped", "failed")
EVIDENCE_STATUSES = ("confirmed", "refuted", "unverified")
EVIDENCE_COLUMNS = ("status", "kind", "claim", "basis", "detail", "source")
GITHUB_SOURCE = re.compile(r"github:(issues|pull_request):([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
                           r"([A-Za-z0-9_.-]+)#([1-9][0-9]*)")

STYLE = """
body{font:14px/1.45 system-ui,sans-serif;margin:1.5rem auto;max-width:72rem;padding:0 1rem;color:#1d1d1f}
h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:1.8rem}
a{color:#0b57d0}nav a{margin-right:.8rem}nav a.on{font-weight:700;text-decoration:none;color:inherit}
table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;padding:.35rem .5rem;
border-bottom:1px solid #ddd}th{background:#f4f4f6}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.3rem 1rem}dt{font-weight:600}dd{margin:0}
pre{white-space:pre-wrap;word-break:break-word;background:#f6f6f8;padding:.5rem;margin:0}
code,pre{font:12.5px ui-monospace,monospace}td,dd{word-break:break-word}
.s-completed,.s-confirmed{color:#137333}.s-failed,.s-refuted{color:#b3261e}
.s-received,.s-processing,.s-skipped,.s-unverified{color:#8a5a00}.muted{color:#666}
"""
_STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
HEADERS = {
    "Content-Security-Policy": (f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; img-src 'none'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def e(value: object) -> str:
    """Escaped text for element content and quoted attributes; None shows as a dash."""
    if value is None:
        return '<span class="muted">-</span>'
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return escape(value, quote=True)


def status_span(status: object, known: tuple[str, ...] = STATUSES) -> str:
    css = f' class="s-{status}"' if status in known else ""  # only known values ever reach an attribute
    return f"<span{css}>{e(status)}</span>"


def block(value: object) -> str:
    """Long or multi-line text as a <pre>, anything else inline."""
    text = value if isinstance(value, str) else None
    if text is not None and ("\n" in text or len(text) > 80):
        return f"<pre>{e(text)}</pre>"
    if value is not None and not isinstance(value, str):
        return f"<code>{e(value)}</code>"
    return e(value)


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    document = (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<title>{e(title)} - claimcheck</title><style>{STYLE}</style></head>"
                f'<body><p><a href="/dashboard">claimcheck investigations</a></p>{body}</body></html>\n')
    return HTMLResponse(document, status_code=status_code, headers=HEADERS)


def error_page(status_code: int, title: str, message: str) -> HTMLResponse:
    return page(title, f"<h1>{e(title)}</h1><p>{e(message)}</p>", status_code)


def detail_href(investigation_id: str) -> str:
    return escape(f"/dashboard/investigations/{quote(investigation_id, safe='')}", quote=True)


def source_link(source: str) -> str:
    """Only a validated webhook source can supply an external destination."""
    match = GITHUB_SOURCE.fullmatch(source)
    if match is None or match[3] in (".", ".."):
        return e(source)
    event, owner, repo, number = match.groups()
    route = "issues" if event == "issues" else "pull"
    href = f"https://github.com/{owner}/{repo}/{route}/{number}"
    return f'<a href="{e(href)}" rel="noopener noreferrer">{e(source)}</a>'


def revision_code(revision: object) -> str:
    """Keep the tested revision compact, with its full value available on hover."""
    if not isinstance(revision, str) or not revision:
        return block(revision)
    return f'<code title="{e(revision)}">{e(revision[:7])}</code>'


def list_href(status: str | None, number: int) -> str:
    query = ([("status", status)] if status else []) + ([("page", number)] if number else [])
    return escape("/dashboard" + (f"?{urlencode(query)}" if query else ""), quote=True)


def verdict_of(investigation: Investigation) -> object:
    return investigation.result.get("verdict") if isinstance(investigation.result, dict) else None


def render_list(investigations: list[Investigation], status: str | None, number: int, more: bool) -> HTMLResponse:
    on = ' class="on"'
    links = [f'<a href="{list_href(None, 0)}"{on if not status else ""}>all</a>']
    links += [f'<a href="{list_href(s, 0)}"{on if s == status else ""}>{s}</a>' for s in STATUSES]
    heading = "Investigations" + (f" with status {e(status)}" if status else "")
    parts = [f"<h1>{heading}</h1>", f"<nav>{''.join(links)}</nav>"]
    if investigations:
        rows = "".join(
            f'<tr><td><a href="{detail_href(i.id)}"><code>{e(i.id)}</code></a></td><td>{e(i.source)}</td>'
            f"<td>{status_span(i.status)}</td><td>{status_span(verdict_of(i), ('supported', 'refuted', 'unverified'))}"
            f"</td><td>{e(i.created_at)}</td></tr>" for i in investigations)
        parts.append("<table><thead><tr><th>ID</th><th>Source</th><th>Status</th><th>Verdict</th>"
                     f"<th>Created (UTC)</th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        parts.append(f"<p>No investigations{' on this page' if number > 1 else ''}.</p>")
    pager = []
    if number > 1:
        pager.append(f'<a href="{list_href(status, number - 1)}">newer</a>')
    if more:
        pager.append(f'<a href="{list_href(status, number + 1)}">older</a>')
    parts.append(f'<nav><span class="muted">page {number}</span> {" ".join(pager)}</nav>')
    return page(heading if not status else f"Investigations ({status})", "".join(parts))


def render_fields(fields: dict) -> str:
    return "<dl>" + "".join(f"<dt>{e(name)}</dt><dd>{block(value)}</dd>" for name, value in fields.items()) + "</dl>"


def render_evidence(evidence: object) -> str:
    if not isinstance(evidence, list):
        return f"<p>Evidence is not a list:</p>{block(evidence)}"
    if not evidence:
        return "<p>No checkable claims were found.</p>"
    rows = []
    for item in evidence:
        if not isinstance(item, dict):
            rows.append(f'<tr><td colspan="{len(EVIDENCE_COLUMNS)}">{block(item)}</td></tr>')
            continue
        cells = [status_span(item.get("status"), EVIDENCE_STATUSES)]
        cells += [block(item.get(column)) for column in EVIDENCE_COLUMNS[1:]]
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    head = "".join(f"<th>{column.capitalize()}</th>" for column in EVIDENCE_COLUMNS)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def render_detail(investigation: Investigation) -> HTMLResponse:
    i = investigation
    parts = [f"<h1>Investigation <code>{e(i.id)}</code></h1>",
             "<dl>" + "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in (
                 ("Source", source_link(i.source)), ("Status", status_span(i.status)), ("Created (UTC)", e(i.created_at)),
                 ("Delivery", e(i.delivery_id)), ("Worker", e(i.worker)), ("Error", block(i.error)))) + "</dl>"]
    parts.append("<h2>Result</h2>")
    result = i.result
    if result is None:
        parts.append("<p>No result yet.</p>")
    elif isinstance(result, dict):
        parts.append("<dl>" + "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in (
            ("Verdict", block(result.get("verdict"))), ("Extraction model", block(result.get("extraction_model"))),
            ("Revision", revision_code(result.get("revision"))))) + "</dl>")
        parts.append("<h2>Claims</h2>" + render_evidence(result.get("evidence", [])))
    else:
        parts.append(block(result))
    parts.append("<h2>Request</h2>")
    parts.append(render_fields(i.request) if isinstance(i.request, dict) else block(i.request))
    return page(f"Investigation {i.id}", "".join(parts))


def router(store: Store) -> APIRouter:
    routes = APIRouter(include_in_schema=False)

    @routes.get("/")
    @routes.get("/dashboard")
    def investigations(request: Request) -> HTMLResponse:
        status = request.query_params.get("status") or None
        raw = request.query_params.get("page", "1")
        if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= MAX_PAGE:
            return error_page(400, "Bad request", f"page must be a whole number from 1 to {MAX_PAGE}")
        number = int(raw)
        found = store.list(status, limit=PAGE_SIZE + 1, offset=(number - 1) * PAGE_SIZE)
        return render_list(found[:PAGE_SIZE], status, number, more=len(found) > PAGE_SIZE)

    # `:path` so an id containing a slash still reaches this handler and gets the HTML 404 page.
    @routes.get("/dashboard/investigations/{investigation_id:path}")
    def investigation(investigation_id: str) -> HTMLResponse:
        found = store.get(investigation_id)
        if found is None:
            return error_page(404, "Not found", "There is no investigation with that id.")
        return render_detail(found)

    return routes
