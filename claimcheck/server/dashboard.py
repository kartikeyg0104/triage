"""A read-only HTML dashboard over the stored investigations, rendered on the server.

Everything stored is untrusted: report text, webhook titles and bodies, and anything derived
from them. Every value goes through `html.escape` before it reaches the page, and the pages
carry a Content-Security-Policy that allows no script at all and only the one inline style
block below, by its hash. Bars and charts are inline SVG with numeric attributes, so no inline
style attribute is ever needed. The only form is a GET search; every route is GET, so nothing
here changes data. "Live" mode is a meta refresh, not script.
"""

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .store import Investigation, Store

PAGE_SIZE = 50
MAX_PAGE = 100_000
MAX_QUERY = 200
STRIP_CELLS = 24
LIVE_SECONDS = 10
STATUSES = ("received", "processing", "completed", "skipped", "failed")
VERDICTS = ("supported", "refuted", "unverified")
SORTS = ("newest", "oldest")
EVIDENCE_STATUSES = ("confirmed", "refuted", "unverified")
EVIDENCE_COLUMNS = ("status", "kind", "claim", "basis", "detail", "source")
GITHUB_SOURCE = re.compile(r"github:(issues|pull_request):([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
                           r"([A-Za-z0-9_.-]+)#([1-9][0-9]*)")

