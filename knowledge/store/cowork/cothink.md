---
name: Co-think
kind: system
description: Explicit, non-evidential support for human deliberation within the Co-work document experience.
summary: Co-think is the planned non-evidential sibling of Co-work Verify. The current editor-bottom dock is a non-operational shell; durable alternative-perspective records and Review actions from the earlier slice remain readable for compatibility while the fuller deliberation workflow is designed.
entry_points:
- work_buddy.cowork.verify_orchestration
- work_buddy.cowork.verify.service
- work_buddy.cowork.chat_targets
- dashboard-react/src/apps/cowork/rail/VerificationAttentionFeed.tsx
tags:
- cowork
- co-think
- alternative-perspective
- deliberation
- reflective-prompt
- non-evidential
aliases:
- Invite another perspective
- alternative perspective
- reflective support
- Socratic questioning
parents:
- cowork/verify-and-cothink
dev_notes: |-
  The historical backend admits only subtype `alternative_perspective`, although
  the durable record carries a subtype for extension. The current document UI
  intentionally exposes no new worker invocation. Keep `CothinkItem` and
  `CothinkItemStatusEvent` distinct from `EvaluationResult`,
  `RoutingDisposition`, and `ProposalRecord`.
---

## Current surface contract

Co-think has a collapsed sibling dock beside Verify beneath the editor. In the
current slice it is labeled **Planned** and does not expose a worker action,
settings, or a substitute transcript. This deliberately avoids locking the
subsystem to the earlier one-shot **Invite perspective** experiment.

Future operation belongs in the dock; durable outputs belong in Review; and
question-led continuation belongs in the existing document Chat. Co-think
does not inherit Verify’s criteria, results, goal, or proposal path. It does
not infer a need for reflection from cursor movement, scrolling, dwell time,
edit repetition, or other passive behavior.

## Historical alternative-perspective contract

The retained backend worker receives:

- the entire frozen document;
- exact target and context/change/egress boundaries;
- the user’s purpose;
- protected intent; and
- a schema requiring either `perspective` or `none`.

This contract is not exposed as a new invocation in the current dock. A
historical perspective must contain nonempty content and a rationale. `none` is an
honest outcome when the worker found no useful alternative. Unavailability and
no-useful-item outcomes remain visible without fabricating a card.

## Item semantics

A delivered item is labeled:

> Co-think · Alternative perspective

It is advisory and non-evidential. It has no severity, pass/fail result,
nonconformity claim, or confirm/reject decision.

The initial status is `open`. Human actions append immutable status events:

- `open → parked` for **Keep for later**;
- `open → dismissed`; and
- `parked → dismissed`.

`parked` means kept for later: it remains available to discuss or dismiss and
is not terminal. `dismissed` is terminal. Repeating the current status is
idempotent.

## Discuss

**Discuss** posts an exact user turn into the registered document conversation.
That turn carries the immutable Co-think item ID/hash and its original action
snapshot. The generation-fenced document agent must consume that snapshot and
return a receipt-bound response like any other targeted Chat turn.

Discussing does not change the item’s lifecycle status. A person may discuss
and still keep or dismiss the perspective independently.

If the frozen context cannot be opened, the agent receives an unavailable
consumption receipt and may only answer truthfully that the exact context was
unavailable before acknowledging the turn. If a restart happens after the
stable reply commits but before acknowledgement, the next consumer generation
may reuse that reply only after both receipts prove identical item/turn/target
semantics.

## Relationship to Verify

Verify may identify a reason that more deliberation would help, but a
coordinator cannot silently run Co-think. A future transition remains a
suggestion until the user explicitly invokes it.

Co-think output cannot:

- create or alter an evaluation result;
- activate or admit a criterion/check;
- create a correction proposal;
- make a human review decision; or
- apply document content.

Multi-turn Socratic questioning, reflective-prompt timing, cognitive forcing
functions, named return conditions for parked items, and typed
Verify-to-Co-think invitations remain extension points. When introduced, each
must keep its actual interaction name and authority boundary rather than being
collapsed into a generic “provocation.”
