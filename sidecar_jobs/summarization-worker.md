---
schedule: "3-59/5 * * * *"  # every 5 minutes, offset 3 minutes from conversation-observability-refresh
recurring: true
jitter_seconds: 60
type: capability
capability: summarization_worker_tick
params: {}
---
v2 summarization queue worker (PRD §6 O2). Drains the
`summarization_queue` SQLite table FIFO over the cooldown-passed
subset, bounded by `conversation_observability.summaries.daily_budget_usd`
(default $1.00).

Triggered by the existing `conversation-observability-refresh` job
(every 5 minutes, offset 2 minutes): that job's `refresh_observed_sessions`
detects JSONL mtime changes and ENQUEUES the changed session_ids; this
worker then drains the queue 3 minutes later.

Per-session cooldown (default 30 min, configurable
`conversation_observability.summaries.cooldown_minutes`) prevents an
actively-churning session from re-summarizing every 5 minutes —
the worker SKIPS in-cooldown sessions and tries the next eligible entry.

Runs by default. The worker itself honors the Session Summaries preference,
goes dormant without a plausible configured backend, and excludes dead letters
from drainage while keeping them visible for diagnosis and backfill revival.
