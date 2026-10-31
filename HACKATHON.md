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

**Models the product uses at runtime:** none decided yet. We will only list an open-weight model here, with the reason, in the PR that actually integrates it. We will not claim a model the code does not use.

## Workflow

- This file is the first commit, pushed straight to `main`. After it, `main` is PR-only.
- Every change is a pull request that follows `.github/pull_request_template.md` and passes CI.
- No API keys or secrets in the repo. Public data only.
- License: MIT.
