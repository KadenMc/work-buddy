---
name: Summarization Failure Handling
kind: concept
description: Classified retry, queue fairness, dormancy, dead-letter, and revival semantics for automatic conversation summaries.
tags:
- summarization
- retries
- dead-letter
- dormancy
- queue
aliases:
- summary retries
- summarization dead letters
parents:
- summarization
---

Conversation summaries are active unless the `conversation_summaries`
preference is explicitly unwanted. An explicit legacy
`conversation_observability.summaries.use_incremental` value is honored only
while that preference is undecided.

The activation policy (`summarization/policy.py`) is keyed to the
`conversation_summaries` component but gates the WHOLE worker, across every
queue namespace. That is correct only while conversation sessions are the
queue's sole background tenant: when a second background source starts using
the queue, activation must move per-namespace (one component per summarizing
source, matching the Settings per-source model), or opting out of session
summaries would silently switch the other source off too.

Before dequeueing, the worker performs a no-network plausibility check over the
configured model chain. With no resolvable local profile and no Anthropic key,
it returns `dormant: true, dormancy_reason: no_backend`; the queue is untouched.
The check fails open for backend kinds it does not recognize — its job is
catching missing credentials for known backends, and an unfamiliar backend that
is genuinely broken fails at call time instead. `bypass_inactive=true` on
`summarization_worker_tick` forces a run past both the opt-out and dormancy
gates for explicit one-off requests.

Failures use the shared LLM `ErrorKind` taxonomy. `backend_unavailable`,
`model_not_available`, `timeout`, `rate_limited`, and `auth` are environmental:
they rotate behind waiting work but do not consume attempts. All other kinds
are item-intrinsic by default and consume the attempt budget. At `max_attempts`
(default 3), the row becomes a dead letter: excluded from dequeue, retained in
queue snapshots and health/UI status.

Every failure updates `enqueued_at`, preventing one poison item from pinning
the FIFO head. Re-enqueueing after source change or `summarization_backfill`
resets attempts and error fields, reviving a dead letter after its cause is
fixed. `None` from incremental refresh means clean no-content only; typed
failures are recorded in the summary store and raised to the worker.
