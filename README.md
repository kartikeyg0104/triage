# claimcheck: triage that tests the claims

Team **Latecomers**, Hacktoberfest Hack Day Sonipat, Problem 2.

Maintainers get reports like "RCE in `parse_url_v2()`" or "crash on long URLs", and PRs that say "fixes #120, adds tests". `claimcheck` checks whether those claims are true and shows the evidence instead of trusting the text.

| Part | What it does | Docs |
|---|---|---|
| CLI | Extracts claims from a report, checks symbols and PR tests, reproduces the failure, prints evidence | below |
| Fix verification | Runs the repro on the base and the PR head revision: fails before, passes after? | [docs/VERIFY_FIX.md](docs/VERIFY_FIX.md) |
| Kubernetes executor | Runs repros as locked-down Jobs, refuses if the cluster does not really block egress | [docs/KUBERNETES_SECURITY.md](docs/KUBERNETES_SECURITY.md) |
| GitHub Action | Posts the evidence as a comment on every PR | below |
| Service API | FastAPI service with signed GitHub webhook ingestion and SQLite storage | [docs/SERVER.md](docs/SERVER.md) |
| Evaluation | Ground-truth cases with measured accuracy, run in CI | [docs/EVALUATION.md](docs/EVALUATION.md) |

**Measured:** 17 ground-truth cases, 3 runs each: 28/28 claims and 17/17 verdicts correct, **0 wrong confirmed, 0 wrong refuted**, identical across runs (regex extraction, revision `0ba1bec`). The cases are small and written by the team; see [docs/EVALUATION.md](docs/EVALUATION.md) for how they are scored.

## Install

```bash
python -m pip install -e ".[dev]"
ollama pull gemma3:1b      # optional: open-weight model used to read the report
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

Add `--json` for JSON output, or `--no-model` to skip Gemma and use the regex extractor. Real outputs of this command are in `examples/output_gemma.md`, `examples/output_no_model.md` and `examples/output_kubernetes.md`.

| Exit code | Meaning |
|---|---|
| 0 | supported: every claim was confirmed |
| 1 | refuted: at least one claim is false |
| 2 | unverified: nothing was refuted, but not everything could be confirmed |

### Verify a fix

```bash
python -m claimcheck check --repo PATH --report pr.txt \
  --repro "python3 poc.py" --expect "buffer overflow" --base main --head my-fix-branch
```

The repro runs in temporary git worktrees of both revisions (your checkout is never touched). *Confirmed* means it fails with the expected text on base and exits 0 on head; *refuted* means the bug is on base and head still fails the same way. Both commit SHAs are in the evidence.

## What it checks

- **Symbols:** does a function or class named in the report exist? Python is parsed with `ast`; JS/TS is matched by pattern. Evidence is `file:line`.
- **PR tests:** does the diff change source files without touching any test file?
- **Repro:** runs the given command in a throwaway copy of the repo with the network denied and a clean environment, either as a local process (`sandbox-exec` on macOS, `unshare -rn` on Linux) or as a Kubernetes Job. If isolation cannot be guaranteed, the command is **not run** and the claim stays unverified.

## How to read the evidence

Each row says whether it was **observed** (we saw it: a definition at `file:line`, a test file in the diff, the expected failure text in the output) or **inferred** (we concluded it from absence or a heuristic). The report also names the git revision that was tested.

- A function found nowhere in the source is *refuted* (inferred). If the name appears in code but has no static definition, or the repo creates names dynamically, it is *unverified*, because it may exist at runtime.
- A PR that changes no test file is *unverified*, not refuted: existing tests may already cover it. Coverage is not measured.
- A repro is *confirmed* only if the command fails **and** prints the expected text. A failure with no expected text, or a different message, is *unverified*. Exit 0 is *refuted*. Setup failures (command not found, missing module or file, exit 126/127, image pull errors) are *unverified* and never count as a reproduction.

## Run repros on Kubernetes

```bash
deploy/kind/setup.sh            # local kind cluster with Calico (enforces NetworkPolicy)
kubectl apply -k deploy/k8s     # namespace with Pod Security "restricted", least-privilege ServiceAccount
examples/run_kubernetes.sh      # the example above, repro in a pod
```

The Job runs as non-root with a read-only root filesystem, no Linux capabilities, seccomp `RuntimeDefault`, no service account token, no inherited environment, CPU/memory limits and a deadline. The repo travels in a ConfigMap (max ~900 KB), never fetched. A deny-all NetworkPolicy is created for the pod, and everything is deleted afterwards.

A NetworkPolicy does nothing unless the cluster's CNI enforces it, and kind's default CNI does not: our integration test caught a pod reaching the API server and the internet. So before the first run claimcheck starts a probe pod under the same policy, and if the probe gets out it **refuses** to run the repro (`egress open`). Reports say `egress blocked, verified` when the probe was blocked.

Options: `--k8s-namespace` (default `claimcheck`), `--k8s-image` (default `python:3.12-slim`), `--kube-context`, `--k8s-runtime-class` (for example `gvisor`; passed through but **not tested**).

## GitHub Action

```yaml
on:
  pull_request:
    types: [opened, edited, synchronize, reopened]
