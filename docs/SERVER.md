# claimcheck service

A small HTTP service: groundwork for a GitHub App. It runs static checks on request and records signed GitHub webhook events. It never runs a repro or any command taken from request or event text.

```bash
python -m pip install -e ".[server]"
export CLAIMCHECK_WEBHOOK_SECRET="$(openssl rand -hex 32)"   # same value as the GitHub webhook secret
python -m claimcheck.server --db claimcheck.db --repo-root examples   # binds 127.0.0.1:8080
```

Options: `--host` (default `127.0.0.1`), `--port` (default `8080`).

## Endpoints

| Method and path | Does |
|---|---|
| `POST /v1/investigations` | Body `{"repo": "<dir inside --repo-root>", "report": "...", "diff": "..."}` (`diff` optional). Runs the symbol and PR-test checks with the regex extractor (no model, no repro) and returns `201` with `{"id", "status": "completed", "result": <same JSON as --json>, ...}`. |
| `GET /v1/investigations/{id}` | The stored investigation, or `404`. |
| `POST /v1/github/webhook` | A GitHub webhook (content type `application/json`). `issues` and `pull_request` events are recorded with status `received`, source `github:<event>:<owner/repo>#<number>` and the claim names found in the title and body. Returns `202`. |

```bash
curl -s localhost:8080/v1/investigations -H 'Content-Type: application/json' \
  -d '{"repo": "sample_repo", "report": "RCE in parse_url_v2()"}'
```

Everything is stored in one SQLite file (table `investigations`), so results survive a restart.

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
- Webhooks are only recorded. Nothing is fetched from GitHub and nothing is executed; the issue or PR text is stored as data.
- The investigations API has **no authentication**. Keep it on localhost, or put it behind a proxy that authenticates. It reads files under `--repo-root` (static analysis only), and stored investigations include the report and event text, which is untrusted.
- The secret is read from the environment, not a flag, so it does not appear in the process list.

## Limits

- Webhook events are recorded but not yet checked: there is no worker that clones the repo and runs the checks.
- No GitHub App authentication (installation tokens, JWT) yet, and no posting of results back to GitHub.
- Checks run inside the request, so a very large repo makes `POST /v1/investigations` slow.