STYLE = """
:root{--ink:#172033;--ink-2:#222d45;--ink-text:#c9d1e3;--fog:#eef1f6;--panel:#fff;--text:#1d2433;--muted:#5b6476;
--line:#d9dee8;--pine:#1f8a70;--garnet:#c63d4f;--ochre:#b97f0e;--slate:#8a93a6;--signal:#3557d6;--wash:#e6ebf5}
@media (prefers-color-scheme:dark){:root{--ink:#0a0e15;--ink-2:#151c2a;--ink-text:#aab4c8;--fog:#0f141d;--panel:#161d29;
--text:#e6eaf2;--muted:#98a2b3;--line:#263042;--pine:#3fb68f;--garnet:#e2596b;--ochre:#d8a23a;--slate:#6b7489;
--signal:#7d93ff;--wash:#1d2535}}
*{box-sizing:border-box}html{color-scheme:light dark}
body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--fog);color:var(--text);
font-variant-numeric:tabular-nums}
a{color:var(--signal)}a:focus-visible,input:focus-visible,button:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
code,pre{font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.app{display:grid;grid-template-columns:15.5rem 1fr;min-height:100vh}
.sidebar{background:var(--ink);color:var(--ink-text);padding:1.25rem .9rem;position:sticky;top:0;height:100vh;overflow:auto}
.brand{display:flex;align-items:center;gap:.55rem;color:#fff;text-decoration:none;font-weight:700;font-size:1.05rem;
padding:.2rem .5rem 1.1rem;letter-spacing:-.01em}
.brand svg{flex:none}
.sidebar h2{font-size:.78rem;font-weight:600;color:var(--slate);margin:1.2rem .5rem .35rem}
.sidebar a{display:flex;align-items:center;gap:.55rem;color:var(--ink-text);text-decoration:none;padding:.42rem .55rem;
border-radius:.45rem}
.sidebar a:hover{background:var(--ink-2);color:#fff}
.sidebar a[aria-current]{background:var(--ink-2);color:#fff;box-shadow:inset 3px 0 var(--signal)}
.sidebar b{margin-left:auto;font-weight:600;font-size:.8rem;color:var(--slate)}
.dot{width:.55rem;height:.55rem;border-radius:50%;flex:none;background:var(--slate)}
.d-supported,.d-completed{background:var(--pine)}.d-refuted,.d-failed{background:var(--garnet)}
.d-unverified,.d-received,.d-processing{background:var(--ochre)}.d-skipped{background:var(--slate)}
.main{min-width:0}
.menubar{position:sticky;top:0;z-index:2;display:flex;flex-wrap:wrap;align-items:center;gap:.75rem 1rem;
padding:.8rem 1.75rem;background:var(--panel);border-bottom:1px solid var(--line)}
.menubar h1{font-size:1.15rem;margin:0;margin-right:auto;letter-spacing:-.01em}
.search{display:flex;gap:.4rem}
.search input{width:16rem;max-width:52vw;padding:.42rem .7rem;border:1px solid var(--line);border-radius:.45rem;
background:var(--fog);color:var(--text);font:inherit}
.search button,.chip{padding:.42rem .8rem;border:1px solid var(--line);border-radius:.45rem;background:var(--panel);
color:var(--text);font:inherit;text-decoration:none;cursor:pointer;white-space:nowrap}
.chip[aria-pressed=true]{border-color:var(--signal);color:var(--signal)}
.live-dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;background:var(--slate);margin-right:.35rem}
.chip[aria-pressed=true] .live-dot{background:var(--pine);animation:pulse 2s ease-in-out infinite}
.content{padding:1.5rem 1.75rem 3rem;max-width:84rem}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:.7rem;padding:1.1rem 1.25rem;margin-bottom:1.25rem}
.panel>h2{font-size:.95rem;margin:0 0 .8rem}
.overview{display:grid;grid-template-columns:2fr 1fr;gap:1.25rem}
.overview .panel{margin:0}
.lede{display:flex;align-items:baseline;gap:.6rem;margin:0 0 .8rem}.lede strong{font-size:2.1rem;line-height:1;letter-spacing:-.02em}
.lede span{color:var(--muted)}
svg.strip{display:block;width:100%;height:1.35rem;border-radius:.35rem}
svg.strip rect{transform-origin:left;animation:grow .7s cubic-bezier(.2,.7,.2,1) both}
.legend{display:flex;flex-wrap:wrap;gap:.4rem 1.1rem;margin:.75rem 0 0;padding:0;list-style:none;color:var(--muted)}
.legend li{display:flex;align-items:center;gap:.4rem}.legend b{color:var(--text)}
.f-supported,.c-confirmed{fill:var(--pine)}.f-refuted,.c-refuted{fill:var(--garnet)}
.f-unverified,.c-unverified{fill:var(--ochre)}.f-waiting{fill:var(--slate)}.f-other{fill:var(--line)}
.c-other{fill:var(--slate)}.f-bar{fill:var(--signal)}.f-track{fill:var(--wash)}
svg.activity{display:block;width:100%;height:5.5rem}
.axis{display:flex;justify-content:space-between;color:var(--muted);font-size:.75rem;margin-top:.25rem}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.75rem;margin:1.25rem 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:.7rem;padding:.8rem 1rem;text-decoration:none;color:inherit}
a.kpi:hover{border-color:var(--signal)}
.kpi strong{display:block;font-size:1.55rem;line-height:1.15}.kpi span{color:var(--muted);font-size:.85rem}
.kpi svg{display:block;width:100%;height:.35rem;margin-top:.55rem;border-radius:.2rem}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;vertical-align:middle;padding:.6rem .7rem;border-bottom:1px solid var(--line)}
th{font-size:.8rem;font-weight:600;color:var(--muted);white-space:nowrap}
tbody tr:hover{background:var(--wash)}
td,dd{overflow-wrap:anywhere}
.tablewrap{overflow-x:auto}
.id{font-weight:600}.sub{display:block;color:var(--muted);font-size:.82rem}
.pill{display:inline-flex;align-items:center;gap:.35rem;padding:.12rem .55rem;border-radius:1rem;font-size:.8rem;
font-weight:600;white-space:nowrap;background:var(--wash);color:var(--muted)}
.p-completed,.p-supported,.p-confirmed{background:color-mix(in srgb,var(--pine) 16%,transparent);color:var(--pine)}
.p-failed,.p-refuted{background:color-mix(in srgb,var(--garnet) 16%,transparent);color:var(--garnet)}
.p-received,.p-processing,.p-unverified{background:color-mix(in srgb,var(--ochre) 18%,transparent);color:var(--ochre)}
svg.cells{display:block;height:.9rem}
.more{color:var(--muted);font-size:.8rem;margin-left:.3rem}
.pager{display:flex;align-items:center;justify-content:flex-end;gap:1rem;margin-top:1rem;color:var(--muted)}
.empty{text-align:center;padding:2.5rem 1rem;color:var(--muted)}.empty strong{display:block;color:var(--text);font-size:1.05rem}
.banner{display:grid;grid-template-columns:auto 1fr;gap:.4rem 1.5rem;align-items:center;padding:1.2rem 1.4rem;
border-radius:.7rem;background:var(--panel);border:1px solid var(--line);border-left:.45rem solid var(--slate);margin-bottom:1.25rem}
.banner strong{font-size:1.9rem;line-height:1;text-transform:capitalize;letter-spacing:-.02em}
.banner .counts{color:var(--muted)}
.v-supported{border-left-color:var(--pine)}.v-refuted{border-left-color:var(--garnet)}.v-unverified{border-left-color:var(--ochre)}
.banner svg.cells{grid-column:2;height:1.1rem}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.5rem 1.5rem;margin:0}dt{color:var(--muted)}dd{margin:0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:.7rem .8rem;background:var(--fog);border-radius:.45rem}
details summary{cursor:pointer;color:var(--muted)}
.muted{color:var(--muted)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem}
@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes pulse{50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){svg.strip rect,.chip[aria-pressed=true] .live-dot{animation:none}}
@media (max-width:60rem){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto;display:flex;flex-wrap:wrap;
gap:.25rem;padding:.6rem}.sidebar h2{display:none}.brand{padding:.4rem .5rem;width:100%}.overview,.grid2{grid-template-columns:1fr}
.menubar,.content{padding-left:1rem;padding-right:1rem}}
"""
_STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
HEADERS = {
    "Content-Security-Policy": (f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; img-src 'none'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
LOGO = ('<svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true"><rect x="1" y="7" width="5" height="8" rx="1.5" '
        'class="c-confirmed"/><rect x="8.5" y="7" width="5" height="8" rx="1.5" class="c-refuted"/>'
        '<rect x="16" y="7" width="5" height="8" rx="1.5" class="c-unverified"/></svg>')


def e(value: object) -> str:
    """Escaped text for element content and quoted attributes; None shows as a dash."""
    if value is None:
        return '<span class="muted">-</span>'
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return escape(value, quote=True)


def status_span(status: object, known: tuple[str, ...] = STATUSES) -> str:
    css = f"pill p-{status}" if status in known else "pill"  # only known values ever reach an attribute
    return f'<span class="{css}">{e(status)}</span>'


def block(value: object) -> str:
    """Long or multi-line text as a <pre>, anything else inline."""
    text = value if isinstance(value, str) else None
    if text is not None and ("\n" in text or len(text) > 80):
        return f"<pre>{e(text)}</pre>"
    if value is not None and not isinstance(value, str):
        return f"<code>{e(value)}</code>"
    return e(value)


class View:
    """The list's filters, kept together so every link and form carries them consistently."""

    def __init__(self, status=None, verdict=None, query="", sort="newest", live=False):
        self.status, self.verdict, self.query, self.sort, self.live = status, verdict, query, sort, live

    def href(self, path: str = "/dashboard", page: int = 0, **changes) -> str:
        values = {"status": self.status, "verdict": self.verdict, "q": self.query,
                  "sort": None if self.sort == "newest" else self.sort, "live": "1" if self.live else None}
        values.update(changes)
        query = [(k, v) for k, v in values.items() if v] + ([("page", page)] if page else [])
        return escape(path + (f"?{urlencode(query)}" if query else ""), quote=True)


def list_href(status: str | None, number: int) -> str:
    return View(status=status).href(page=number)


def sidebar(counts: dict, view: View, here: str) -> str:
    def link(href: str, label: str, count: int | None, dot: str | None, current: bool) -> str:
        marker = f'<span class="dot d-{dot}"></span>' if dot else ""
        badge = f"<b>{count}</b>" if count is not None else ""
        on = ' aria-current="page"' if current else ""
        return f'<a href="{href}"{on}>{marker}{label}{badge}</a>'

    unfiltered = here == "list" and not (view.status or view.verdict or view.query)
    parts = [f'<nav class="sidebar" aria-label="Dashboard"><a class="brand" href="/dashboard">{LOGO}claimcheck</a>',
             link("/dashboard", "All investigations", counts["total"], None, unfiltered),
             link("/dashboard/health", "Health", None, None, here == "health"), "<h2>Verdict</h2>"]
    parts += [link(View(verdict=v).href(), v.capitalize(), counts["verdict"][v], v, view.verdict == v) for v in VERDICTS]
    parts.append("<h2>Status</h2>")
    parts += [link(View(status=s).href(), s.capitalize(), counts["status"][s], s, view.status == s) for s in STATUSES]
    return "".join(parts) + "</nav>"


def page(title: str, body: str, status_code: int = 200, *, counts: dict | None = None, view: View | None = None,
         here: str = "", tools: str = "", live: bool = False) -> HTMLResponse:
    counts = counts or {"total": 0, "status": dict.fromkeys(STATUSES, 0), "verdict": dict.fromkeys(VERDICTS, 0)}
    refresh = f'<meta http-equiv="refresh" content="{LIVE_SECONDS}">' if live else ""
    document = (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}'
                f"<title>{e(title)} - claimcheck</title><style>{STYLE}</style></head>"
                f'<body><div class="app">{sidebar(counts, view or View(), here)}<div class="main">'
                f'<header class="menubar"><h1>{e(title)}</h1>{tools}</header>'
                f'<main class="content">{body}</main></div></div></body></html>\n')
    return HTMLResponse(document, status_code=status_code, headers=HEADERS)


def error_page(status_code: int, title: str, message: str) -> HTMLResponse:
    return page(title, f'<div class="panel empty"><strong>{e(message)}</strong>'
                       '<p><a href="/dashboard">Back to all investigations</a></p></div>', status_code)


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


def source_short(source: object) -> str:
    """`octo/widgets #7 · issue` for a webhook source; anything else as is."""
    match = GITHUB_SOURCE.fullmatch(source) if isinstance(source, str) else None
    if match is None:
        return e(source)
    event, owner, repo, number = match.groups()
    return f'{e(owner)}/{e(repo)} #{e(number)} {"issue" if event == "issues" else "pull request"}'


def revision_code(revision: object) -> str:
    """Keep the tested revision compact, with its full value available on hover."""
    if not isinstance(revision, str) or not revision:
        return block(revision)
    return f'<code title="{e(revision)}">{e(revision[:7])}</code>'


def ago(value: object, now: datetime | None = None) -> str:
    """'4 min ago' with the stored UTC time on hover; unparseable values shown as they are."""
    try:
        then = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        then = None
    if then is None:
        return e(value)
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds()))
    text = ("just now" if seconds < 60 else f"{seconds // 60} min ago" if seconds < 3600
            else f"{seconds // 3600} h ago" if seconds < 86400 else f"{seconds // 86400} d ago")
    return f'<time datetime="{e(value)}" title="{e(value)}">{text}</time>'


