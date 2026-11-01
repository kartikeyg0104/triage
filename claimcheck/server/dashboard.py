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
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from .store import Investigation, Store

PAGE_SIZE = 50
MAX_PAGE = 100_000
STATUSES = ("received", "processing", "completed", "skipped", "failed")
EVIDENCE_STATUSES = ("confirmed", "refuted", "unverified")
EVIDENCE_COLUMNS = ("status", "kind", "claim", "basis", "detail", "source")
GITHUB_SOURCE = re.compile(r"github:(issues|pull_request):([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
                           r"([A-Za-z0-9_.-]+)#([1-9][0-9]*)")

PICO_PATH = "/dashboard/static/pico.min.css"
# Pico CSS v2.0.6 (MIT, see static/PICO_LICENSE.md), vendored so nothing is loaded from another host.
PICO_CSS = (Path(__file__).parent / "static" / "pico.min.css").read_bytes()

# Small additions on top of Pico: status badges, the filter bar and the definition lists.
STYLE = """
:root{--pico-font-size:95%}
header.container{padding-block:.75rem 0}
header.container nav{border-bottom:1px solid var(--pico-muted-border-color)}
.brand{font-weight:700;font-size:1.15rem;color:var(--pico-contrast);text-decoration:none}
main.container{padding-block:1.5rem 3rem}
hgroup h1{margin-bottom:.25rem}h1 code{font-size:.8em}
.badge{display:inline-block;padding:.1rem .6rem;border-radius:999px;font-size:.8em;font-weight:600;
line-height:1.6;white-space:nowrap;background:var(--pico-muted-border-color);color:var(--pico-color)}
.s-completed,.s-confirmed,.s-supported{background:#d4f4dd;color:#0b5a21}
.s-failed,.s-refuted{background:#fde1df;color:#8c1d18}
.s-received,.s-processing,.s-skipped,.s-unverified{background:#fbefc9;color:#6b4600}
@media (prefers-color-scheme:dark){
.s-completed,.s-confirmed,.s-supported{background:#123a20;color:#93e2aa}
.s-failed,.s-refuted{background:#45181a;color:#ffb3ad}
.s-received,.s-processing,.s-skipped,.s-unverified{background:#3d2f0b;color:#f5d27a}}
nav.filters{display:flex;flex-wrap:wrap;gap:.4rem;justify-content:flex-start;margin-bottom:1.25rem}
nav.filters a{padding:.3rem .9rem;border:1px solid var(--pico-muted-border-color);border-radius:999px;
font-size:.875rem;text-decoration:none;color:var(--pico-color)}
nav.filters a[aria-current]{background:var(--pico-primary-background);border-color:transparent;
color:var(--pico-primary-inverse)}
nav.pager{justify-content:flex-end;gap:1rem;align-items:center}
article>header h2{margin:0;font-size:1.05rem}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.45rem 1.5rem;margin:0}
dt{font-weight:600;color:var(--pico-muted-color)}dd{margin:0}
td,dd{overflow-wrap:break-word}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:.75rem}
.muted{color:var(--pico-muted-color)}
td a code{color:var(--pico-primary);text-decoration:underline;text-underline-offset:3px;background:none;padding:0}
td time,td:first-child{white-space:nowrap}
section.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));gap:.75rem;margin-bottom:1.5rem}
.tile{padding:.9rem 1rem;border-radius:var(--pico-border-radius);background:var(--pico-card-background-color);
box-shadow:var(--pico-card-box-shadow);border-top:3px solid var(--pico-muted-border-color)}
.tile strong{display:block;font-size:1.7rem;line-height:1.1}.tile span{color:var(--pico-muted-color);font-size:.85rem}
.t-supported{border-top-color:#2e9e57}.t-refuted{border-top-color:#d93a2e}.t-unverified,.t-pending{border-top-color:#d9a21b}
.t-failed{border-top-color:#8a8a8a}
.banner{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem 1.25rem;padding:1rem 1.25rem;margin-bottom:1.5rem;
border-radius:var(--pico-border-radius);border-left:6px solid var(--pico-muted-border-color);
background:var(--pico-card-background-color);box-shadow:var(--pico-card-box-shadow)}
.banner strong{font-size:1.5rem;text-transform:capitalize}
.v-supported{border-left-color:#2e9e57}.v-refuted{border-left-color:#d93a2e}.v-unverified{border-left-color:#d9a21b}
tr.c-confirmed td:first-child{box-shadow:inset 4px 0 #2e9e57}tr.c-refuted td:first-child{box-shadow:inset 4px 0 #d93a2e}
tr.c-unverified td:first-child{box-shadow:inset 4px 0 #d9a21b}
details summary{font-size:.9rem;color:var(--pico-muted-color)}
"""
_STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
HEADERS = {
    "Content-Security-Policy": (f"default-src 'none'; style-src 'self' 'sha256-{_STYLE_HASH}'; img-src 'none'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
VERDICTS = ("supported", "refuted", "unverified")


def e(value: object) -> str:
    """Escaped text for element content and quoted attributes; None shows as a dash."""
    if value is None:
        return '<span class="muted">-</span>'
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return escape(value, quote=True)


def status_span(status: object, known: tuple[str, ...] = STATUSES) -> str:
    css = f"badge s-{status}" if status in known else "badge"  # only known values ever reach an attribute
    return f'<span class="{css}">{e(status)}</span>'


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
                f'<meta name="color-scheme" content="light dark">'
                f"<title>{e(title)} - claimcheck</title>"
                f'<link rel="stylesheet" href="{PICO_PATH}"><style>{STYLE}</style></head><body>'
                '<header class="container"><nav><ul><li><a class="brand" href="/dashboard">claimcheck</a></li></ul>'
                '<ul><li><span class="muted">investigations</span></li></ul></nav></header>'
                f'<main class="container">{body}</main></body></html>\n')
    return HTMLResponse(document, status_code=status_code, headers=HEADERS)


def error_page(status_code: int, title: str, message: str) -> HTMLResponse:
    return page(title, f'<article><h1>{e(title)}</h1><p>{e(message)}</p>'
                       '<a href="/dashboard">Back to all investigations</a></article>', status_code)


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


def when(value: object) -> str:
    """A short UTC time; the stored ISO value stays in `datetime`."""
    if not isinstance(value, str) or len(value) < 16 or value[10] != "T":
        return e(value)
    return f'<time datetime="{e(value)}">{e(value[:10])} {e(value[11:16])} UTC</time>'


def source_short(source: object) -> str:
    """`octo/widgets #7 PR` for a webhook source, with the raw source on hover; anything else as is."""
    match = GITHUB_SOURCE.fullmatch(source) if isinstance(source, str) else None
    if match is None:
        return e(source)
    event, owner, repo, number = match.groups()
    kind = "issue" if event == "issues" else "PR"
    return f'<span title="{e(source)}">{e(owner)}/{e(repo)} #{e(number)} <small class="muted">{kind}</small></span>'


def tiles(counts: dict) -> str:
    names = ("total", "supported", "refuted", "unverified", "pending", "failed")
    return '<section class="tiles">' + "".join(
        f'<div class="tile t-{name}"><strong>{int(counts.get(name, 0))}</strong><span>{name}</span></div>'
        for name in names) + "</section>"


def long_text(value: object) -> str:
    """Long multi-line text folds away; short values render as before."""
    if isinstance(value, str) and value.count("\n") > 8:
        return f"<details><summary>{value.count(chr(10)) + 1} lines</summary>{block(value)}</details>"
    return block(value)


def list_href(status: str | None, number: int) -> str:
    query = ([("status", status)] if status else []) + ([("page", number)] if number else [])
    return escape("/dashboard" + (f"?{urlencode(query)}" if query else ""), quote=True)


def verdict_of(investigation: Investigation) -> object:
    return investigation.result.get("verdict") if isinstance(investigation.result, dict) else None


def card(title: str, body: str) -> str:
    return f"<article><header><h2>{title}</h2></header>{body}</article>"


def table(head: str, rows: str) -> str:
    return (f'<div class="overflow-auto"><table class="striped"><thead><tr>{head}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def render_list(investigations: list[Investigation], status: str | None, number: int, more: bool,
                counts: dict | None = None) -> HTMLResponse:
    on = ' aria-current="page"'
    links = [f'<a href="{list_href(None, 0)}"{on if not status else ""}>all</a>']
    links += [f'<a href="{list_href(s, 0)}"{on if s == status else ""}>{s}</a>' for s in STATUSES]
    heading = "Investigations" + (f" with status {e(status)}" if status else "")
    shown = f"{len(investigations)} on page {number}, newest first"
    parts = [f'<hgroup><h1>{heading}</h1><p class="muted">{shown}</p></hgroup>',
             tiles(counts) if counts else "",
             f'<nav class="filters" aria-label="Filter by status">{"".join(links)}</nav>']
    if investigations:
        rows = "".join(
            f'<tr><td><a href="{detail_href(i.id)}"><code>{e(i.id)}</code></a></td><td>{source_short(i.source)}</td>'
            f"<td>{status_span(i.status)}</td><td>{status_span(verdict_of(i), VERDICTS)}</td>"
            f"<td>{when(i.created_at)}</td></tr>" for i in investigations)
        head = "<th>ID</th><th>Source</th><th>Status</th><th>Verdict</th><th>Created (UTC)</th>"
        parts.append(table(head, rows))
    else:
        parts.append(f'<article><p>No investigations{" on this page" if number > 1 else ""}.</p>'
                     '<p class="muted">Start one with <code>POST /v1/investigations</code> '
                     '(<code>{"repo": "...", "report": "..."}</code>) or point a GitHub webhook at '
                     '<code>/v1/github/webhook</code>.</p></article>')
    pager = []
    if number > 1:
        pager.append(f'<a href="{list_href(status, number - 1)}">&larr; newer</a>')
    if more:
        pager.append(f'<a href="{list_href(status, number + 1)}">older &rarr;</a>')
    parts.append(f'<nav class="pager" aria-label="Pages"><span class="muted">page {number}</span>{"".join(pager)}</nav>')
    return page(heading if not status else f"Investigations ({status})", "".join(parts))


def render_fields(fields: dict) -> str:
    return "<dl>" + "".join(f"<dt>{e(name)}</dt><dd>{long_text(value)}</dd>" for name, value in fields.items()) + "</dl>"


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
        tint = f' class="c-{item.get("status")}"' if item.get("status") in EVIDENCE_STATUSES else ""
        rows.append(f"<tr{tint}>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    return table("".join(f"<th>{column.capitalize()}</th>" for column in EVIDENCE_COLUMNS), "".join(rows))


def banner(result: dict) -> str:
    verdict = result.get("verdict")
    css = f" v-{verdict}" if verdict in VERDICTS else ""
    evidence = result.get("evidence")
    items = [item for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []
    counts = [f"{n} {status}" for status in EVIDENCE_STATUSES
              if (n := sum(item.get("status") == status for item in items))]
    summary = ", ".join(counts) if counts else "no checkable claims"
    return (f'<div class="banner{css}"><span class="muted">Verdict</span><strong>{e(verdict)}</strong>'
            f'<span>{e(summary)}</span></div>')


def render_detail(investigation: Investigation) -> HTMLResponse:
    i = investigation
    parts = [f'<hgroup><h1>Investigation <code>{e(i.id)}</code></h1><p class="muted">{e(i.source)}</p></hgroup>',
             card("Overview", "<dl>" + "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in (
                 ("Source", source_link(i.source)), ("Status", status_span(i.status)), ("Created", when(i.created_at)),
                 ("Delivery", e(i.delivery_id)), ("Worker", e(i.worker)), ("Error", block(i.error)))) + "</dl>")]
    result = i.result
    if result is None:
        parts.append(card("Result", "<p>No result yet.</p>"))
    elif isinstance(result, dict):
        parts.insert(1, banner(result))
        parts.append(card("Result", "<dl>" + "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in (
            ("Verdict", status_span(result.get("verdict"), VERDICTS)),
            ("Extraction model", block(result.get("extraction_model"))),
            ("Revision", revision_code(result.get("revision"))))) + "</dl>"))
        parts.append(card("Claims", render_evidence(result.get("evidence", []))))
    else:
        parts.append(card("Result", block(result)))
    parts.append(card("Request", render_fields(i.request) if isinstance(i.request, dict) else block(i.request)))
    return page(f"Investigation {i.id}", "".join(parts))


def router(store: Store) -> APIRouter:
    routes = APIRouter(include_in_schema=False)

    @routes.get(PICO_PATH)
    def stylesheet() -> Response:
        return Response(PICO_CSS, media_type="text/css; charset=utf-8", headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=86400"})

    @routes.get("/")
    @routes.get("/dashboard")
    def investigations(request: Request) -> HTMLResponse:
        status = request.query_params.get("status") or None
        raw = request.query_params.get("page", "1")
        if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= MAX_PAGE:
            return error_page(400, "Bad request", f"page must be a whole number from 1 to {MAX_PAGE}")
        number = int(raw)
        found = store.list(status, limit=PAGE_SIZE + 1, offset=(number - 1) * PAGE_SIZE)
        return render_list(found[:PAGE_SIZE], status, number, more=len(found) > PAGE_SIZE, counts=store.summary())

    # `:path` so an id containing a slash still reaches this handler and gets the HTML 404 page.
    @routes.get("/dashboard/investigations/{investigation_id:path}")
    def investigation(investigation_id: str) -> HTMLResponse:
        found = store.get(investigation_id)
        if found is None:
            return error_page(404, "Not found", "There is no investigation with that id.")
        return render_detail(found)

    return routes
