# Examples

Each script runs the real CLI from the repo root. The saved `output_*.md` files are what the individual examples print. `unit_tests/test_examples.py` and `unit_tests/test_examples_more.py` rerun them and compare; `unit_tests/test_demo.py` checks the presentation flow.

| Script | What it shows | Saved output |
|---|---|---|
| `demo.sh` | The full presentation: local sandbox, Kubernetes when `kind-claimcheck` is reachable, fix verification in a throwaway git repo, SARIF when supported, and a fresh evaluation summary. | Printed live |
| `run.sh` | A Python repo (`sample_repo/`), a security report (`report.txt`), a PR diff (`pr.diff`) and a repro run in the local sandbox. One claimed function does not exist, so the verdict is refuted. | `output_no_model.md` with `--no-model`, `output_gemma.md` with the local model |
| `run_kubernetes.sh` | The same check, but the repro runs as a locked-down Kubernetes Job. Needs a cluster that enforces NetworkPolicy: `deploy/kind/setup.sh` creates one. | `output_kubernetes.md` |
| `run_js.sh` | A JavaScript repo (`js_repo/`) and a report (`js_report.txt`) that claims `slugify()`, which exists, and `deduplicateSlug()`, which does not. | `output_js.md` |
| `fix_verification.sh` | Builds a throwaway git repo with a buggy commit and a fixed commit, then checks the fix with `--base`/`--head`: the repro fails on the buggy commit and passes on the fixed one. See `docs/VERIFY_FIX.md`. | `output_fix_verification.md` |

```bash
examples/demo.sh
examples/demo.sh --no-cluster
examples/run.sh --no-model
examples/run_kubernetes.sh
examples/run_js.sh
examples/fix_verification.sh
```

`demo.sh` uses `python` unless `PYTHON` selects another interpreter, for example `PYTHON=.venv/bin/python examples/demo.sh --no-cluster`. It uses regex extraction, so Ollama is not needed. Run it from any directory; a relative `PYTHON` path is resolved from the calling directory.

The demo prints a header for every section. Kubernetes is skipped with a message when `kubectl` is missing or `kind-claimcheck` does not answer a five-second readiness probe. `--no-cluster` skips even that probe. A reachable cluster must already have the claimcheck namespace and security manifests installed as described in the top-level README.

Fix verification imports two deterministic revisions into a temporary repo with an empty contact field. It never commits in your checkout, and removes the temporary repo and output files on exit. Without local sandbox isolation, repro and fix evidence stays unverified; the evaluation skips repro cases. SARIF uses the existing `check --sarif` renderer, prints its JSON, and is skipped if the flag is unavailable. The evaluation runs once per case and prints fresh counts rather than using saved measurements.

Expected supported, refuted and unverified CLI verdicts (exit codes 0, 1 and 2) let the presentation continue. Evaluation findings are also shown without failing the demo. Unexpected tool failures, including a CLI argument error without a verdict, exit non-zero.

`run_js.sh` and `fix_verification.sh` use `python` unless `PYTHON` says otherwise, e.g. `PYTHON=.venv/bin/python examples/run_js.sh`. `fix_verification.sh` sets a fixed identity and fixed dates for its commits, so the commit SHAs are the same on every run. The repros (`run.sh`, `fix_verification.sh`) need sandbox isolation: `sandbox-exec` on macOS or `unshare` on Linux.
