# claimcheck service

A small HTTP service: groundwork for a GitHub App. It runs static checks on request and records signed GitHub webhook events. It never runs a repro or any command taken from request or event text.

```bash
python -m pip install -e ".[server]"
export CLAIMCHECK_WEBHOOK_SECRET="$(openssl rand -hex 32)"   # same value as the GitHub webhook secret
export CLAIMCHECK_API_TOKEN="$(openssl rand -hex 32)"        # bearer token for the API and dashboard
python -m claimcheck.server --db claimcheck.db --repo-root examples   # binds 127.0.0.1:8080
```

Options: `--host` (default `127.0.0.1`), `--port` (default `8080`).

## Authentication

With `CLAIMCHECK_API_TOKEN` set, every request to `/v1/investigations` (and anything under it) and to the dashboard (`/`, `/dashboard` and anything under it) needs `Authorization: Bearer <token>`. Anything else, including a missing header, another scheme or a wrong token, gets `401` with `WWW-Authenticate: Bearer`: a JSON `{"detail": ...}` for the API, and an HTML `401` page with the dashboard's security headers for dashboard paths. The scheme name is case-insensitive; the token is compared with `hmac.compare_digest`.

- **The webhook does not use the token.** `POST /v1/github/webhook` is authenticated only by its HMAC signature (below), because GitHub cannot send a bearer token. A bearer header on it is ignored.
- **No token means open, on loopback only.** Without `CLAIMCHECK_API_TOKEN` (or with it empty) the API and dashboard need no token, as before, but the server only binds a loopback host (`127.0.0.1` or anything in `127.0.0.0/8`, `::1`, `localhost`). Any other `--host`, such as `0.0.0.0`, is refused with an error naming `CLAIMCHECK_API_TOKEN` and exit code `2`, before anything listens. This keeps local use and the existing tests unchanged.
- The token is read from the environment, not a flag, so it does not appear in the process list. Use a long random value; anyone with it can read every stored investigation and run checks.

```bash
curl -s localhost:8080/v1/investigations/<id> -H "Authorization: Bearer $CLAIMCHECK_API_TOKEN"
```

## Endpoints

| Method and path | Does |
|---|---|
| `POST /v1/investigations` | Body `{"repo": "<dir inside --repo-root>", "report": "...", "diff": "..."}` (`diff` optional). Runs the symbol and PR-test checks with the regex extractor (no model, no repro) and returns `201` with `{"id", "status": "completed", "result": <same JSON as --json>, ...}`. |
| `GET /v1/investigations/{id}` | The stored investigation, or `404`. |
| `POST /v1/github/webhook` | A GitHub webhook (content type `application/json`). `issues` and `pull_request` events are recorded with status `received`, source `github:<event>:<owner/repo>#<number>` and the claim names found in the title and body. Returns `202`. |

```bash
curl -s localhost:8080/v1/investigations -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $CLAIMCHECK_API_TOKEN" \
  -d '{"repo": "sample_repo", "report": "RCE in parse_url_v2()"}'
```

Everything is stored in one SQLite file (table `investigations`), so results survive a restart. A file from an older version gets the new columns (`error`, `worker`, `claimed_at`, `attempts`) when it is opened.

## Worker

Webhook events are checked by a separate process that shares the SQLite file:

```bash
echo '{"octo/widgets": "widgets"}' > repos.json   # GitHub repo -> checkout under --repo-root
python -m claimcheck.server.worker --db claimcheck.db --repo-root /srv/checkouts --repos repos.json
```

It claims the oldest `received` row, runs the same static checks as `POST /v1/investigations` on the issue or PR title and body (regex extractor, no model, no repro, no diff), stores the result and logs one JSON line (`{"id", "status", "worker"}`) per row. From Python: `claimcheck.server.worker.process_one(store, root, repos, worker_id)` handles one row, `run(...)` loops.

| Status | Meaning |
|---|---|
| `processing` | Claimed by the worker named in `worker` |
| `completed` | `result` holds the same JSON as `--json` |
| `skipped` | The repo is not in `--repos`; `error` says which. Nothing was read |
| `failed` | The check raised, the event had no repo or text, or the checkout no longer resolves inside `--repo-root`; `error` says why |

Options: `--once` (exit when the queue is empty, e.g. from cron), `--poll` (seconds between polls, default 2), `--stale-after` (default 600), `--worker-id` (default `host:pid`). `SIGTERM` or `SIGINT` finish the current row, then exit `0`.

