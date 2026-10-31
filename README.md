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

## Models

`gemma3:1b` through Ollama reads the report and lists the names and repro command in it. Names the model returns that are not in the report are dropped, and names written as `` `name()` `` are always added, so a small model cannot hide a claim. If Ollama is not running, the regex extractor is used and the report says so. There are no cloud AI calls.

## Limits

- The sandbox is process-level isolation, not a container or gVisor. It stops network and secret access, not every kind of misbehaviour. Do not use it on hostile code you would not otherwise run.
- A 1B model misses some claims on its own. The regex pass covers `name()` and backticked names only.
- JS/TS symbol detection is pattern-based, so unusual definitions can be missed.
- It is a CLI. There is no GitHub App yet.

License: MIT.
