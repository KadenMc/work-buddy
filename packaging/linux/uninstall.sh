#!/usr/bin/env bash
# Remove a per-user work-buddy Linux installation.
set -euo pipefail

APP_HOME="$HOME/work-buddy"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/work-buddy"
REMOVE_DATA=0

while [ $# -gt 0 ]; do
  case "$1" in
    --home)        APP_HOME="$2"; shift 2 ;;
    --data-dir)    DATA_DIR="$2"; shift 2 ;;
    --remove-data) REMOVE_DATA=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$APP_HOME" in
  ""|"/"|"$HOME") echo "refusing unsafe application home: $APP_HOME" >&2; exit 2 ;;
esac
case "$DATA_DIR" in
  ""|"/"|"$HOME") echo "refusing unsafe data directory: $DATA_DIR" >&2; exit 2 ;;
esac

VENV_PY="$APP_HOME/.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  echo "==> Removing work-buddy services and user integration"
  (
    cd "$APP_HOME"
    export WORK_BUDDY_CONFIG_DIR="$APP_HOME"
    "$VENV_PY" -m work_buddy.cli uninstall
  ) || echo "warning: integration teardown reported a failure; continuing file cleanup" >&2
fi

DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/work-buddy.desktop"
rm -f "$DESKTOP_FILE"

echo "==> Removing application files from $APP_HOME"
rm -rf -- "$APP_HOME"

if [ "$REMOVE_DATA" -eq 1 ]; then
  echo "==> Removing user data from $DATA_DIR"
  rm -rf -- "$DATA_DIR"
else
  echo "==> Preserved user data at $DATA_DIR"
fi

echo "==> work-buddy uninstall complete."
