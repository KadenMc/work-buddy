#!/bin/bash
# SessionStart hook: materialize the /tmp/wb/status helper command from its
# template, baking in this session's id and the repo path. This gives
# shell-level tooling (the Monitor tool, bash loops, cron) a sanctioned,
# read-only poll target for consent-request and operation status — the
# pieces that cannot speak MCP.
#
# Mirrors the messaging hook's /tmp/wb/* generation
# (work_buddy/messaging/check_messages.sh) but is deliberately separate so
# the status command does not depend on the messaging service's health.
# Registered as a SessionStart hook in config/global_settings.json.

INPUT=$(cat)

# Extract session_id from the hook's stdin JSON (python, then python3).
SESSION=$(echo "$INPUT" | python -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
if [ -z "$SESSION" ]; then
    SESSION=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
fi
if [ -z "$SESSION" ]; then
    SESSION="${WORK_BUDDY_SESSION_ID:-}"
fi

# Locate the template (next to this script) and the repo root (two levels up:
# work_buddy/statusctl/ -> work_buddy/ -> repo).
BIN_SRC="$(cd "$(dirname "$0")" && pwd)/bin"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

mkdir -p /tmp/wb
if [ -f "$BIN_SRC/status.sh" ]; then
    sed \
        -e "s|%%SESSION%%|$SESSION|g" \
        -e "s|%%REPO%%|$REPO|g" \
        "$BIN_SRC/status.sh" > /tmp/wb/status
    chmod +x /tmp/wb/status
fi

exit 0