permissions: { contents: read, pull-requests: write, issues: write }
jobs:
  claims:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: kartikeyg0104/triage@main
```

It reads the PR title and body, checks the symbols and whether the diff touches tests, and posts one comment (updated in place on every push). The PR text is untrusted: it never reaches a shell, and a repro command found in it is never run. Set the `repro` and `expect` inputs in the workflow to run a repro of your own in the sandbox. Inputs: `path`, `repro`, `expect`, `comment`, `fail-on-refuted` (default `false`).

## Service API

```bash
python -m pip install -e ".[server]"
CLAIMCHECK_WEBHOOK_SECRET=... python -m claimcheck.server --db claimcheck.db --repo-root ./repos
```

`POST /v1/investigations` runs the static checks on a repo inside `--repo-root`; `GET /v1/investigations/{id}` returns it. `POST /v1/github/webhook` accepts `issues` and `pull_request` events only with a valid `X-Hub-Signature-256`, refuses everything when no secret is set, and records each delivery once. Event text is stored as data and never executed. Binds to 127.0.0.1; the investigations API has no authentication yet.

## Models

`gemma3:1b` through Ollama reads the report and lists the names and repro command in it. Names the model returns that are not in the report are dropped, and names written as `name()` or backticked code-like names are always added, so a small model cannot hide a claim. If Ollama is not running, the regex extractor is used and the report says so. The Action, the service and the evaluation use the regex extractor. There are no cloud AI calls.

## Tests

```bash
python -m pytest -q unit_tests                                              # unit + local integration
CLAIMCHECK_K8S_CONTEXT=kind-claimcheck python -m pytest -q unit_tests/integration   # real cluster
python -m claimcheck.evaluate --runs 3                                      # ground-truth evaluation
```

CI runs the unit tests and local integration tests on every PR, builds a kind cluster with Calico and runs the Kubernetes integration tests against real pods, and runs claimcheck on its own PRs.

## Limits

- The local executor is process-level isolation, not a container or gVisor. The Kubernetes executor is a hardened pod with verified egress blocking; gVisor has not been tested.
- A 1B model misses some claims on its own; the regex pass covers `name()` and backticked code-like names.
- JS/TS symbol detection is pattern-based, so unusual definitions can be missed.
- In a repo that creates names at runtime anywhere (`setattr(`, `exec(`, `globals()[...] =`, JS `exports[...] =`), a missing name is never refuted, only left unverified. That is deliberately conservative.
- The service's webhook records events but no worker processes them yet, and there is no GitHub App installation flow.
- The evaluation cases are small and written by the team; Gemma extraction is not evaluated.

License: MIT.
