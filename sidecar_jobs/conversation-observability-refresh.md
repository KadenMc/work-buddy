---
schedule: "2-59/5 * * * *"  # every 5 minutes, offset 2 minutes from ir-index-rebuild
recurring: true
jitter_seconds: 60
type: capability
capability: conversation_observability_refresh
params:
  days: 7
  max_sessions: 5
  stale_only: true
---
Refresh the conversation_observability DB: observed-session metadata,
session-attributed commits, and session-attributed file writes (with
dirty-state snapshot). Runs on its own schedule (offset 2 minutes from
ir-index-rebuild so the IR endpoint isn't fighting it for SQLite write
locks), stale-only so unchanged JSONL files don't get re-parsed, and
capped at 5 sessions per pass so the refresh stays bounded.

The collector that feeds journal/context bundles
(`claude_session_summary`) also triggers a stale-only refresh inline,
so the cron is a backstop — it keeps the DB fresh between bundle
collections.
