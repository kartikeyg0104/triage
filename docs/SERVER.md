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

Everything is stored in one SQLite file, so results survive a restart. A file from an older version gets the worker columns (`error`, `worker`, `claimed_at`, `attempts`) and the companion `writebacks` table when it is opened; existing investigation rows are preserved.

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

### GitHub comments

Set `CLAIMCHECK_GITHUB_TOKEN` in the worker's environment to enable writeback. Use a GitHub token with permission to read and write comments on the mapped repositories (Issues write for issues, Pull requests write for PRs). This is separate from `CLAIMCHECK_API_TOKEN`, which protects the service API, and `CLAIMCHECK_WEBHOOK_SECRET`, which authenticates incoming events. An unset or empty GitHub token disables comments.

After a check completes, the worker posts the markdown evidence, including the verdict, claim statuses, basis, sources and tested revision, to the source issue or PR. It lists issue comments through GitHub's REST API and updates the comment containing `<!-- claimcheck-service -->`, or creates one if none exists. Later investigations of the same issue or PR update that comment. Skipped and failed investigations make no GitHub API calls. Redelivering the same webhook delivery does not run another check or post another comment.

The investigation returned by `GET /v1/investigations/{id}` includes `writeback` (`pending`, `created`, `updated`, `error`, or `null` when disabled/skipped/failed) and `writeback_error`. API failures leave the check's `status` and `result` intact, record a sanitized error in `writeback_error`, and let the worker continue. The worker's JSON log includes the writeback outcome when present. The token is sent only in the Authorization header; it is never logged or persisted, and HTTP error response bodies are not recorded.

`CLAIMCHECK_GITHUB_API` overrides `https://api.github.com`, for example with a GitHub Enterprise API URL or a local fake HTTP server. It must be an HTTP(S) URL with a host and valid port, without credentials, query, fragment or whitespace; a bad value stops startup with exit `2` when comments are enabled. Requests time out after 30 seconds and redirects are refused to avoid forwarding the token. Comment lookup searches up to 20 pages of 100 comments; a failed, malformed or exhausted lookup records an error instead of creating a possible duplicate.

Workers sharing the database reserve the issue/PR target from claiming through the comment attempt, including repo names with different casing. Other targets can still be processed. Set `--stale-after` longer than the full check and comment attempt on every worker. If a worker dies after storing a completed check, a later worker expires its pending writeback and records an abandonment error. Comment errors and previously completed rows are not automatically retried or backfilled; a new webhook investigation attempts writeback again and finds any existing marked comment.

From Python, pass `writeback=claimcheck.server.github_client.from_env(os.environ)` to `process_one(...)` or `run(...)` to enable the same behavior.

## Webhook responses

| Case | Status |
|---|---|
| `CLAIMCHECK_WEBHOOK_SECRET` not set | `503`: every request is refused |
| `X-Hub-Signature-256` missing, wrong, or not `sha256=` | `401`, nothing stored |
| Signed, but not `issues` or `pull_request` (e.g. `ping`, `push`) | `202` `{"status": "ignored"}`, nothing stored |
| Signed, but no `X-GitHub-Delivery`, or not a valid payload | `400` |
| Signed, valid new delivery, but the repository has reached either intake cap | `429` with `{"detail": ...}`; nothing stored |
| Same `X-GitHub-Delivery` again | `202` with the same id and `"duplicate": true`, even at an intake cap; no second row |

### Repository intake caps

Each repository can have at most **100 pending investigations** and accept at most **60 new deliveries in a rolling 60-second window**. A new delivery that would exceed either cap gets `429`. Set positive integer environment values before starting the server to change the caps:

```bash
export CLAIMCHECK_WEBHOOK_MAX_PENDING=100
export CLAIMCHECK_WEBHOOK_MAX_PER_MINUTE=60
python -m claimcheck.server --db claimcheck.db --repo-root examples
```

From Python, `create_app(..., webhook_max_pending=100, webhook_max_per_minute=60)` overrides the corresponding environment values. Invalid or nonpositive values raise `ValueError` at app creation.

- Pending means `received` or `processing`: a worker claim keeps the slot occupied until the investigation is `completed`, `skipped` or `failed`.
- The minute cap counts accepted `issues` and `pull_request` deliveries regardless of their current status. A delivery expires from that count exactly 60 seconds after acceptance. Duplicate IDs, rejected requests and ignored events consume no slots.
- Both caps use `repository.full_name` (`owner/repo`) from the payload after its signature is verified. Names match case-insensitively; issues and PRs share the same caps. Headers and client addresses do not select a repository's quota, and other repositories have independent quotas.
- Deduplication, counts and insertion share one SQLite write transaction. Existing rows count, and caps persist across restarts and apply across server processes sharing the database. Repository indexes are added when an existing database is opened; investigation rows are preserved.

