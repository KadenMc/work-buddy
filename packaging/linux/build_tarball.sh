#!/usr/bin/env bash
# Build the Linux release tarball: source-tree payload + vendored uv + install.sh.
# Runs in CI (or on a Linux box). Usage: packaging/linux/build_tarball.sh [VERSION]
set -euo pipefail

VERSION="${1:-0.0.0}"
UV_VERSION="${UV_VERSION:-0.11.26}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STAGE="$REPO/dist/linux/work-buddy"

rm -rf "$STAGE"
mkdir -p "$STAGE"

python "$REPO/packaging/build_payload.py" --out "$STAGE/payload" --root "$REPO"
python "$REPO/packaging/vendor_uv.py" --target linux --out "$STAGE/payload/vendor" --version "$UV_VERSION"
cp "$HERE/install.sh" "$STAGE/install.sh"
chmod +x "$STAGE/install.sh"

OUT="$REPO/dist/work-buddy-${VERSION}-linux-x86_64.tar.gz"
tar -C "$REPO/dist/linux" -czf "$OUT" work-buddy
echo "built $OUT"
