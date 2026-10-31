#!/bin/sh
# A JavaScript repo: one claimed function exists (slugify), one does not (deduplicateSlug).
# Regenerates output_js.md with the real CLI. Usage: examples/run_js.sh
cd "$(dirname "$0")/.." || exit 1
"${PYTHON:-python}" -m claimcheck check --repo examples/js_repo --report examples/js_report.txt --no-model "$@"
