#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=packaging/acceptance/common.sh
source "$HERE/common.sh"

[ $# -eq 2 ] || die "usage: linux.sh <artifact.tar.gz> <version>"
ARTIFACT="$(resolve_path "$1")"
VERSION="$2"

require_hosted_runner
unset_external_credentials

SANDBOX="$(require_under_runner_temp "$RUNNER_TEMP/work-buddy acceptance linux")"
EXTRACT="$SANDBOX/extracted"
export HOME="$SANDBOX/home with space"
export XDG_DATA_HOME="$SANDBOX/xdg data"
export XDG_CONFIG_HOME="$SANDBOX/xdg config"
export XDG_CACHE_HOME="$SANDBOX/xdg cache"
APP_HOME="$HOME/Work Buddy Home"
DATA_DIR="$XDG_DATA_HOME/work-buddy"
EVIDENCE="$RUNNER_TEMP/work-buddy-linux-evidence"
mkdir -p "$EXTRACT" "$HOME" "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$EVIDENCE"

collect_service_logs() {
  cp -R "$DATA_DIR/logs" "$EVIDENCE/service-logs" 2>/dev/null || true
  cp -R "$DATA_DIR/runtime/service_logs" "$EVIDENCE/runtime-service-logs" 2>/dev/null || true
}
trap collect_service_logs EXIT

for target in "$SANDBOX" "$HOME" "$APP_HOME" "$DATA_DIR"; do
  require_under_runner_temp "$target" >/dev/null
done
record_runner_facts "$EVIDENCE"
assert_ports_free

python3 "$HERE/verify_archive.py" \
  --archive "$ARTIFACT" --platform linux --version "$VERSION" \
  --evidence "$EVIDENCE/archive.json"
tar -xzf "$ARTIFACT" -C "$EXTRACT"
PACKAGE="$EXTRACT/work-buddy"
file "$PACKAGE/payload/vendor/uv" | tee "$EVIDENCE/uv-file.txt"
grep -Eqi 'x86[-_ ]64|x86-64' "$EVIDENCE/uv-file.txt"

ACCOUNT_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
if [ "$HOME" = "$ACCOUNT_HOME" ] && systemctl --user show-environment >/dev/null 2>&1; then
  AUTOSTART_MODE=require
else
  AUTOSTART_MODE=skip
fi
printf '%s\n' "$AUTOSTART_MODE" > "$EVIDENCE/autostart-mode.txt"

bash "$PACKAGE/install.sh" \
  --home "$APP_HOME" --data-dir "$DATA_DIR" --autostart "$AUTOSTART_MODE" \
  2>&1 | tee "$EVIDENCE/install.log"

VENV_PY="$APP_HOME/.venv/bin/python"
WBUDDY="$APP_HOME/.venv/bin/wbuddy"
test -x "$VENV_PY"
test -x "$WBUDDY"
test -f "$APP_HOME/config.yaml"
test -f "$APP_HOME/config.local.yaml"
test -f "$APP_HOME/.mcp.json"
test -f "$XDG_DATA_HOME/applications/work-buddy.desktop"
test -x "$APP_HOME/uninstall.sh"
DESKTOP_FILE="$XDG_DATA_HOME/applications/work-buddy.desktop"
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$DESKTOP_FILE" | tee "$EVIDENCE/desktop-file-validation.txt"
else
  echo "desktop-file-validate unavailable; applying required-field checks" \
    | tee "$EVIDENCE/desktop-file-validation.txt"
  grep -Fx '[Desktop Entry]' "$DESKTOP_FILE"
  grep -Fx 'Type=Application' "$DESKTOP_FILE"
  grep -Fx 'Name=Work Buddy' "$DESKTOP_FILE"
fi
grep -F 'Exec="' "$XDG_DATA_HOME/applications/work-buddy.desktop"

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
"$WBUDDY" launch 2>&1 | tee "$EVIDENCE/launcher-cold-start.log"
wait_for_url "http://127.0.0.1:5127/app/" 300
grep -F 'http://127.0.0.1:5127/app/' "$BROWSER_LOG"

printf '\n# acceptance-config-sentinel\n' >> "$APP_HOME/config.local.yaml"
mkdir -p "$DATA_DIR/acceptance"
printf 'preserve-me\n' > "$DATA_DIR/acceptance/data-sentinel.txt"
"$VENV_PY" -m work_buddy.cli stop
bash "$PACKAGE/install.sh" \
  --home "$APP_HOME" --data-dir "$DATA_DIR" --autostart "$AUTOSTART_MODE" \
  2>&1 | tee "$EVIDENCE/repair.log"
grep -F '# acceptance-config-sentinel' "$APP_HOME/config.local.yaml"
grep -F 'preserve-me' "$DATA_DIR/acceptance/data-sentinel.txt"
wait_for_url "http://127.0.0.1:5127/app/" 300

bash "$APP_HOME/uninstall.sh" --home "$APP_HOME" --data-dir "$DATA_DIR" \
  2>&1 | tee "$EVIDENCE/uninstall-preserve-data.log"
test ! -e "$APP_HOME"
test ! -e "$XDG_DATA_HOME/applications/work-buddy.desktop"
test -f "$DATA_DIR/acceptance/data-sentinel.txt"

bash "$PACKAGE/uninstall.sh" --home "$APP_HOME" --data-dir "$DATA_DIR" --remove-data \
  2>&1 | tee "$EVIDENCE/uninstall-remove-data.log"
test ! -e "$DATA_DIR"
assert_ports_free

echo "Linux full acceptance passed."