def verdict_of(investigation: Investigation) -> object:
    return investigation.result.get("verdict") if isinstance(investigation.result, dict) else None


def evidence_items(result: object) -> list[dict]:
    evidence = result.get("evidence") if isinstance(result, dict) else None
    return [item for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []


def claim_cells(result: object) -> str:
    """One square per claim, coloured by its status: the investigation at a glance."""
    items = evidence_items(result)
    if not items:
        return '<span class="muted">no claims</span>'
    shown = items[:STRIP_CELLS]
    rects = "".join(
        f'<rect class="cell c-{item.get("status") if item.get("status") in EVIDENCE_STATUSES else "other"}" '
        f'x="{n * 14}" y="0" width="11" height="11" rx="2.5"><title>{e(item.get("claim"))}: {e(item.get("status"))}</title></rect>'
        for n, item in enumerate(shown))
    extra = f'<span class="more">+{len(items) - len(shown)}</span>' if len(items) > len(shown) else ""
    width = len(shown) * 14 - 3
    return (f'<svg class="cells" width="{width}" viewBox="0 0 {width} 11" role="img" '
            f'aria-label="{len(items)} claims">{rects}</svg>{extra}')


def strip(parts: list[tuple[str, str, int]]) -> str:
    """A full-width bar split by share; each part is (css, label, count)."""
    total = sum(count for _, _, count in parts)
    if not total:
        return '<svg class="strip" viewBox="0 0 1000 20" preserveAspectRatio="none" aria-hidden="true"><rect class="f-track" width="1000" height="20"/></svg>'
    x, rects = 0.0, []
    for css, label, count in parts:
        if count:
            width = 1000 * count / total
            rects.append(f'<rect class="{css}" x="{x:.2f}" y="0" width="{width:.2f}" height="20">'
                         f"<title>{label}: {count}</title></rect>")
            x += width
    return f'<svg class="strip" viewBox="0 0 1000 20" preserveAspectRatio="none" role="img" aria-label="Verdicts">{"".join(rects)}</svg>'


def share_bar(css: str, count: int, total: int) -> str:
    width = 100 * count / total if total else 0
    return (f'<svg viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true"><rect class="f-track" width="100" height="4"/>'
            f'<rect class="{css}" width="{width:.2f}" height="4"/></svg>')


def activity_chart(hours: list[tuple[str, int]]) -> str:
    peak = max((count for _, count in hours), default=0) or 1
    step = 1000 / len(hours)
    bars = "".join(
        f'<rect class="f-bar" x="{n * step + 4:.1f}" y="{100 - 96 * count / peak:.1f}" width="{step - 8:.1f}" '
        f'height="{96 * count / peak:.1f}" rx="3"><title>{e(hour[11:13])}:00 UTC: {count}</title></rect>'
        for n, (hour, count) in enumerate(hours))
    track = "".join(f'<rect class="f-track" x="{n * step + 4:.1f}" y="98" width="{step - 8:.1f}" height="2"/>' for n in range(len(hours)))
    return f'<svg class="activity" viewBox="0 0 1000 100" preserveAspectRatio="none" role="img" aria-label="Investigations per hour">{track}{bars}</svg>'


def overview(counts: dict, hours: list[tuple[str, int]], view: View) -> str:
    status, verdict, total = counts["status"], counts["verdict"], counts["total"]
    waiting = status["received"] + status["processing"]
    other = total - sum(verdict.values()) - waiting
    parts = [("f-supported", "Supported", verdict["supported"]), ("f-refuted", "Refuted", verdict["refuted"]),
             ("f-unverified", "Unverified", verdict["unverified"]), ("f-waiting", "Waiting", waiting),
             ("f-other", "Skipped or failed", other)]
    legend = "".join(f'<li><span class="dot d-{css[2:] if css[2:] in VERDICTS else "skipped"}"></span>{label} <b>{count}</b></li>'
                     for css, label, count in parts)
    checked = sum(verdict.values())
    lede = (f'<p class="lede"><strong>{checked}</strong><span>of {total} investigations have a verdict</span></p>')
    first, last = (hours[0][0][11:13], hours[-1][0][11:13]) if hours else ("", "")
    kpis = [(View(verdict="supported").href(), "f-supported", "Supported", verdict["supported"]),
            (View(verdict="refuted").href(), "f-refuted", "Refuted", verdict["refuted"]),
            (View(verdict="unverified").href(), "f-unverified", "Unverified", verdict["unverified"]),
            (View(status="received").href(), "f-waiting", "Waiting", waiting),
            (View(status="failed").href(), "f-refuted", "Failed", status["failed"])]
    tiles = "".join(f'<a class="kpi" href="{href}"><strong>{count}</strong><span>{label}</span>{share_bar(css, count, total)}</a>'
                    for href, css, label, count in kpis)
    return (f'<section class="overview"><div class="panel"><h2>Verdicts</h2>{lede}{strip(parts)}<ul class="legend">{legend}</ul></div>'
            f'<div class="panel"><h2>Last 24 hours</h2>{activity_chart(hours)}<div class="axis"><span>{first}:00</span>'
            f'<span>{last}:00 UTC</span></div></div></section><section class="kpis">{tiles}</section>')


def tools(view: View) -> str:
    hidden = "".join(f'<input type="hidden" name="{name}" value="{e(value)}">' for name, value in
                     (("status", view.status), ("verdict", view.verdict), ("sort", None if view.sort == "newest" else view.sort),
                      ("live", "1" if view.live else None)) if value)
    search = (f'<form class="search" method="get" action="/dashboard" role="search">{hidden}'
              f'<input type="search" name="q" value="{e(view.query)}" placeholder="Search id or source" '
              f'aria-label="Search investigations" maxlength="{MAX_QUERY}"><button type="submit">Search</button></form>')
    other = "oldest" if view.sort == "newest" else "newest"
    sort = f'<a class="chip" href="{view.href(sort=None if other == "newest" else other)}">{"Oldest first" if other == "oldest" else "Newest first"}</a>'
    live = (f'<a class="chip" href="{view.href(live=None if view.live else "1")}" aria-pressed="{"true" if view.live else "false"}">'
            f'<span class="live-dot"></span>{"Live" if view.live else "Go live"}</a>')
    return search + sort + live


def render_list(investigations: list[Investigation], view: View, number: int, more: bool, counts: dict,
                hours: list[tuple[str, int]]) -> HTMLResponse:
    filters = [f"status {view.status}" if view.status else "", f"verdict {view.verdict}" if view.verdict else "",
               f'"{view.query}"' if view.query else ""]
    shown = ", ".join(f for f in filters if f)
    title = "Investigations" + (f" ({shown})" if shown else "")
    parts = [overview(counts, hours, view)] if not shown and number == 1 else []
    if investigations:
        now = datetime.now(timezone.utc)
        rows = "".join(
            f'<tr><td><a class="id" href="{detail_href(i.id)}"><code>{e(i.id[:12])}</code></a>'
            f'<span class="sub" title="{e(i.source)}">{source_short(i.source)}</span></td><td>{status_span(i.status)}</td>'
            f"<td>{status_span(verdict_of(i), VERDICTS)}</td><td>{claim_cells(i.result)}</td><td>{ago(i.created_at, now)}</td></tr>"
            for i in investigations)
        parts.append('<div class="panel"><div class="tablewrap"><table><thead><tr><th>Investigation</th><th>Status</th>'
                     f"<th>Verdict</th><th>Claims</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table></div>")
    else:
        hint = ("Try a different search or filter." if shown else
                'Send a report with <code>POST /v1/investigations</code> or point a GitHub webhook at <code>/v1/github/webhook</code>.')
        parts.append(f'<div class="panel"><div class="empty"><strong>No investigations'
                     f'{" on this page" if number > 1 else ""}</strong><p>{hint}</p></div>')
    pager = [f'<span>page {number}</span>']
    if number > 1:
        pager.append(f'<a href="{view.href(page=number - 1)}">Newer</a>')
    if more:
        pager.append(f'<a href="{view.href(page=number + 1)}">Older</a>')
    parts.append(f'<nav class="pager" aria-label="Pages">{"".join(pager)}</nav></div>')
    return page(title, "".join(parts), counts=counts, view=view, here="list", tools=tools(view), live=view.live)


def render_fields(fields: dict) -> str:
    return "<dl>" + "".join(f"<dt>{e(name)}</dt><dd>{block(value)}</dd>" for name, value in fields.items()) + "</dl>"


def render_evidence(evidence: object) -> str:
    if not isinstance(evidence, list):
        return f"<p>Evidence is not a list:</p>{block(evidence)}"
    if not evidence:
        return '<div class="empty"><strong>No checkable claims were found.</strong></div>'
    rows = []
    for item in evidence:
        if not isinstance(item, dict):
            rows.append(f'<tr><td colspan="{len(EVIDENCE_COLUMNS)}">{block(item)}</td></tr>')
            continue
        cells = [status_span(item.get("status"), EVIDENCE_STATUSES)]
        cells += [block(item.get(column)) for column in EVIDENCE_COLUMNS[1:]]
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    head = "".join(f"<th>{column.capitalize()}</th>" for column in EVIDENCE_COLUMNS)
    return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def banner(result: dict) -> str:
    verdict = result.get("verdict")
    css = f" v-{verdict}" if verdict in VERDICTS else ""
    items = evidence_items(result)
    counts = [f"{n} {status}" for status in EVIDENCE_STATUSES if (n := sum(item.get("status") == status for item in items))]
    return (f'<div class="banner{css}"><strong>{e(verdict)}</strong><span class="counts">'
            f'{e(", ".join(counts) if counts else "no checkable claims")}</span>{claim_cells(result)}</div>')


def panel(title: str, body: str) -> str:
    return f'<section class="panel"><h2>{title}</h2>{body}</section>'


def render_detail(investigation: Investigation, counts: dict) -> HTMLResponse:
    i = investigation
    parts = []
    result = i.result
    if isinstance(result, dict):
        parts.append(banner(result))
    about = "<dl>" + "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in (
        ("Investigation", f"<code>{e(i.id)}</code>"), ("Source", source_link(i.source)), ("Status", status_span(i.status)),
        ("Created", ago(i.created_at)), ("Delivery", e(i.delivery_id)), ("Worker", e(i.worker)),
        ("Error", block(i.error)))) + "</dl>"
    if result is None:
        outcome = '<div class="empty"><strong>No result yet.</strong><p>The worker has not checked this one.</p></div>'
    elif isinstance(result, dict):
        outcome = "<dl>" + "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in (
            ("Verdict", status_span(result.get("verdict"), VERDICTS)),
            ("Extraction model", block(result.get("extraction_model"))),
            ("Revision", revision_code(result.get("revision"))))) + "</dl>"
    else:
        outcome = block(result)
    parts.append(f'<div class="grid2">{panel("About", about)}{panel("Result", outcome)}</div>')
    if isinstance(result, dict):
        parts.append(panel("Claims", render_evidence(result.get("evidence", []))))
    request = render_fields(i.request) if isinstance(i.request, dict) else block(i.request)
    parts.append(panel("Request", f"<details><summary>Show what was submitted</summary>{request}</details>"))
    return page(f"Investigation {i.id[:12]}", "".join(parts), counts=counts, here="detail")


