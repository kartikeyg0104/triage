#!/bin/sh
# Verify a fix claim across two revisions (see docs/VERIFY_FIX.md).
# Builds a throwaway git repo with a buggy commit and a fixed commit, then checks that
# the repro fails on the buggy one and passes on the fixed one.
# Regenerates output_fix_verification.md with the real CLI. Usage: examples/fix_verification.sh
cd "$(dirname "$0")/.." || exit 1
tmp=$(mktemp -d) || exit 1
trap 'rm -rf "$tmp"' EXIT

# Fixed identity, dates and config so the commit SHAs are the same on every run.
g() {
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
  GIT_AUTHOR_NAME=example GIT_AUTHOR_EMAIL=example@example.invalid \
  GIT_COMMITTER_NAME=example GIT_COMMITTER_EMAIL=example@example.invalid \
  GIT_AUTHOR_DATE="2026-01-01T00:00:00Z" GIT_COMMITTER_DATE="2026-01-01T00:00:00Z" \
  git -C "$tmp/repo" "$@"
}

mkdir "$tmp/repo"
g init -q -b main
cat > "$tmp/repo/urls.py" <<'EOF'
def parse_url(url):
    if len(url) > 64:  # the bug: a fixed 64-character buffer
        return None
    return url
EOF
cat > "$tmp/repo/poc.py" <<'EOF'
from urls import parse_url

if parse_url("http://example.invalid/" + "a" * 100) is None:
    raise RuntimeError("buffer overflow in parse_url")
EOF
g add urls.py poc.py
g commit -q -m "Add parse_url"
buggy=$(g rev-parse HEAD)
cat > "$tmp/repo/urls.py" <<'EOF'
def parse_url(url):
    return url
EOF
g commit -q -a -m "Handle long URLs in parse_url"
fixed=$(g rev-parse HEAD)

cat > "$tmp/report.txt" <<'EOF'
Fix: long URLs no longer crash parse_url().

Before this change `python3 poc.py` failed with "buffer overflow in parse_url".
EOF

"${PYTHON:-python}" -m claimcheck check --repo "$tmp/repo" --report "$tmp/report.txt" \
  --repro "python3 poc.py" --expect "buffer overflow in parse_url" \
  --base "$buggy" --head "$fixed" --no-model "$@"
