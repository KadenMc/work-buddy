#!/bin/bash
# Global hook: check for pending messages from work-buddy's messaging service.
# Runs on SessionStart, UserPromptSubmit, PostToolUse, and Stop hooks.
#
# Modes (first argument):
#   instructions  — SessionStart: full instructions, all messages, create helper scripts
#   (none)        — UserPromptSubmit: all pending messages
#   urgent        — PostToolUse: only high-priority/agent-ingest messages, rate-limited
#   stop          — Stop hook: like urgent, but returns decision:block if events
#                   found (blocks the stop so the agent can review pending events)
#
# The "urgent" and "stop" modes implement AgentIngest mid-turn delivery:
# - Rate-limited to avoid hammering the messaging service (5s cooldown)
# - Only surfaces high-priority or agent-ingest-tagged messages
# - "stop" mode uses decision:block to block Claude from stopping while events are pending

SERVICE_URL="http://localhost:5123"
BIN_SRC="$(cd "$(dirname "$0")" && pwd)/bin"
RECIPIENT="$(basename "$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")")"
MODE="${1:-}"  # instructions, urgent, stop, or empty

# Read stdin JSON and extract session_id
STDIN_JSON=$(cat)
SESSION=$(echo "$STDIN_JSON" | python -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

# Fall back to env var if stdin parsing failed
if [ -z "$SESSION" ]; then
    SESSION="${WORK_BUDDY_SESSION_ID:-}"
fi

# ---------------------------------------------------------------------------
# Rate limiting for urgent/stop modes (5s cooldown)
# ---------------------------------------------------------------------------
RATE_LIMIT_FILE="/tmp/wb/last_ingest_check"

if [ "$MODE" = "urgent" ] || [ "$MODE" = "stop" ]; then
    if [ -f "$RATE_LIMIT_FILE" ]; then
        LAST_CHECK=$(cat "$RATE_LIMIT_FILE" 2>/dev/null || echo "0")
        NOW=$(date +%s)
        ELAPSED=$(( NOW - LAST_CHECK ))
        if [ "$ELAPSED" -lt 5 ]; then
            # Skip — checked recently, no output
            exit 0
        fi
    fi
    mkdir -p /tmp/wb
    date +%s > "$RATE_LIMIT_FILE"
fi

# ---------------------------------------------------------------------------
# SessionStart: create /tmp/wb/ helper scripts
# ---------------------------------------------------------------------------
if [ "$MODE" = "instructions" ]; then
    mkdir -p /tmp/wb
    for script in send.sh reply.sh read.sh; do
        if [ -f "$BIN_SRC/$script" ]; then
            sed \
                -e "s|%%SERVICE_URL%%|$SERVICE_URL|g" \
                -e "s|%%SENDER%%|$RECIPIENT|g" \
                -e "s|%%SESSION%%|$SESSION|g" \
                "$BIN_SRC/$script" > "/tmp/wb/${script%.sh}"
            chmod +x "/tmp/wb/${script%.sh}"
        fi
    done
fi

# ---------------------------------------------------------------------------
# Health check (1s timeout) — skip for urgent/stop to save latency
# ---------------------------------------------------------------------------
if [ "$MODE" != "urgent" ] && [ "$MODE" != "stop" ]; then
    if ! curl -s --max-time 1 "$SERVICE_URL/health" > /dev/null 2>&1; then
        cat <<'HOOK_JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "work-buddy messaging service is not running. You may have unread messages from work-buddy. Let the user know so they can start it if they'd like. If the user asks you to send a message to work-buddy, tell them the service must be running first before you can deliver it."
  }
}
HOOK_JSON
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Determine hook event name and query parameters
# ---------------------------------------------------------------------------
case "$MODE" in
    instructions)
        HOOK_EVENT="SessionStart"
        QUERY_EXTRA=""
        ;;
    urgent)
        HOOK_EVENT="PostToolUse"
        # Session filtering handles targeting — session-targeted messages
        # are consent/notification responses from AgentIngest dispatch.
        # TODO: Add priority/tags filtering to messaging service for
        # finer-grained control (e.g., &priority=high&tags=agent-ingest)
        QUERY_EXTRA=""
        ;;
    stop)
        HOOK_EVENT="Stop"
        QUERY_EXTRA=""
        ;;
    *)
        HOOK_EVENT="UserPromptSubmit"
        QUERY_EXTRA=""
        ;;
esac

# ---------------------------------------------------------------------------
# Query for pending messages
# ---------------------------------------------------------------------------
TIMEOUT=2
if [ "$MODE" = "urgent" ] || [ "$MODE" = "stop" ]; then
    TIMEOUT=1  # Shorter timeout for mid-turn checks
fi

HTTP_CODE=$(curl -s -o /tmp/wb_msg_response.json -w "%{http_code}" --max-time "$TIMEOUT" \
    "$SERVICE_URL/messages?recipient=$RECIPIENT&session=$SESSION&status=pending&format=context&hook_event=$HOOK_EVENT$QUERY_EXTRA" 2>/dev/null)

if [ "$HTTP_CODE" = "200" ]; then
    # For "stop" mode: wrap the response in a decision:block envelope
    # so Claude Code blocks the stop AND injects the additionalContext.
    # Exit code 0 + "decision":"block" is the correct pattern for Stop hooks
    # (exit code 2 is a raw block signal that doesn't inject context).
    if [ "$MODE" = "stop" ]; then
        CONTEXT=$(python -c "import sys,json; d=json.load(sys.stdin); print(d['hookSpecificOutput']['additionalContext'])" < /tmp/wb_msg_response.json 2>/dev/null)
        rm -f /tmp/wb_msg_response.json 2>/dev/null
        cat <<STOP_JSON
{
  "decision": "block",
  "reason": "Pending agent-ingest events require review before stopping",
  "hookSpecificOutput": {
    "hookEventName": "Stop",
    "additionalContext": $(python -c "import json,sys; print(json.dumps(sys.argv[1]))" "$CONTEXT" 2>/dev/null || echo "\"Pending ingest events found\"")
  }
}
STOP_JSON
        exit 0
    fi
    cat /tmp/wb_msg_response.json
elif [ "$HTTP_CODE" = "204" ] && [ "$MODE" = "instructions" ]; then
    # No messages on SessionStart — show command help only
    cat <<'HOOK_JSON'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "work-buddy messaging service is running. No pending messages.\nMessaging commands (run --help for details):\n  bash /tmp/wb/send --help\n  bash /tmp/wb/reply --help\n  bash /tmp/wb/read --help"
  }
}
HOOK_JSON
fi
# For urgent/stop with no events (204): output nothing, exit 0 (don't block)

rm -f /tmp/wb_msg_response.json 2>/dev/null
exit 0