- **Concurrency.** Any number of workers can share the file. A row is claimed in one `BEGIN IMMEDIATE` transaction (select the oldest, mark it `processing` with the worker id), so two workers never get the same row. Tested with two worker processes racing over 320 rows: every row was handled exactly once.
- **Crashed workers.** A `processing` row older than `--stale-after` seconds is claimed again by the next worker. A result is stored only while the worker still holds the claim, so a slow worker whose row was taken over cannot overwrite the newer attempt. After 3 claims that never finished, the row is `failed` (`abandoned after 3 attempts`) so one event cannot crash workers forever.
- **Repo map.** Every path must resolve (`..` and symlinks) to a directory inside `--repo-root`, or the worker refuses to start (exit `2`). Paths are checked again before each check. Repo names match case-insensitively.

## Webhook responses

| Case | Status |
|---|---|
| `CLAIMCHECK_WEBHOOK_SECRET` not set | `503`: every request is refused |
| `X-Hub-Signature-256` missing, wrong, or not `sha256=` | `401`, nothing stored |
| Signed, but not `issues` or `pull_request` (e.g. `ping`, `push`) | `202` `{"status": "ignored"}`, nothing stored |
| Signed, but no `X-GitHub-Delivery`, or not a valid payload | `400` |
| Same `X-GitHub-Delivery` again | `202` with the same id and `"duplicate": true`; no second row |

## Security notes

- The signature is an HMAC-SHA256 of the raw body, checked with `hmac.compare_digest` before the body is parsed. An empty secret never verifies, because anyone can compute an HMAC with an empty key.
- Request bodies are limited to 1 MiB (`413`). The limit is checked against `Content-Length` and against the bytes actually read, so chunked uploads are capped too.
- `repo` is resolved (`..` and symlinks) and must be a directory inside `--repo-root`; anything else is `400`. A request with a `repro` field is rejected (`422`), not silently ignored.
- Webhooks are only recorded by the server. The worker reads the recorded text only as data for the regex extractor: it never runs a repro or a command from it. Nothing is fetched from GitHub.
- The investigations API and the dashboard need the bearer token when `CLAIMCHECK_API_TOKEN` is set, and are open (loopback only) when it is not; see [Authentication](#authentication). The API reads files under `--repo-root` (static analysis only), and stored investigations include the report and event text, which is untrusted.
- The token is plain bearer auth: serve it over TLS (a reverse proxy) when it leaves the machine, or the token can be read off the wire.
- The secret and the token are read from the environment, not flags, so they do not appear in the process list.

## Limits

- The worker checks a checkout that is already on disk, at whatever revision it is: it does not clone, fetch or check out the event's commit, and a PR is checked without its diff. Keeping the checkouts current is up to you.
- `--stale-after` must be longer than the slowest check, or a row still in progress is claimed and checked a second time (only one result is kept). Stale detection uses each host's clock, so workers on several hosts sharing a network file need synced clocks; SQLite over a network filesystem is not recommended anyway.
- Rows are polled, not pushed: a new event waits up to `--poll` seconds.
- No GitHub App authentication (installation tokens, JWT) yet, and no posting of results back to GitHub.
- Checks run inside the request, so a very large repo makes `POST /v1/investigations` slow.

## Dashboard

The server also renders a small read-only HTML dashboard from the same SQLite file (no JavaScript, no build step, nothing loaded from another host):

| Path | Shows |
|---|---|
| `GET /` or `GET /dashboard` | Investigations newest first: id, source, status, verdict, created time (UTC). `?status=received` (or `processing`, `completed`, `skipped`, `failed`) filters; 50 per page, `?page=2` for older ones. A bad `page` is a `400` page. |
| `GET /dashboard/investigations/{id}` | One investigation: source, status, created time, delivery id, worker and error; the verdict, extraction model and revision; each claim's status, kind, basis, evidence detail and source; and the stored request (report and diff, or the webhook event, its text and claims). An unknown id is a `404` page. |

Open `http://127.0.0.1:8080/` in a browser. The JSON API is unchanged. With `CLAIMCHECK_API_TOKEN` set, a browser does not send a bearer token by itself: use a client that adds the `Authorization` header (`curl -H`, a header-injecting extension, or an authenticating proxy that adds it).

- **Escaping.** Report text, webhook titles and bodies, and every value derived from them are untrusted. Every value is escaped with `html.escape` before it is written to the page, so `<script>alert(1)</script>` or `"><img onerror=...>` shows up as text. Status values only become CSS classes if they are one of the known statuses.
- **Headers.** Every dashboard response, including the `400`, `401` and `404` pages, sends `Content-Security-Policy: default-src 'none'; style-src 'sha256-...'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'` (no script runs at all, and only the page's one inline style block is allowed, by its hash), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and `Cache-Control: no-store`.
- **Read-only.** Only `GET` routes and no forms; anything else is `405`.
- **Authentication** is the same as the API's: the bearer token when `CLAIMCHECK_API_TOKEN` is set, otherwise open on a loopback host only. It shows report and event text to anyone who can reach it.
