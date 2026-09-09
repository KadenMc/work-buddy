# Task staleness review proposal

Status: proposed follow-up. This document does not add a worker or dashboard button.

## User experience

A task's dedicated view offers **Check staleness**, with help explaining that it
investigates current relevance and completion without changing lifecycle state.
The button starts a durable background review using the model selected in
Dashboard AI settings. The task shows the run's status and the exact model used.
Navigation away does not cancel the investigation; repeated clicks open the same
active review instead of launching duplicates.

The result distinguishes:

- **Already complete:** the underlying intent is satisfied, possibly through a
  better or different implementation.
- **Superseded:** another task, decision, or implementation replaces its intent.
  Name the replacement and explain whether anything remains.
- **Details outdated:** the task remains useful, but specific assumptions,
  instructions, links, or remaining steps should change.
- **Still current:** the reviewed evidence supports its current intent and details.
- **Insufficient evidence:** the worker cannot justify any of these conclusions.
  Missing provenance must never silently become “still current.”

Every result includes evidence links, confidence, what was checked, and any
proposed next action. Append a dated staleness-check note to the task's knowledge
document; preserve the original task text. Repeated delivery of one result must
not append duplicate notes. A task without a knowledge document uses the existing
task document creation/linking flow before the append.

Actionable results create a persistent dashboard notification linked to the
review. Already-complete findings offer **Review and mark complete**. Superseded
findings explain the replacement before offering the same decision. Outdated
details offer **Review suggested changes**. Still-current findings remain visible
on the task without an interrupting notification. A review record outlives its
notification, including when delivery fails or a notification expires.

## Reuse and required additions

Build on `wb-task-completeness` and `wb-task-completeness-sweep`: their intent-first
rubric already distinguishes done, done differently, partial, consciously
descoped, and not done. These investigation outcomes are independent of the
workspace's lifecycle Status and Attention filters. The existing `task_stale_check`
capability only checks age, due dates, and lifecycle heuristics; it is not a
semantic investigation.

Reuse the completeness evidence collector, agent-execution model selection and
worker disclosure, durable Co-work dispatch patterns, append-only document writes,
and notification delivery tracking. A detached worker also needs scoped access to
task-specific repository, session, and decision evidence. A cached task bundle
alone cannot support a strong completion claim.

Add a durable review record keyed by run ID, task ID, initiating human, task
revision, knowledge-document head, and selected provider/model. Store launch,
progress, result, delivery, and decision independently so failed notification or
note delivery can retry without repeating the model investigation. Record the
document head and any task revision produced by its own document attachment to
avoid treating those owned writes as unrelated task changes.

Use a dedicated review decision route with existing task mutation receipts and
revision checks. The current action-proposal API supports task creation only;
completion cannot be passed through it without an intentional contract extension.
Any intervening task change requires review of the current task before applying
completion or suggested edits. Never automatically complete, archive, or rewrite
a task because a worker produced a verdict.

## Verification before shipping

Exercise each verdict with cited and missing evidence; duplicate clicks; an
unavailable configured model; navigation and restart during a review; invalid
worker output; task edits during the investigation; and note/notification delivery
failures. Verify that retrying cannot duplicate a worker, append, or completion,
and that a dismissed or expired notification leaves the review accessible.
Finally, test the actual dashboard journey from Check staleness through review and
an explicit completion decision using an isolated task store.
