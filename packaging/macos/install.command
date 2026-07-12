#!/usr/bin/env bash
# work-buddy macOS installer (per-user, no sudo). Double-click to run in Terminal.
#
# Lays the source-tree payload into a HOME, runs the uv bootstrap (managed
# Python 3.11 + venv + editable install with CPU torch, retried) + provision,
# and registers a launchd login agent. Idempotent; re-run repairs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_HOME="$HOME/work-buddy"
DATA_DIR="$HOME/Library/Application Support/work-buddy"
VAULT_ROOT=""
ANTHROPIC_KEY=""
APPLICATIONS_DIR="$HOME/Applications"
AUTOSTART_MODE="auto"

while [ $# -gt 0 ]; do
  case "$1" in
    --home)         APP_HOME="$2"; shift 2 ;;
    --data-dir)     DATA_DIR="$2"; shift 2 ;;
    --vault-root)   VAULT_ROOT="$2"; shift 2 ;;
    --anthropic-key) ANTHROPIC_KEY="$2"; shift 2 ;;
    --applications-dir) APPLICATIONS_DIR="$2"; shift 2 ;;
    --autostart)    AUTOSTART_MODE="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$AUTOSTART_MODE" in
  auto|require|skip) ;;
  *) echo "--autostart must be auto, require, or skip" >&2; exit 2 ;;
esac

UV="$HERE/payload/vendor/uv"
VENV_PY="$APP_HOME/.venv/bin/python"
chmod +x "$UV"
# Keep uv's data dir + managed Python under the per-user DATA dir (off any
# cloud-synced location; consistent with the Windows OneDrive-448 fix). Setting
# UV_DATA_DIR (not just UV_PYTHON_INSTALL_DIR) is what actually relocates the
# version-link uv creates.
export UV_DATA_DIR="$DATA_DIR/uv"
export UV_PYTHON_INSTALL_DIR="$DATA_DIR/uv/python"

echo "==> Installing work-buddy into $APP_HOME"
mkdir -p "$APP_HOME" "$DATA_DIR"
cp -a "$HERE/payload/." "$APP_HOME/"
cp "$HERE/uninstall.command" "$APP_HOME/uninstall.command"
chmod +x "$APP_HOME/uninstall.command"
export WORK_BUDDY_CONFIG_DIR="$APP_HOME"

echo "==> work-buddy runs a private semantic-search engine on your machine, so this"
echo "    downloads its own Python and machine-learning libraries (about 1 GB, one"
echo "    time). Search models download later, on first use. Nothing is sent to a"
echo "    cloud service."

echo "==> Installing Python 3.11 (uv)"
"$UV" python install 3.11

echo "==> Creating the virtual environment"
"$UV" venv --clear --python 3.11 "$APP_HOME/.venv"

echo "==> Downloading dependencies (this can take several minutes)"
attempt=1
until "$UV" pip install --python "$VENV_PY" --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cpu -e "$APP_HOME"; do
  if [ "$attempt" -ge 3 ]; then
    echo "Dependency install failed after $attempt attempts. Re-run install.command to resume (downloads are cached)." >&2
    exit 1
  fi
  sleep "$((2 ** attempt))"; attempt="$((attempt + 1))"
done

echo "==> Provisioning work-buddy"
prov=(-m work_buddy.cli provision --home "$APP_HOME" --data-dir "$DATA_DIR")
[ -n "$VAULT_ROOT" ]    && prov+=(--vault-root "$VAULT_ROOT")
[ -n "$ANTHROPIC_KEY" ] && prov+=(--anthropic-key "$ANTHROPIC_KEY")
"$VENV_PY" "${prov[@]}"

if [ "$AUTOSTART_MODE" = "skip" ]; then
  echo "==> Skipping login auto-start registration"
else
  echo "==> Registering login auto-start (launchd)"
  if "$VENV_PY" -m work_buddy.cli autostart enable; then
    "$VENV_PY" -m work_buddy.cli autostart status
  elif [ "$AUTOSTART_MODE" = "require" ]; then
    echo "Auto-start registration is required but failed." >&2
    exit 1
  else
    echo "warning: auto-start registration failed; the installation remains usable with 'wbuddy start'" >&2
  fi
fi

echo "==> Installing Work Buddy.app into $APPLICATIONS_DIR"
mkdir -p "$APPLICATIONS_DIR"
rm -rf -- "$APPLICATIONS_DIR/Work Buddy.app"
cp -R "$HERE/app/Work Buddy.app" "$APPLICATIONS_DIR/Work Buddy.app"
printf '%s\n' "$APP_HOME" > "$APPLICATIONS_DIR/Work Buddy.app/Contents/Resources/app-home"
chmod +x "$APPLICATIONS_DIR/Work Buddy.app/Contents/MacOS/work-buddy-launcher"

echo "==> work-buddy install complete."
echo "    Open Work Buddy from $APPLICATIONS_DIR."
echo "    Open Claude Code in $APP_HOME and run /wb-setup guided."