def render_health(health: dict, counts: dict) -> HTMLResponse:
    status = counts["status"]
    tiles = "".join(f'<div class="kpi"><strong>{value}</strong><span>{label}</span></div>' for label, value in (
        ("Waiting", status["received"]), ("Processing", status["processing"]), ("Failed", status["failed"]),
        ("Oldest waiting", ago(health["oldest_waiting"]) if health["oldest_waiting"] else "none")))
    processing = "".join(f'<tr><td><a href="{detail_href(i.id)}"><code>{e(i.id[:12])}</code></a></td><td>{e(i.worker)}</td>'
                         f"<td>{ago(i.created_at)}</td></tr>" for i in health["processing"])
    failed = "".join(f'<tr><td><a href="{detail_href(i.id)}"><code>{e(i.id[:12])}</code></a>'
                     f'<span class="sub">{source_short(i.source)}</span></td><td>{block(i.error)}</td><td>{ago(i.created_at)}</td></tr>'
                     for i in health["failed"])

    def table(head: str, rows: str, empty: str) -> str:
        if not rows:
            return f'<div class="empty"><strong>{empty}</strong></div>'
        return f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'

    body = (f'<section class="kpis">{tiles}</section><div class="grid2">'
            + panel("Processing now", table("<th>Investigation</th><th>Worker</th><th>Created</th>", processing,
                                            "Nothing is being processed."))
            + panel("Recent failures", table("<th>Investigation</th><th>Error</th><th>Created</th>", failed, "No failures."))
            + "</div>")
    return page("Health", body, counts=counts, here="health")


