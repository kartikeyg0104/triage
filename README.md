# claimcheck: triage that tests the claims

Team **Latecomers**, Hacktoberfest Hack Day Sonipat, Problem 2.

Maintainers get reports like "RCE in `parse_url_v2()`" or "crash on long URLs", and PRs that say "adds tests". `claimcheck` checks whether those claims are true and shows the evidence instead of trusting the text.

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

Add `--json` for JSON output, or `--no-model` to skip Gemma and use the regex extractor. The real output of this command is in `examples/output_gemma.md` and `examples/output_no_model.md`.

| Exit code | Meaning |
|---|---|
| 0 | supported: every claim was confirmed |
| 1 | refuted: at least one claim is false |
| 2 | unverified: nothing was refuted, but not everything could be confirmed |

## What it checks

- **Symbols:** does a function or class named in the report exist? Python is parsed with `ast`; JS/TS is matched by pattern. Evidence is `file:line`.
- **PR tests:** does the diff change source files without touching any test file?
- **Repro:** runs the given command in a throwaway copy of the repo with the network denied and a clean environment. If the machine has no way to block the network (`sandbox-exec` on macOS, `unshare -rn` on Linux), the command is **not run** and the claim stays unverified.

## Run repros on Kubernetes

`--executor kubernetes` runs the repro as a one-shot Job instead of a local process:

```bash
deploy/kind/setup.sh            # local kind cluster with Calico (enforces NetworkPolicy)
examples/run_kubernetes.sh      # the example above, repro in a pod
```

The Job runs as non-root with a read-only root filesystem, no Linux capabilities, seccomp `RuntimeDefault`, no service account token, no inherited environment, CPU/memory limits and a deadline. The repo is shipped in a ConfigMap (max ~900 KB), never fetched. A deny-all NetworkPolicy is created for the pod, and the Job, ConfigMap and policy are deleted afterwards.

A NetworkPolicy does nothing unless the cluster's CNI enforces it, and kind's default CNI does not (we tested this: a pod reached the API server and the internet). So before the first run, claimcheck starts a probe pod under the same policy. If the probe can reach the network, it **refuses** to run the repro and the report says `egress open`. Reports say `egress blocked, verified` when the probe was blocked.

Options: `--k8s-namespace` (default `claimcheck`), `--k8s-image` (default `python:3.12-slim`), `--kube-context`, `--k8s-runtime-class` (for example `gvisor`). gVisor is **not** tested yet: claimcheck passes the RuntimeClass to the Job but does not check that the cluster has it.

## How to read the evidence

Each row says whether it was **observed** (we saw it: a definition at `file:line`, a test file in the diff, the expected failure text in the output) or **inferred** (we concluded it from absence or a heuristic). The report also names the git revision that was tested.

- A function found nowhere in the source is *refuted* (inferred). If the name appears but has no static definition, it is *unverified*, because it may be defined at runtime.
- A PR that changes no test file is *unverified*, not refuted: existing tests may already cover it. Coverage is not measured.
- A repro is *confirmed* only if the command fails **and** prints the expected text. A failure with no expected text, or a different message, is *unverified*. Exit 0 is *refuted*. Setup failures (command not found, missing module or file, exit 126/127) are *unverified* and never count as a reproduction.

## Models

`gemma3:1b` through Ollama reads the report and lists the names and repro command in it. Names the model returns that are not in the report are dropped, and names written as `name()` are always added, so a small model cannot hide a claim. If Ollama is not running, the regex extractor is used and the report says so. There are no cloud AI calls.

## Limits

- The local executor is process-level isolation, not a container or gVisor. The Kubernetes executor is a hardened pod with verified egress blocking, but gVisor has not been tested. It stops network and secret access, not every kind of misbehaviour. Do not use it on hostile code you would not otherwise run.
- A 1B model misses some claims on its own. The regex pass covers `name()` and backticked names that look like code (with an underscore, or CamelCase); a plain backticked word is treated as prose.
- JS/TS symbol detection is pattern-based, so unusual definitions can be missed.
- There is a CLI and a GitHub Action (`action.yml`, regex extraction, no model on the runner). There is no GitHub App.

License: MIT.

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

It reads the PR title and body, checks the symbols and whether the diff touches tests, and posts one comment (updated in place on every push). The PR text is treated as untrusted: it never reaches a shell, and a repro command found in it is never run. Set the `repro` and `expect` inputs in the workflow to run a repro of your own in the sandbox. Inputs: `path`, `repro`, `expect`, `comment`, `fail-on-refuted` (default `false`).
