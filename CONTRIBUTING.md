# Contributing to claimcheck

Thanks for helping. claimcheck tells maintainers whether a claim is true, so a contribution is only useful if its own claims are true too: say what you changed, how you tested it, and which tools helped.

## Setup

Python 3.10 or newer (CI uses 3.12).

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
ollama pull gemma3:1b      # optional: only for model extraction; everything else works without it
```

## Running the tests

Unit tests and the local integration tests (the local sandbox, the git worktree checks, the service and worker):

```bash
python -m pytest -q -rs unit_tests
```

`-rs` lists skipped tests and why. The sandbox tests need process isolation (`sandbox-exec` on macOS, `unshare -rn` on Linux) and are skipped where it is not available; on Ubuntu you may need `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, as CI does. The Kubernetes tests in `unit_tests/integration` are skipped unless `CLAIMCHECK_K8S_CONTEXT` is set. A few checks of `action.yml` and `deploy/k8s` are skipped unless PyYAML is installed (`python -m pip install pyyaml`).

Cluster tests run real Jobs on a real cluster. You need `kind` and `kubectl`:

```bash
deploy/kind/setup.sh                                    # kind cluster "claimcheck" with Calico (enforces NetworkPolicy)
kubectl --context kind-claimcheck apply -k deploy/k8s   # restricted Pod Security, least-privilege ServiceAccount and Role
CLAIMCHECK_K8S_CONTEXT=kind-claimcheck python -m pytest -v -rs unit_tests/integration
```

Do not run the cluster tests against a cluster you care about; use a throwaway kind cluster. See [docs/KUBERNETES_SECURITY.md](docs/KUBERNETES_SECURITY.md).

## The evaluation

```bash
python -m claimcheck.evaluate --runs 3                                                    # regex extraction, prints the report
python -m claimcheck.evaluate --runs 3 --out docs/EVALUATION.md                           # regenerate the committed report
python -m claimcheck.evaluate --model gemma3:1b --runs 3 --out docs/EVALUATION_GEMMA.md   # needs Ollama running
```

The cases are in `eval/cases/<name>/`: a tiny `repo/`, a `report.txt`, an optional PR diff, and a `case.json` with the expected answers and a `truth` sentence saying why they are right. How cases are scored is in [docs/EVALUATION.md](docs/EVALUATION.md).

Ground-truth rules:

- **Expected answers come from what is true about the case's repo, never from what the tool outputs.** Write `truth` and `expected` from reading the code, before running the tool.
- **Never edit expectations to match the tool.** If the tool disagrees with a case, either the tool is wrong (fix the tool) or the case is wrong (fix the case and explain in the PR why the old truth was false).
- When the evidence cannot settle a claim, the truth is `unverified`. Do not pick a definite answer to make a case look decisive.
- `docs/EVALUATION.md` and `docs/EVALUATION_GEMMA.md` are generated from real runs. Rerun the command; do not edit them by hand, and do not commit numbers you did not measure.

## How we work

- `main` is PR-only. Every change is a pull request, and CI (`.github/workflows/ci.yml`: unit tests plus the Kubernetes integration tests on a kind cluster) must pass before it is merged. claimcheck also checks its own PRs (`.github/workflows/claimcheck.yml`).
- **Test first.** For a bug, write a test that fails on `main`, then fix it. For a feature, write the tests that describe it, watch them fail, then implement. Say in the PR what failed before your change.
- Keep PRs small and focused on one thing. Keep the docs in step with the code: if a command, option or limit changes, update the README and `docs/` in the same PR.
- Be honest about results. If something is untested, say so (as the README does for gVisor) rather than implying it works.

## The pull request template

Every PR fills in the four sections of [.github/pull_request_template.md](.github/pull_request_template.md):

1. **What this PR does**: the change, and why.
2. **Models the product uses in this PR**: the open-source or open-weight models the code calls at runtime, for example `gemma3:1b` via Ollama, or `none`.
3. **AI coding assistants that helped write it**: name every one truthfully (for example GitHub Copilot, Cursor, Claude Code), or `none`. Using one is fine; hiding it is not.
4. **How we tested it**: the commands you ran and what they showed, including test counts and anything skipped.

## Secrets

Never commit secrets: API keys, tokens, kubeconfigs, webhook secrets, `.env` files or SQLite databases with real event data. claimcheck needs no API keys; the webhook secret is read from the `CLAIMCHECK_WEBHOOK_SECRET` environment variable. Use public data only in tests, examples and cases. If you commit a secret by mistake, treat it as leaked: revoke it first, then remove it.

## Security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