def router(store: Store) -> APIRouter:
    routes = APIRouter(include_in_schema=False)

    @routes.get("/")
    @routes.get("/dashboard")
    def investigations(request: Request) -> HTMLResponse:
        params = request.query_params
        raw = params.get("page", "1")
        if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= MAX_PAGE:
            return error_page(400, "Bad request", f"page must be a whole number from 1 to {MAX_PAGE}")
        verdict, sort, query = params.get("verdict") or None, params.get("sort") or "newest", params.get("q", "").strip()
        if verdict is not None and verdict not in VERDICTS:
            return error_page(400, "Bad request", f"verdict must be one of: {', '.join(VERDICTS)}")
        if sort not in SORTS:
            return error_page(400, "Bad request", f"sort must be one of: {', '.join(SORTS)}")
        view = View(params.get("status") or None, verdict, query[:MAX_QUERY], sort, params.get("live") == "1")
        number = int(raw)
        found = store.list(view.status, limit=PAGE_SIZE + 1, offset=(number - 1) * PAGE_SIZE, verdict=view.verdict,
                           query=view.query or None, oldest_first=sort == "oldest")
        return render_list(found[:PAGE_SIZE], view, number, len(found) > PAGE_SIZE, store.summary(), store.activity())

    @routes.get("/dashboard/health")
    def health() -> HTMLResponse:
        return render_health(store.health(), store.summary())

    # `:path` so an id containing a slash still reaches this handler and gets the HTML 404 page.
    @routes.get("/dashboard/investigations/{investigation_id:path}")
    def investigation(investigation_id: str) -> HTMLResponse:
        found = store.get(investigation_id)
        if found is None:
            return error_page(404, "Not found", "There is no investigation with that id.")
        return render_detail(found, store.summary())

    return routes