## Security notes

- The signature is an HMAC-SHA256 of the raw body, checked with `hmac.compare_digest` before the body is parsed. An empty secret never verifies, because anyone can compute an HMAC with an empty key.
- Request bodies are limited to 1 MiB (`413`). The limit is checked against `Content-Length` and against the bytes actually read, so chunked uploads are capped too.
- `repo` is resolved (`..` and symlinks) and must be a directory inside `--repo-root`; anything else is `400`. A request with a `repro` field is rejected (`422`), not silently ignored.
- Webhooks are only recorded by the server. The worker reads the recorded text only as data for the regex extractor: it never runs a repro or a command from it. Repository code is never fetched from GitHub; with writeback enabled, only the comments API is used.
- The investigations API and the dashboard need the bearer token when `CLAIMCHECK_API_TOKEN` is set, and are open (loopback only) when it is not; see [Authentication](#authentication). The API reads files under `--repo-root` (static analysis only), and stored investigations include the report and event text, which is untrusted.
- The token is plain bearer auth: serve it over TLS (a reverse proxy) when it leaves the machine, or the token can be read off the wire.
- The secret and the token are read from the environment, not flags, so they do not appear in the process list.

## Limits

- Webhook intake caps apply only to servers sharing the same SQLite file. The rolling window uses the host's UTC clock; hosts sharing a file need synced clocks. The caps bound pending work and intake rate, but completed investigations are retained, so they do not cap total database size.
- The worker checks a checkout that is already on disk, at whatever revision it is: it does not clone, fetch or check out the event's commit, and a PR is checked without its diff. Keeping the checkouts current is up to you.
- `--stale-after` must be longer than the slowest check plus its comment attempt, or a live claim/target reservation can expire. Stale detection uses each host's clock, so workers on several hosts sharing a network file need synced clocks; SQLite over a network filesystem is not recommended anyway. Comment coordination applies only to workers sharing the same SQLite file.
- Rows are polled, not pushed: a new event waits up to `--poll` seconds.
- No GitHub App authentication (installation tokens, JWT) yet; writeback uses the token supplied in the environment. A crash between storing a completed result and posting its comment can leave the result without a comment until a new investigation. API failures have no automatic retry.
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

## Container

The `Dockerfile` builds the CLI and the service (the `[server]` extra) on `python:3.12-slim`, pinned by tag and digest. Wheels are built in a first stage; the final image has no compilers or source tree, runs as UID `10001`, and has `ENTRYPOINT ["claimcheck"]`. `git` is included so reports name the tested revision and `--base`/`--head` work on mounted checkouts. Everything that writes (`HOME`, `TMPDIR`, the `--db` file) goes under `/tmp`, so the root filesystem can be read-only.

```bash
docker build -t claimcheck .

# CLI: mount the repo read-only. Exit codes as without a container (1 = refuted).
docker run --rm --read-only -v "$PWD/examples:/examples:ro" claimcheck \
  check --no-model --repo /examples/sample_repo --report /examples/report.txt --diff /examples/pr.diff

# Service: override the entrypoint, give it a writable /tmp, bind 0.0.0.0 inside the container.
docker run --rm --read-only --tmpfs /tmp -p 127.0.0.1:8080:8080 \
  -e CLAIMCHECK_WEBHOOK_SECRET -v /srv/checkouts:/repos:ro \
  --entrypoint python claimcheck \
  -m claimcheck.server --db /tmp/claimcheck.db --repo-root /repos --host 0.0.0.0 --port 8080
```

The worker runs the same way with `-m claimcheck.server.worker`. A `--tmpfs /tmp` database is lost when the container stops: to keep it, bind-mount a host directory writable by UID `10001` (e.g. `-v /srv/claimcheck:/data`, `--db /data/claimcheck.db`) and give the worker the same mount. Publish the port on `127.0.0.1` only, or set `CLAIMCHECK_API_TOKEN` (see [Authentication](#authentication)).

CI (`image` job) builds the image and runs, all with `--read-only`: `--help`; the example check (expects exit `1` and `parse_url_v2` in the report); the service, expecting `404` from `GET /v1/investigations/does-not-exist`; and `unit_tests/integration/test_image.py` (set `CLAIMCHECK_TEST_IMAGE=<image>` to run it locally).

Limits: `--repro` uses the local sandbox (`unshare -rn`), which Docker's default seccomp profile blocks, so a repro run inside the container reports that no sandbox is available; use `--executor kubernetes` or run the CLI outside a container for repros. No model is bundled: without `--no-model` the CLI calls Ollama at `localhost:11434` (inside the container, so only with `--network host` on Linux) and otherwise falls back to the regex extractor. The digest pin does not update itself; bump it to pick up base image security fixes.
