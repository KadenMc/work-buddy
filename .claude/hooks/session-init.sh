#!/bin/bash
# Capture Claude Code session ID on SessionStart.
# On CLI: persists via CLAUDE_ENV_FILE (automatic).
# On Desktop: outputs the ID so the agent can set it manually.

INPUT=$(cat)

# Try python (Windows), then python3 (Linux/Mac)
SESSION_ID=$(echo "$INPUT" | python -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
fi

if [ -z "$SESSION_ID" ]; then
    echo "WORK_BUDDY_SESSION_ID= (hook fired but could not extract session_id from stdin)"
    exit 0
fi

if [ -n "$CLAUDE_ENV_FILE" ]; then
    echo "export WORK_BUDDY_SESSION_ID=\"$SESSION_ID\"" >> "$CLAUDE_ENV_FILE"
    echo "WORK_BUDDY_SESSION_ID set automatically: $SESSION_ID"
else
    echo "WORK_BUDDY_SESSION_ID=$SESSION_ID"
fi

exit 0
