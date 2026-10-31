"""FastAPI app: run checks on request, and record signed GitHub webhook events.

Neither endpoint runs a repro or any command taken from request or event text. Webhook
events are only recorded here (status "received"); `claimcheck.server.worker` checks them
later. Nothing is fetched from GitHub.
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from .. import report
from ..cli import run_check
from . import dashboard, webhook
from .paths import inside_root
from .store import Store

MAX_BODY = 1024 * 1024


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a "repro" field is refused, not silently ignored

    repo: str
    report: str
    diff: str | None = None


def _no_repro(*args, **kwargs):
    raise RuntimeError("the service API does not run repro commands")


async def read_body(request: Request, limit: int) -> bytes:
    """Read at most `limit` bytes. Content-Length is checked first, but the stream is counted
    too: a chunked request has no length, and the limit must hold before anything is parsed."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "request body too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, "request body too large")
    return bytes(body)


def resolve_repo(root: Path, relative: str) -> Path:
    path = inside_root(root, relative)
    if path is None:
        raise HTTPException(400, "repo must be a directory inside the repo root")
    return path


def create_app(db_path: Path, repo_root: Path, webhook_secret: str, max_body: int = MAX_BODY) -> FastAPI:
    app = FastAPI(title="claimcheck")
    store = Store(db_path)
    root = repo_root.resolve()

    @app.post("/v1/investigations", status_code=201)
    async def create_investigation(request: Request) -> dict:
        try:
            body = InvestigationRequest.model_validate_json(await read_body(request, max_body))
        except ValidationError as error:
            raise HTTPException(422, error.errors(include_url=False, include_input=False)) from None
        repo = resolve_repo(root, body.repo)
        verdict, model = await run_in_threadpool(run_check, repo, body.report, body.diff, repro=None, expect="",
                                                 use_model=False, run_repro=_no_repro)
        result = json.loads(report.to_json(verdict, model))
        investigation, _ = await run_in_threadpool(store.create, "api", "completed", body.model_dump(), result)
        return investigation.to_dict()

    @app.get("/v1/investigations/{investigation_id}")
    def get_investigation(investigation_id: str) -> dict:
        investigation = store.get(investigation_id)
        if investigation is None:
            raise HTTPException(404, "no such investigation")
        return investigation.to_dict()

    @app.post("/v1/github/webhook", status_code=202)
    async def github_webhook(request: Request) -> dict:
        if not webhook_secret:
            raise HTTPException(503, "no webhook secret is configured, so every webhook request is refused")
        body = await read_body(request, max_body)
        if not webhook.verify_signature(webhook_secret, body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "missing or invalid X-Hub-Signature-256")
        event = request.headers.get("x-github-event", "")
        if event not in webhook.PAYLOAD_KEY:
            return {"status": "ignored", "event": event}
        delivery = request.headers.get("x-github-delivery")
        if not delivery:
            raise HTTPException(400, "missing X-GitHub-Delivery")
        try:
            parsed = webhook.parse_event(event, json.loads(body))
        except ValueError as error:  # includes JSONDecodeError and UnicodeDecodeError
            raise HTTPException(400, f"malformed {event} payload: {error}") from None
        investigation, created = await run_in_threadpool(store.create, parsed.source, "received", parsed.request(),
                                                         delivery_id=delivery)
        return {"id": investigation.id, "status": investigation.status, "duplicate": not created}

    app.include_router(dashboard.router(store))
    return app
