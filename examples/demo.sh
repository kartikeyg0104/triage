#!/bin/sh
# Show the complete flow without requiring a model or a cluster.
set -eu

no_cluster=0
case "${1:-}" in
  --no-cluster) no_cluster=1; shift ;;
  --help) printf 'Usage: examples/demo.sh [--no-cluster]\n'; exit 0 ;;
esac
if [ "$#" -ne 0 ]; then
  printf 'Usage: examples/demo.sh [--no-cluster]\n' >&2
  exit 2
fi

python=${PYTHON:-python}
case "$python" in
  /*) ;;
  */*) python="$(cd "$(dirname "$python")" && pwd)/$(basename "$python")" ;;
esac
cd "$(dirname "$0")/.."
tmp=$(mktemp -d "${TMPDIR:-/tmp}/claimcheck-demo.XXXXXX")
trap 'rm -rf "$tmp"' 0
trap 'exit 130' INT
trap 'exit 143' TERM

section() {
  printf '\n=== %s ===\n\n' "$1"
}

check() {
  status=0
  "$python" -m claimcheck check "$@" > "$tmp/check.md" || status=$?
  # Exit 2 is also used for argument errors, which do not produce a verdict.
  case "$status:$(cat "$tmp/check.md")" in
    0:*'**Verdict: supported**'*|1:*'**Verdict: refuted**'*|2:*'**Verdict: unverified**'*)
      cat "$tmp/check.md" ;;
    *)
      cat "$tmp/check.md" >&2
      printf 'Unexpected CLI error (exit %s).\n' "$status" >&2
      if [ "$status" -eq 0 ]; then status=1; fi
      return "$status" ;;
  esac
}

sample() {
  check --repo examples/sample_repo --report examples/report.txt --diff examples/pr.diff \
    --repro "python3 poc.py" --expect "buffer overflow in parse_url" --no-model "$@"
}

section 'Local sandbox'
sample --executor local

section 'Kubernetes'
if [ "$no_cluster" -eq 1 ]; then
  printf 'Skipped: Kubernetes disabled (--no-cluster).\n'
elif ! command -v kubectl >/dev/null 2>&1; then
  printf 'Skipped: kind-claimcheck is not reachable (kubectl is not installed).\n'
elif kubectl --context kind-claimcheck --request-timeout=5s get --raw=/readyz >/dev/null 2>&1; then
  sample --executor kubernetes --kube-context kind-claimcheck
else
  printf 'Skipped: kind-claimcheck is not reachable.\n'
fi

section 'Fix verification'
mkdir "$tmp/repo"
g() (
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    git -C "$tmp/repo" -c core.hooksPath=/dev/null -c init.templateDir= "$@"
)
g init -q -b main
# Import deterministic revisions with an empty contact field, entirely inside the temp repo.
g fast-import --quiet <<'EOF'
commit refs/heads/main
mark :1
committer demo <> 1767225600 +0000
data <<MESSAGE
Add parse_url
MESSAGE
M 100644 inline urls.py
data <<FILE
def parse_url(url):
    if len(url) > 64:
        return None
    return url
FILE
M 100644 inline poc.py
data <<FILE
from urls import parse_url

if parse_url("http://example.invalid/" + "a" * 100) is None:
    raise RuntimeError("buffer overflow in parse_url")
FILE

commit refs/heads/main
committer demo <> 1767225600 +0000
data <<MESSAGE
Handle long URLs in parse_url
MESSAGE
from :1
M 100644 inline urls.py
data <<FILE
def parse_url(url):
    return url
FILE

done
EOF
g reset --hard -q
base=$(g rev-parse HEAD^)
head=$(g rev-parse HEAD)
printf 'Fix: long URLs no longer crash parse_url().\n' > "$tmp/report.txt"
check --repo "$tmp/repo" --report "$tmp/report.txt" --no-model --executor local \
  --repro "python3 poc.py" --expect "buffer overflow in parse_url" --base "$base" --head "$head"

section 'SARIF output'
"$python" -m claimcheck check --help > "$tmp/help.txt"
case "$(cat "$tmp/help.txt")" in
  *--sarif*)
    check --repo examples/sample_repo --report examples/report.txt --diff examples/pr.diff \
      --no-model --sarif "$tmp/result.sarif" >/dev/null
    cat "$tmp/result.sarif" ;;
  *) printf 'Skipped: SARIF output is not available in this CLI.\n' ;;
esac

section 'Evaluation summary'
status=0
"$python" -m claimcheck.evaluate --runs 1 --out "$tmp/evaluation.md" || status=$?
case "$status" in
  0) ;;
  1) printf 'Evaluation reported wrong definite verdicts; see the summary above.\n' ;;
  *) printf 'Unexpected evaluation error (exit %s).\n' "$status" >&2; exit "$status" ;;
esac
