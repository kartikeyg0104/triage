#!/bin/sh
# Same example as run.sh, but the repro runs as a locked-down Kubernetes Job.
# Needs a cluster that enforces NetworkPolicy: deploy/kind/setup.sh creates one.
cd "$(dirname "$0")/.." || exit 1
python -m claimcheck check --repo examples/sample_repo --report examples/report.txt \
  --diff examples/pr.diff --repro "python3 poc.py" --expect "buffer overflow in parse_url" \
  --no-model --executor kubernetes --kube-context "${KUBE_CONTEXT:-kind-claimcheck}" "$@"
