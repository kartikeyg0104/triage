# HACKATHON.md

**Event:** Hacktoberfest 2026 · Hack Day Sonipat (NST · Rishihood University), 31 Oct – 1 Nov 2026

## Team

**Team name:** Latecomers

| Member | Role |
|---|---|
| Kartikey Gupta | Team lead |
| Awantika Maheshwari | Member |
| Abhijeet | Member |
| Mohammad Affan | Member |

## Problem statement

**Problem 2: Triage that tests the claims.**

A GitHub App that checks whether a bug report, security report or pull request claim is true, and shows maintainers the evidence. Claims are tested safely in an isolated sandbox with no secrets and no network. Examples of the evidence it produces: "reproduced on 2.4.1", "`parse_url_v2()` does not exist", "no test covers this change".

## Models

**Models the product uses at runtime:** `gemma3:1b` (open-weight, Google Gemma) through Ollama on the local machine. It reads a free-text report and lists the function names, the repro command and the expected error in it. We chose it because it is open-weight, small enough to run on a laptop with no API key, and fast enough for a CI-style check. Its output is validated: names not in the report are dropped, and a regex pass adds any `name()` it missed. If Ollama is not running, the tool falls back to regex and says so in the report. No cloud AI APIs are used.

## Workflow

- This file is the first commit, pushed straight to `main`. After it, `main` is PR-only.
- Every change is a pull request that follows `.github/pull_request_template.md` and passes CI.
- No API keys or secrets in the repo. Public data only.
- License: MIT.
