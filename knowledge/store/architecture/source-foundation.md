---
name: Shared source foundation
kind: system
description: Authority-qualified source identity, exact retained representations, provenance, authorized resolution, disclosure, dependency maintenance, and redaction beneath Work Buddy domains.
summary: Sources records what content occurrence a domain used without collapsing inputter, author, semantic producer, reviewer, or applier. Domains retain their own portable receipts and mutations; Sources owns exact retained representations, resolution/access, usage dependencies, and a durable outbox for maintenance such as redaction.
entry_points:
- work_buddy.sources
- work_buddy.security.local_identity
- work_buddy.agent_execution.disclosure
- work_buddy.journal_capture
- work_buddy.document_kernel
tags:
- sources
- provenance
- identity
- disclosure
- redaction
- outbox
- causality
aliases:
- Sources store
- source ledger
- source foundation
- shared source layer
parents:
- architecture
dev_notes: |-
  Treat `SourceRef` as provider-independent retained-source identity, never as a claim, evidence relationship, document, or generic artifact. `OriginRef` is provider-qualified native identity. Both include an authority namespace; paths, turn indexes, snippets, and content search are not identity.

  Inputter, author, semantic producer, selector, reviewer, decision actor, and applier are separate roles. A local UI gesture proves an enrolled local session submitted exact bytes under the documented loopback threat boundary; it does not prove sole physical authorship. Agents and callers cannot assert a trusted human actor through headers or payload fields.

  Resolve and reserve outside a destination write lock, recheck the redaction epoch immediately before the destination mutation, persist a domain-local receipt, then acknowledge the usage. A crash leaves maintenance incomplete and recoverable. Never report managed-copy redaction complete merely because the current pointer changed; retained history, projections, snapshots, excerpts, and mixed derivatives remain part of the coverage audit.

  Agent Execution owns model runs and the ordered disclosure manifest. Sources grants/reserves exact content boundaries. `possibly_sent` is irreversible/ambiguous and cannot be replayed automatically.

  A machine restore publishes a central Source Foundation recovery fence. While present, persistence authorities—including filesystem-only Y.Doc tails/blobs and Co-work conversation-source dependencies—remain read-only and every dispatch path fails before transport. The high-consent reconciliation operator validates whole cohorts and never clears on partial evidence. Sanitized local enrollment is a trust input only; sessions and gestures are never restored.

  Sources is deliberately excluded from the existing unencrypted remote vital-backup path. Until encryption, bounded retention, and restore-time redaction fencing are implemented, retained source bytes are local-only and must be exported only through the explicit authorized Sources export contract.
---

# Shared source foundation

`work_buddy.sources` is the common provenance and maintenance layer beneath
Journal, Co-work, Truth, Agent Execution, Hindsight projection, and compatible
task-note migration. It answers a narrow question: **which exact retained
source occurrence did this downstream action depend on, under whose authority,
and what maintenance is still owed?**

It does not become a domain god-store. Journal still owns captures and entries;
Co-work owns documents; Truth owns claims, evidence relationships, and
lifecycle; Agent Execution owns runs; Tasks owns task identity and scheduling.

## Core records

- `SourceRef` is server-minted and authority-qualified. It survives provider
  location changes and cannot collide with an item minted by another Work Buddy
  authority.
- `OriginRef` identifies a provider-native occurrence using an explicit
  provider and authority instance. Capturing the same native origin reuses the
  retained item under the provider's uniqueness contract.
- `SourceRepresentation` records exact digest, byte length, media type,
  encoding, and retained bytes or a content-addressed blob.
- attribution assertions, observations, and derivations are append-only. Later
  assertions supersede earlier ones without rewriting them.
- access bindings authorize a principal, purpose, content boundary, and any
  external recipient/model.
- usage reservations record the downstream consumer, exact or semantic use,
  disclosure class, redaction policy, and bound redaction epoch.
- the Sources outbox is the durable delivery authority for commands and source
  maintenance. Events/SSE may wake consumers but are not commit authority.

## Trusted local human ingress

The local identity authority persists a machine/tenant-qualified actor. A
trusted host launch delivers a one-use bootstrap in the URL fragment; the
browser immediately removes it, redeems an HttpOnly same-site session, and uses
Origin/CSRF plus a one-use exact-context gesture for protected writes. No public
route can mint a bootstrap, and caller actor fields are ignored.

This boundary supports precise language: an enrolled local profile submitted
these exact bytes using a stated input mode. Paste, import, dictation,
automation, and direct entry remain distinct. Human authorship is a separate
attestation when the product actually asks for and records it.

## Domain commit and recovery

The normal cross-database sequence is:

1. resolve the retained representation under an access binding;
2. reserve a usage under the current source redaction epoch;
3. prepare the domain mutation and persist its intent;
4. recheck the source epoch immediately before the canonical write;
5. commit the domain's source-resolution/change receipt with the mutation;
6. acknowledge the usage and recover either side idempotently after a crash.

Exact managed copies may use a `scrub` policy. Once a user or agent edits an
exact copy into a mixed derivative, its dependency changes to policy-driven
review; source redaction must not erase unrelated later work. Semantic
derivatives normally invalidate, rebuild, or enter review according to the
destination contract.

## Agent disclosure

`work_buddy.agent_execution.disclosure` stores a content-free ordered manifest
for every content handoff. Sources performs the exact resolution and usage
reservation; raw content is passed only through the bounded handoff callback.
The manifest advances through write-ahead states before provider invocation,
and model output binds the manifest digest. Search/connector arguments derived
from protected content are outbound disclosures too.

## Storage and portability boundary

Sources has an explicit authorized export/import format with authority remap
and collision checks. Domain exports remain independently usable because each
domain persists the portable resolution, provenance, and redaction-safe
tombstone facts it needs. Retained Sources bytes are not included in the legacy
unencrypted GitHub backup workflow; this is a deliberate privacy boundary, not
backup coverage.

Every portable scoped Truth backup pairs the Truth ledger with an
identity-bound document-causality companion. Import validates the permanent
store ID and referenced document IDs into one clean staged sidecar before
publication. After machine restore, normal writes and dispatches remain fenced
until identity, Sources/Agent Execution, Co-work conversation dependencies,
Journal/task-note, Truth/causality, and Hindsight projection cohorts reconcile. `possibly_sent` remains ambiguous
until an explicit proven outcome is recorded without transport replay.
