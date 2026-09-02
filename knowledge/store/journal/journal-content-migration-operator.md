---
name: Retired Journal Content Migration Operator
kind: concept
description: Historical description of the retired Markdown-to-Co-work compatibility migration. The private native Journal importer supersedes this path.
tags:
- journal
- cowork
- migration
- authority
- recovery
aliases:
- migrate Journal prose
- Journal migration inventory
- migrate Running Note
- migrate daily Log
- Journal authority rollback
parents:
- journal
---

`inventory` is content-free and read-only. Every mutating action has its own
high-weight, zero-TTL consent boundary, so selection, source capture, cutover,
rollback, recovery, and exit certification cannot inherit a workflow grant or
silently reuse a prior approval.

The public capability is no longer registered in the MCP capability registry.
This concept remains in the knowledge store only as historical and recovery
documentation. Its code is retained to interpret or recover older per-section
migration receipts; do not use it for the database-only Journal cohort.

Historically, the deployment gates `journal.content_migration.enabled` and
`journal.content_migration.cutover_enabled` both default to false and cannot be
changed through this capability. Shadow import records exact file/section
digests, unknown file-origin authorship, a source-backed document binding, and
separate byte, newline/BOM-normalized, and narrowly structural Markdown parity
facts. Structural parity covers only the representation-neutral list-marker
and section-edge separator changes made by the current document kernel; it
does not normalize prose whitespace or visible formatting. Unmarked Running
Notes require an explicit reviewed line range before an opaque stable identity
is assigned; the range itself is not durable identity.

Cutover is per stable Running Note or per logical-day Log. It requires parity,
a future rollback deadline, and the closed-by-default deployment gate. The
document-kernel binding is the canonical authority epoch. Markdown becomes an
editable compatibility projection guarded by section CAS; an unexpected
external change is captured as an exact Source and pauses that entity rather
than being overwritten. Once a logical-day Log is cut over, its whole Log
section is owned; generic Journal writers cannot append around the managed
block. Rollback projects the latest head, fences the Co-work
epoch, and only then removes managed markers to restore Markdown authority.

`certify_exit` is not a freely asserted gate. It rescans the vault and refuses
to write evidence while any Running Note is unadmitted, any entity lacks
parity, any projection is diverged, or any migration operation is recoverable.
Dependent task-note cutover must use `latest_current_exit_evidence` (or the
service's `latest_exit_evidence`) rather than treating the latest stored row as
a Boolean. The current verifier recomputes the stable entity-topology and
reviewed-callsite digests and checks current legacy comparison plus owned-file
body/cursor/head agreement. Subsequent cohort, authority/projection, external
file, projection-lag, or callsite changes leave the historical receipt
auditable but make it ineligible for a dependent cutover.
