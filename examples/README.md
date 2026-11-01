# Examples

Each script runs the real CLI from the repo root. The saved `output_*.md` files are what those runs print. `unit_tests/test_examples.py` and `unit_tests/test_examples_more.py` rerun them and compare.

| Script | What it shows | Saved output |
|---|---|---|
| `run.sh` | A Python repo (`sample_repo/`), a security report (`report.txt`), a PR diff (`pr.diff`) and a repro run in the local sandbox. One claimed function does not exist, so the verdict is refuted. | `output_no_model.md` with `--no-model`, `output_gemma.md` with the local model |
| `run_kubernetes.sh` | The same check, but the repro runs as a locked-down Kubernetes Job. Needs a cluster that enforces NetworkPolicy: `deploy/kind/setup.sh` creates one. | `output_kubernetes.md` |
| `run_js.sh` | A JavaScript repo (`js_repo/`) and a report (`js_report.txt`) that claims `slugify()`, which exists, and `deduplicateSlug()`, which does not. | `output_js.md` |
| `fix_verification.sh` | Builds a throwaway git repo with a buggy commit and a fixed commit, then checks the fix with `--base`/`--head`: the repro fails on the buggy commit and passes on the fixed one. See `docs/VERIFY_FIX.md`. | `output_fix_verification.md` |

```bash
examples/run.sh --no-model
examples/run_kubernetes.sh
examples/run_js.sh
examples/fix_verification.sh
```

`run_js.sh` and `fix_verification.sh` use `python` unless `PYTHON` says otherwise, e.g. `PYTHON=.venv/bin/python examples/run_js.sh`. `fix_verification.sh` sets a fixed identity and fixed dates for its commits, so the commit SHAs are the same on every run. The repros (`run.sh`, `fix_verification.sh`) need sandbox isolation: `sandbox-exec` on macOS or `unshare` on Linux.
