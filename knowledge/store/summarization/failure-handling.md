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

Before dequeueing, the worker performs a no-network plausibility check over the
configured model chain. With no resolvable local profile and no Anthropic key,
it returns `dormant: true, dormancy_reason: no_backend`; the queue is untouched.

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
