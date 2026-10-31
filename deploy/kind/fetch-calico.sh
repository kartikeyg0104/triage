#!/bin/sh
# Download a manifest and write it to OUT only if its SHA-256 matches the pinned value.
# Usage: deploy/kind/fetch-calico.sh URL SHA256 OUT
# URL may be any curl URL, including file:// (used by the tests).
set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: $0 URL SHA256 OUT" >&2
    exit 2
fi
URL="$1"
WANT="$2"
OUT="$3"

case "$WANT" in
    *[!0-9a-f]* | "")
        echo "fetch-calico: pinned SHA-256 must be 64 lowercase hex characters, got '$WANT'" >&2
        exit 2
        ;;
esac
if [ "${#WANT}" -ne 64 ]; then
    echo "fetch-calico: pinned SHA-256 must be 64 lowercase hex characters, got '$WANT'" >&2
    exit 2
fi

if command -v sha256sum >/dev/null 2>&1; then
    sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
    echo "fetch-calico: need sha256sum or shasum to verify the download" >&2
    exit 2
fi

TMP="$(mktemp "${TMPDIR:-/tmp}/calico.XXXXXX")"
trap 'rm -f "$TMP"' EXIT INT TERM

if ! curl -fsSL --retry 3 --retry-delay 2 -o "$TMP" "$URL"; then
    echo "fetch-calico: download failed: $URL" >&2
    exit 1
fi

GOT="$(sha256 "$TMP")"
if [ "$GOT" != "$WANT" ]; then
    echo "fetch-calico: SHA-256 mismatch for $URL" >&2
    echo "  expected: $WANT" >&2
    echo "  actual:   $GOT" >&2
    echo "Refusing to use it. If the upstream change is legitimate, update deploy/kind/calico.sha256." >&2
    exit 1
fi

mv "$TMP" "$OUT"
trap - EXIT INT TERM
echo "fetch-calico: verified $URL ($GOT)"
