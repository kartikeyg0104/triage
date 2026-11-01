# claimcheck: triage that tests the claims

Team **Latecomers**, Hacktoberfest Hack Day Sonipat, Problem 2 ([HACKATHON.md](HACKATHON.md)).

Maintainers get reports like "RCE in `parse_url_v2()`" or "crash on long URLs", and PRs that say "fixes #120, adds tests". `claimcheck` checks whether those claims are true and shows the evidence instead of trusting the text.

| Part | What it does | Docs |
|---|---|---|
| CLI | Extracts claims from a report, checks symbols and PR tests, reproduces the failure in a sandbox, prints evidence as Markdown, JSON or SARIF | [Use](#use) |
| Fix verification | Runs the repro on a base and a head revision: fails before, passes after? | [docs/VERIFY_FIX.md](docs/VERIFY_FIX.md) |
| Kubernetes executor | Runs repros as locked-down Jobs and refuses if egress is not really blocked; least-privilege RBAC, Pod Security `restricted`, ResourceQuota/LimitRange, optional impersonation | [docs/KUBERNETES_SECURITY.md](docs/KUBERNETES_SECURITY.md) |
| GitHub Action | PR/issue comment, step outputs, JSON report, a Check Run with `file:line` annotations | [GitHub Action](#github-action) |
| Service | FastAPI investigations API, signed GitHub webhook, a queue worker and a read-only HTML dashboard, on one SQLite file | [docs/SERVER.md](docs/SERVER.md) |
| Container image | `Dockerfile` for the CLI and the service, non-root, works with a read-only root filesystem | [docs/SERVER.md](docs/SERVER.md#container) |
| Issue forms | Structured bug and security-claim forms, plus a parser for them | [Issue forms](#issue-forms) |
| Evaluation | Ground-truth cases with measured accuracy, for regex and for Gemma extraction | [docs/EVALUATION.md](docs/EVALUATION.md), [docs/EVALUATION_GEMMA.md](docs/EVALUATION_GEMMA.md) |

**Measured** (local sandbox, 3 runs per case, numbers from the committed reports):

| Extraction | Cases | Claims correct | Verdicts correct | Wrong confirmed / refuted | Revision |
|---|---|---|---|---|---|
| Regex | 19 | 41/41 | 19/19 | 0 / 0 | `2b300c6` |
| `gemma3:1b` (regex names merged in) | 17 | 28/28 | 17/17 | 0 / 0 | `f192446` |

Results were identical across runs in both. The Gemma report measured an earlier, smaller case set (cases 18 and 19 did not exist yet). The cases are small and written by the team; see the reports for how they are scored.

## Install

```bash
python -m pip install -e ".[dev]"   # CLI plus test and service dependencies; ".[server]" for the service only
ollama pull gemma3:1b               # optional: open-weight model that reads the report
python -m pytest -q unit_tests
```

## Use

```bash
python -m claimcheck check \
  --repo examples/sample_repo \
  --report examples/report.txt \
  --diff examples/pr.diff \
  --repro "python3 poc.py" --expect "buffer overflow in parse_url"
```

| Flag | Does |
|---|---|
| `--repo`, `--report` | Repository to check and the report text (required) |
| `--diff` | Unified diff of the PR the report is about |
| `--repro`, `--expect` | Command that should reproduce the failure, and text it should print |
| `--base`, `--head` | Verify a fix: the repro must fail on `--base` and exit 0 on `--head` |
| `--json` | JSON instead of Markdown on stdout |
| `--sarif PATH` | Also write SARIF 2.1.0 (for GitHub code scanning); stdout is unchanged |
| `--no-model` | Skip Gemma and use the regex extractor |
| `--executor {local,kubernetes}` | Where the repro runs (default `local`) |
| `--k8s-namespace`, `--k8s-image`, `--kube-context`, `--k8s-runtime-class`, `--k8s-impersonate USER` | Kubernetes executor options, see below |

Real outputs of this command: [examples/output_gemma.md](examples/output_gemma.md), [examples/output_no_model.md](examples/output_no_model.md), [examples/output_kubernetes.md](examples/output_kubernetes.md) (regenerate with `examples/run.sh`).

| Exit code | Meaning |
|---|---|
| 0 | supported: every claim was confirmed |
| 1 | refuted: at least one claim is false |
| 2 | unverified: nothing was refuted, but not everything could be confirmed |

SARIF has one result per refuted (`error`) or unverified (`warning`) row; confirmed rows are not findings. A location is attached only for a repo-relative `path:line` (no absolute paths, no `..`).

### Verify a fix

```bash
python -m claimcheck check --repo PATH --report pr.txt \
  --repro "python3 poc.py" --expect "buffer overflow" --base main --head my-fix-branch
```

The repro runs in temporary git worktrees of both revisions; your checkout is never touched. *Confirmed* means it fails with the expected text on base and exits 0 on head; *refuted* means the bug is on base and head still fails the same way. Both commit SHAs are in the evidence.

## What it checks

- **Symbols:** does a function or class named in the report exist? Python is parsed with `ast`; JS/TS is matched by pattern. Evidence is `file:line`.
- **PR tests:** does the diff change source files without touching any test file?
- **Repro:** runs the command in a throwaway copy of the repo with the network denied and a clean environment (only `PATH`, `HOME`, `LANG`), with a 30 s timeout, either as a local process (`sandbox-exec` on macOS, `unshare -rn` on Linux) or as a Kubernetes Job. If isolation cannot be set up, the command is **not run** and the claim stays unverified. The copy never follows symlinks out of the repo, skips FIFOs, sockets and devices, and is capped (50 MiB per file, 200 MiB and 20,000 entries in total); anything left out is named in the evidence.

## How to read the evidence

Each row says whether it was **observed** (a definition at `file:line`, a test file in the diff, the expected failure text in the output) or **inferred** (concluded from absence or a heuristic). The report also names the git revision that was tested.

- A function found nowhere in the source is *refuted* (inferred). If the name appears in code but has no static definition, or the repo creates names dynamically, it is *unverified*, because it may exist at runtime.
- A PR that changes no test file is *unverified*, not refuted: existing tests may already cover it. Coverage is not measured.
- A repro is *confirmed* only if the command fails **and** prints the expected text. A failure without the expected text is *unverified*; exit 0 is *refuted*. Setup failures (command not found, missing module or file, exit 126/127, image pull errors) are *unverified* and never count as a reproduction.

## Run repros on Kubernetes

```bash
deploy/kind/setup.sh            # local kind cluster with Calico (enforces NetworkPolicy)
kubectl apply -k deploy/k8s     # namespace, ServiceAccount, Role, quota, Pod Security "restricted"
examples/run_kubernetes.sh      # the example above, repro in a pod
```

The Job runs as non-root with a read-only root filesystem, no capabilities, seccomp `RuntimeDefault`, no service account token, no inherited environment, CPU/memory limits (500m / 256Mi) and a deadline. The repo travels in a ConfigMap (archive at most 900,000 bytes), never fetched. A deny-all NetworkPolicy is created for the pod, and everything is deleted afterwards.

A NetworkPolicy does nothing unless the CNI enforces it, and kind's default CNI does not: our integration test caught a pod reaching the API server and the internet. So before the first repro claimcheck starts a probe pod under the same policy, and if the probe gets out it **refuses** to run the repro (`egress open`). Reports say `egress blocked, verified` when the probe was blocked.

`deploy/k8s` adds a namespaced Role with only the verbs the executor uses (no Secrets, no exec, nothing cluster-wide), Pod Security `restricted` enforcement, and a ResourceQuota/LimitRange sized for 10 concurrent runs. `--k8s-impersonate system:serviceaccount:claimcheck:claimcheck-executor` runs every kubectl call as that ServiceAccount.

Options: `--k8s-namespace` (default `claimcheck`), `--k8s-image` (default `python:3.12-slim`), `--kube-context`, `--k8s-impersonate`, `--k8s-runtime-class` (for example `gvisor`; passed through but **not tested**).

## GitHub Action

```yaml
on:
  pull_request:
    types: [opened, edited, synchronize, reopened]
permissions: { contents: read, pull-requests: write, issues: write, checks: write }
jobs:
  claims:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: kartikeyg0104/triage@main
        id: claimcheck
```

It reads the PR (or issue) title and body, checks the symbols and whether the diff touches tests, and:

- posts one comment, updated in place on every push, and writes the report to the job summary;
- creates a `claimcheck` Check Run on the PR head commit with annotations at each `file:line` (conclusion `success` when supported, `failure` only for refuted with `fail-on-refuted`, otherwise `neutral`); without `checks: write` it logs a warning and continues;
- sets outputs `verdict`, `confirmed`, `refuted`, `unverified` and `report-json` (path of the JSON report under `$RUNNER_TEMP`).

Inputs: `path`, `repro`, `expect`, `comment` (default `true`), `check-run` (default `true`), `fail-on-refuted` (default `false`), `github-token`. The PR text is untrusted: it never reaches a shell, and a repro command found in it is never run; set `repro` and `expect` in the workflow to run one of your own in the sandbox. The Action always uses the regex extractor. [.github/workflows/claimcheck.yml](.github/workflows/claimcheck.yml) runs it on this repo's own PRs.

## Service, worker and dashboard

```bash
python -m pip install -e ".[server]"
CLAIMCHECK_WEBHOOK_SECRET=... python -m claimcheck.server --db claimcheck.db --repo-root ./repos
python -m claimcheck.server.worker --db claimcheck.db --repo-root ./repos --repos repos.json
```

- `POST /v1/investigations` runs the static checks on a repo inside `--repo-root`; `GET /v1/investigations/{id}` returns it. No repro is ever run by the service.
- `POST /v1/github/webhook` records `issues` and `pull_request` events only with a valid `X-Hub-Signature-256`, refuses everything (`503`) when no secret is set, and records each delivery once. Bodies are capped at 1 MiB.
- The worker claims recorded events from the same SQLite file (safe with several workers), runs the static checks on the title and body against a checkout mapped in `--repos`, and stores the result. It does not clone or fetch, and checks a PR without its diff.
- `GET /` is a read-only HTML dashboard of the investigations: every value escaped, no JavaScript, a strict Content-Security-Policy.

Server options: `--db`, `--repo-root`, `--host` (default `127.0.0.1`), `--port` (default `8080`). Worker options: `--once`, `--poll`, `--stale-after`, `--worker-id`. Details in [docs/SERVER.md](docs/SERVER.md).

### Container

```bash
docker build -t claimcheck .
docker run --rm --read-only -v "$PWD/examples:/examples:ro" claimcheck \
  check --no-model --repo /examples/sample_repo --report /examples/report.txt --diff /examples/pr.diff
```

The image runs as UID `10001` on a digest-pinned `python:3.12-slim`, with `ENTRYPOINT ["claimcheck"]`; the service and worker run with `--entrypoint python ... -m claimcheck.server`. No model is bundled, and the local repro sandbox does not work under Docker's default seccomp profile, so use `--executor kubernetes` for repros. See [docs/SERVER.md](docs/SERVER.md#container).

## Issue forms

Bug reports and public security claims use the forms in [.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/) (affected function or class, repro command, expected error text, version or commit, description); blank issues are off, and anything security-sensitive goes to [private vulnerability reporting](https://github.com/kartikeyg0104/triage/security/advisories/new). `claimcheck/issue_form.py` reads a submitted form back out of the issue body as untrusted text: stripped, length-capped strings, nothing run. The Action, CLI and worker do not use the parser yet.

## Models

`gemma3:1b` through Ollama reads the report (at most its first 16 KiB, fenced by a random marker) and lists the names and repro command in it. Names the model returns that are not in the report are dropped, and names written as `name()` or backticked code-like names are always added, so a small model cannot hide a claim. If Ollama is not running, the regex extractor is used and the report says so. The Action, the service, the worker and the default evaluation use the regex extractor. There are no cloud AI calls.

## Tests and evaluation

```bash
python -m pytest -q -rs unit_tests                                                  # unit + local integration
CLAIMCHECK_K8S_CONTEXT=kind-claimcheck python -m pytest -q unit_tests/integration   # real cluster
python -m claimcheck.evaluate --runs 3                                              # regex evaluation
python -m claimcheck.evaluate --model gemma3:1b --runs 3                            # Gemma evaluation (needs Ollama)
```

`claimcheck.evaluate` also takes `--cases` and `--out`. CI runs the unit and local integration tests on every PR, builds a kind cluster with Calico and runs the Kubernetes integration tests against real pods, builds the container image and tests it read-only, and runs claimcheck on its own PRs.

## Security and limits

- **The local sandbox is process-level isolation** (`sandbox-exec` or `unshare -rn`), not a container, VM or gVisor. It blocks the network and clears the environment but does not block file reads, and it sets **no memory or CPU limit** (only the timeout), on macOS or Linux. Use the Kubernetes executor for untrusted repros.
- **gVisor is untested.** `--k8s-runtime-class` is passed to the pod, but nothing checks the RuntimeClass exists or is really gVisor. The Kubernetes setup is tested only on kind with Calico.
- **The investigations API and dashboard have no authentication.** They bind to `127.0.0.1` by default; do not expose them without an authenticating proxy. Stored investigations include untrusted report and event text.
- No GitHub App authentication (installation tokens, JWT) yet, and the service does not post results back to GitHub.
- The evaluation cases are small and written by the team, so 100% on them is not a claim about real-world accuracy.
- A 1B model misses some claims on its own; the regex pass covers `name()` and backticked code-like names. JS/TS symbol detection is pattern-based, so unusual definitions can be missed.
- In a repo that creates names at runtime anywhere (`setattr(`, `exec(`, `globals()[...] =`, JS `exports[...] =`), a missing name is never refuted, only left unverified. That is deliberately conservative.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Setup, tests and PR rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

License: MIT ([LICENSE](LICENSE)).
