---
name: Co-think
kind: system
description: Explicit, non-evidential support for human deliberation within the Co-work document experience.
summary: Co-think is the non-evidential sibling of Co-work Verify in the document action bar. It uses the same exact action target and explicit provider/model to request at most one alternative perspective, recording a distinct advisory item or an honest no-useful-item/unavailable outcome.
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
  Co-think currently admits only subtype `alternative_perspective`, although the
  durable record carries a subtype for extension. Keep `CothinkItem` and
  `CothinkItemStatusEvent` distinct from `EvaluationResult`,
  `RoutingDisposition`, and `ProposalRecord`.
---

## Interaction contract

The user explicitly invokes **Invite perspective** from Co-think’s own action
block beside Co-work Verify. Both actions use the same **Action target**
chooser and display the selected provider/model, but Co-think does not inherit
Verify’s criteria, results, goal, or proposal path. Co-think does not infer a
need for reflection from cursor
movement, scrolling, dwell time, edit repetition, or other passive behavior.

The worker receives:

- the complete permitted frozen document;
- exact target and context/change/egress boundaries;
- the user’s purpose;
- protected intent; and
- a schema requiring either `perspective` or `none`.

A perspective must contain nonempty content and a rationale. `none` is an
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
coordinator cannot silently run Co-think. The transition remains a suggestion
until the user invokes it.

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
