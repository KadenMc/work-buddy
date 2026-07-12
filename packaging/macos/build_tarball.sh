#!/usr/bin/env bash
# Build the macOS release tarball: source-tree payload + vendored uv +
# install.command (double-clickable; opens Terminal and shows progress).
#
# A .pkg was considered and rejected: its postinstall would have to run the
# multi-minute, network-heavy uv dependency download inside Installer.app, which
# offers no progress UI and can time out. A Terminal-driven install.command keeps
# dependency progress visible and makes cached retries actionable.
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

npm --prefix "$REPO/dashboard-react" ci
npm --prefix "$REPO/dashboard-react" run build
python "$REPO/packaging/build_payload.py" --out "$STAGE/payload" --root "$REPO"
python "$REPO/packaging/vendor_uv.py" --target macos --out "$STAGE/payload/vendor" --version "$UV_VERSION"
cp "$HERE/install.command" "$STAGE/install.command"
cp "$HERE/uninstall.command" "$STAGE/uninstall.command"
chmod +x "$STAGE/install.command" "$STAGE/uninstall.command"

APP="$STAGE/app/Work Buddy.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/app/Info.plist" "$APP/Contents/Info.plist"
cp "$HERE/app/work-buddy-launcher" "$APP/Contents/MacOS/work-buddy-launcher"
chmod +x "$APP/Contents/MacOS/work-buddy-launcher"
python - "$APP/Contents/Info.plist" "$VERSION" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
path.write_text(path.read_text(encoding="utf-8").replace("__APP_VERSION__", sys.argv[2]), encoding="utf-8")
PY

ICON_SOURCE="$REPO/dashboard-react/dist/icons/app-1024.png"
ICONSET="$STAGE/app/work-buddy.iconset"
mkdir -p "$ICONSET"
while IFS=: read -r size name; do
  sips -z "$size" "$size" "$ICON_SOURCE" --out "$ICONSET/$name" >/dev/null
done <<'EOF'
16:icon_16x16.png
32:icon_16x16@2x.png
32:icon_32x32.png
64:icon_32x32@2x.png
128:icon_128x128.png
256:icon_128x128@2x.png
256:icon_256x256.png
512:icon_256x256@2x.png
512:icon_512x512.png
1024:icon_512x512@2x.png
EOF
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/work-buddy.icns"
rm -rf "$ICONSET"

OUT="$REPO/dist/work-buddy-${VERSION}-macos-arm64.tar.gz"
tar -C "$REPO/dist/macos" -czf "$OUT" work-buddy
echo "built $OUT"
