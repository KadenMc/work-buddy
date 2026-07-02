#!/usr/bin/env bash
# Build the macOS release tarball: source-tree payload + vendored uv +
# install.command (double-clickable; opens Terminal and shows progress).
#
# A .pkg was considered and rejected: its postinstall would have to run the
# multi-minute, network-heavy uv dependency download inside Installer.app, which
# offers no progress UI and can time out. A Terminal-driven install.command gives
# the user visible progress and resumable retries. See AFK-DECISIONS.md.
#
# Runs in CI (or on a Mac). Usage: packaging/macos/build_tarball.sh [VERSION]
set -euo pipefail

VERSION="${1:-0.0.0}"
UV_VERSION="${UV_VERSION:-0.11.26}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STAGE="$REPO/dist/macos/work-buddy"

rm -rf "$STAGE"
mkdir -p "$STAGE"

python "$REPO/packaging/build_payload.py" --out "$STAGE/payload" --root "$REPO"
python "$REPO/packaging/vendor_uv.py" --target macos --out "$STAGE/payload/vendor" --version "$UV_VERSION"
cp "$HERE/install.command" "$STAGE/install.command"
chmod +x "$STAGE/install.command"

OUT="$REPO/dist/work-buddy-${VERSION}-macos-arm64.tar.gz"
tar -C "$REPO/dist/macos" -czf "$OUT" work-buddy
echo "built $OUT"
