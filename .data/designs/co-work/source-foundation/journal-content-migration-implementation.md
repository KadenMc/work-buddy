# Journal content migration implementation

**Status:** Implemented for the Source Foundation PR 4 boundary on 2026-08-10.

This note records the production contract, not a migration of the user's vault.
Deployment remains closed by default and each content entity advances only
through the consent-gated operator.

## Smallest authority model

The migration unit is deliberately content-sized rather than file-sized:

- one binding for the Log of each logical Journal day;
- one binding for each stable Running Note; and
- no authority claim over Sign-In, planner, carried banners, or unknown daily-
  note sections.

The canonical authority state and monotonically increasing epoch live in the
document-kernel `DocumentCausalityStore` binding. The Journal database mirrors
that state for inventory and recovery, but cannot independently assert a newer
epoch.

Unmarked legacy Running Notes do not receive IDs derived from text, line
number, or placeholder position. An operator must select an exact transient
line range; the Journal store then assigns an opaque stable ID. Repeating the
same reviewed selection is transactionally idempotent, while two occurrences
of identical text receive different IDs.

## Compatibility adapter and production inventory

`JournalContentAdapter` is the authority-aware seam for daily-note reads,
snapshots, creation-if-absent, and whole-file section-CAS changes. Before
cutover it reads and writes legacy Markdown. After cutover it overlays canonical
Co-work content on reads, and only the projection worker may mutate the owned
managed block. Generic daily-note writes must preserve every owned block
exactly. For a cut-over logical-day Log, the authority boundary is the entire
Log section: generic writers may still change unowned Journal sections, but
cannot add prose before or after the managed Log block inside that section.

The reviewed production callsite inventory covers Journal state, Log append,
Sign-In and briefing, source-backed capture materialization, backlog extract/
rewrite/append, day planner, health section creation, Thread cleanup, Journal
collectors/activity, and the generic vault writer. Its canonical digest is:

`826e050c8af35c46a6d413f73157fca32fef924ab3a94418c0dc0a13d0aaddee`

`tests/unit/journal_capture/test_callsite_inventory.py` fails if one of the
reviewed locators disappears or the audited consumers stop using the adapter.
The generic vault writer is the final interposition point for `today`,
`latest_journal`, and explicit daily-note targets, including callers that are
not otherwise Journal-aware.

## Migration sequence

1. `inventory` reports logical-day Log candidates, already managed Running
   Notes, unadmitted legacy prose, authority mirrors, parity, projection state,
   and malformed/unreadable days without returning prose.
2. `select` assigns identity to a logical-day Log, an existing managed Running
   Note, or one explicitly reviewed unmarked Running Note range.
3. `shadow_import` captures the exact selected bytes as a Source with unknown
   file-origin authorship, creates the domain-bound document without cutting
   authority over, and records comparison facts.
4. `cutover` requires a future rollback deadline, clean parity, current
   binding, and both static deployment gates. It installs the outer ownership
   marker with exact file/section CAS, advances the canonical binding epoch,
   projects the canonical head, and only then commits the Journal mirror.
5. `reconcile` completes interrupted receipts idempotently. It never treats a
   captured external divergence as an ordinary retry.
6. `rollback` first projects the latest canonical head, advances the canonical
   binding to domain authority at a new epoch, then removes projection/ownership
   markers from the exact observed file. A crash at either boundary is
   recoverable.

All mutating operator actions have separate high-weight, zero-TTL consent
requirements. Consent is checked before configuration, identity, or store
construction, so denial creates no files or database rows.

## Comparison policy

The store records three independent facts:

- exact byte parity;
- UTF-8 BOM/newline-normalized parity; and
- narrowly defined structural Markdown parity for representation-neutral
  changes currently produced by the document kernel: section-edge blank-line
  separators and CommonMark unordered-list marker choice.

The third comparison does not normalize prose whitespace, headings, emphasis,
ordering, or visible text. `comparison=parity` means at least one declared
comparison succeeded; the individual facts remain observable.

## Divergence and crash behavior

Every projection is based on an authenticated managed-section hash and commits
a durable intent/receipt. A mismatch never triggers a blind write. The exact
changed daily-note file is captured as a Source, the entity mirror becomes
`paused_diverged`, and generic reconciliation remains observational until a
future explicit review/import action resolves it. Unknown and unowned daily-
note bytes remain untouched throughout cutover, projection, reconciliation,
and rollback.

Durable migration-operation states cover prepared shadow work, document
commit, authority-epoch commit, projection commit, recoverable failure, and
paused divergence. Tests inject process stops after document/epoch/file
boundaries and prove a replay settles to one result rather than a duplicate
entity or overwritten file.

## Restore fence

While Source Foundation restore reconciliation is pending:

- `JournalCaptureStore` opens an existing exact-schema database read-only with
  SQLite `query_only`, and neither creates nor migrates state;
- every Journal transaction fails through
  `require_source_foundation_writable`;
- cached Journal dispatch/recovery is fenced before Sources leasing,
  document-domain reconciliation, or model/provider work; and
- migration cutover, rollback, and reconcile are fenced before document-kernel
  dispatch.

Read-only observability may reopen an already-valid retained Journal store, but
it cannot run startup recovery until the cohort fence is cleared by the
separate restore operator.

## Derived exit evidence

`certify_exit` is not a settable gate. It rescans current files and migration
state and refuses certification for unadmitted Running Notes, missing parity,
unsettled operations, malformed days, or divergence. It persists only a
content-free receipt:

```text
receipt_id
inventory_sha256
callsite_inventory_sha256
authority_summary
created_at
```

Dependent systems must call
`latest_current_exit_evidence(vault_root, journal_store, cutover_enabled)` (or
`JournalMigrationService.latest_exit_evidence()`), not read the latest row as a
Boolean. The verifier recomputes the stable cohort and callsite digests. A
historical receipt remains auditable, but is no longer current after a new or
unadmitted entity, authority/projection topology change, or callsite inventory
change. The rescan also compares a Co-work-owned file body with its durable
projection cursor/head and compares legacy content under the recorded parity
policy, so an external edit or projection lag invalidates evidence before a
worker updates the mirrored state.

## Deployment gates

```yaml
journal:
  content_migration:
    enabled: false
    cutover_enabled: false
```

Neither gate can be changed through the migration capability. The gates permit
new cutovers; they do not override the canonical binding for an entity that has
already advanced.
