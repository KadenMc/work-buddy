#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=packaging/acceptance/common.sh
source "$HERE/common.sh"

[ $# -eq 2 ] || die "usage: macos.sh <artifact.tar.gz> <version>"
ARTIFACT="$(resolve_path "$1")"
VERSION="$2"

require_hosted_runner
unset_external_credentials

SANDBOX="$(require_under_runner_temp "$RUNNER_TEMP/work-buddy acceptance macos")"
EXTRACT="$SANDBOX/extracted"
export HOME="$SANDBOX/home with space"
APP_HOME="$HOME/Work Buddy Home"
DATA_DIR="$HOME/Library/Application Support/work-buddy"
APPLICATIONS_DIR="$HOME/Applications"
EVIDENCE="$RUNNER_TEMP/work-buddy-macos-evidence"
mkdir -p "$EXTRACT" "$HOME" "$DATA_DIR" "$APPLICATIONS_DIR" "$EVIDENCE"

collect_service_logs() {
  cp -R "$DATA_DIR/logs" "$EVIDENCE/service-logs" 2>/dev/null || true
  cp -R "$HOME/Library/Logs/work-buddy" "$EVIDENCE/launchd-logs" 2>/dev/null || true
}
trap collect_service_logs EXIT

for target in "$SANDBOX" "$HOME" "$APP_HOME" "$DATA_DIR" "$APPLICATIONS_DIR"; do
  require_under_runner_temp "$target" >/dev/null
done
record_runner_facts "$EVIDENCE"
assert_ports_free

python3 "$HERE/verify_archive.py" \
  --archive "$ARTIFACT" --platform macos --version "$VERSION" \
  --evidence "$EVIDENCE/archive.json"
tar -xzf "$ARTIFACT" -C "$EXTRACT"
PACKAGE="$EXTRACT/work-buddy"
file "$PACKAGE/payload/vendor/uv" | tee "$EVIDENCE/uv-file.txt"
grep -qi 'arm64' "$EVIDENCE/uv-file.txt"
plutil -lint "$PACKAGE/app/Work Buddy.app/Contents/Info.plist" | tee "$EVIDENCE/plutil.txt"

ACCOUNT_HOME="$(python3 -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
if [ "$HOME" = "$ACCOUNT_HOME" ] && launchctl print "gui/$(id -u)" >/dev/null 2>&1; then
  AUTOSTART_MODE=require
else
  AUTOSTART_MODE=skip
fi
printf '%s\n' "$AUTOSTART_MODE" > "$EVIDENCE/autostart-mode.txt"

bash "$PACKAGE/install.command" \
  --home "$APP_HOME" --data-dir "$DATA_DIR" --applications-dir "$APPLICATIONS_DIR" \
  --autostart "$AUTOSTART_MODE" 2>&1 | tee "$EVIDENCE/install.log"

VENV_PY="$APP_HOME/.venv/bin/python"
WBUDDY="$APP_HOME/.venv/bin/wbuddy"
APP_BUNDLE="$APPLICATIONS_DIR/Work Buddy.app"
test -x "$VENV_PY"
test -x "$WBUDDY"
test -f "$APP_HOME/config.yaml"
test -f "$APP_HOME/config.local.yaml"
test -f "$APP_HOME/.mcp.json"
test -x "$APP_HOME/uninstall.command"
test -x "$APP_BUNDLE/Contents/MacOS/work-buddy-launcher"
test "$(cat "$APP_BUNDLE/Contents/Resources/app-home")" = "$APP_HOME"
plutil -lint "$APP_BUNDLE/Contents/Info.plist"

wait_for_url "http://127.0.0.1:5127/app/" 300
curl --silent --show-error --fail --dump-header "$EVIDENCE/app-headers.txt" \
  "http://127.0.0.1:5127/app/" --output "$EVIDENCE/app.html"
grep -F '<div id="root"></div>' "$EVIDENCE/app.html"
"$VENV_PY" -m work_buddy.cli status | tee "$EVIDENCE/status-after-install.txt"

BROWSER_LOG="$EVIDENCE/browser-open.log"
BROWSER_SINK="$SANDBOX/browser-sink"
cat > "$BROWSER_SINK" <<'SH'
#!/usr/bin/env sh
printf '%s\n' "$1" >> "$WB_BROWSER_LOG"
SH
chmod +x "$BROWSER_SINK"
export WB_BROWSER_LOG="$BROWSER_LOG"
export BROWSER="$BROWSER_SINK"

"$VENV_PY" -m work_buddy.cli stop
assert_ports_free
open -n -W "$APP_BUNDLE" 2>&1 | tee "$EVIDENCE/app-launch.log"
wait_for_url "http://127.0.0.1:5127/app/" 300
grep -F 'OK | Opened http://127.0.0.1:5127/app/' "$DATA_DIR/logs/desktop_launcher.log"

printf '\n# acceptance-config-sentinel\n' >> "$APP_HOME/config.local.yaml"
mkdir -p "$DATA_DIR/acceptance"
printf 'preserve-me\n' > "$DATA_DIR/acceptance/data-sentinel.txt"
"$VENV_PY" -m work_buddy.cli stop
bash "$PACKAGE/install.command" \
  --home "$APP_HOME" --data-dir "$DATA_DIR" --applications-dir "$APPLICATIONS_DIR" \
  --autostart "$AUTOSTART_MODE" 2>&1 | tee "$EVIDENCE/repair.log"
grep -F '# acceptance-config-sentinel' "$APP_HOME/config.local.yaml"
grep -F 'preserve-me' "$DATA_DIR/acceptance/data-sentinel.txt"
wait_for_url "http://127.0.0.1:5127/app/" 300

bash "$APP_HOME/uninstall.command" \
  --home "$APP_HOME" --data-dir "$DATA_DIR" --applications-dir "$APPLICATIONS_DIR" \
  2>&1 | tee "$EVIDENCE/uninstall-preserve-data.log"
test ! -e "$APP_HOME"
test ! -e "$APP_BUNDLE"
test -f "$DATA_DIR/acceptance/data-sentinel.txt"

bash "$PACKAGE/uninstall.command" \
  --home "$APP_HOME" --data-dir "$DATA_DIR" --applications-dir "$APPLICATIONS_DIR" --remove-data \
  2>&1 | tee "$EVIDENCE/uninstall-remove-data.log"
test ! -e "$DATA_DIR"
assert_ports_free

echo "macOS full acceptance passed."
