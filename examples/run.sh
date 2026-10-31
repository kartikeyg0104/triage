#!/bin/sh
# Regenerates the example outputs with the real CLI. Usage: examples/run.sh [--no-model]
cd "$(dirname "$0")/.." || exit 1
python -m claimcheck check --repo examples/sample_repo --report examples/report.txt \
  --diff examples/pr.diff --repro "python3 poc.py" --expect "buffer overflow in parse_url" "$@"
