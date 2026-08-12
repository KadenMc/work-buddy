# Task-note compatibility migration implementation

Status: implemented as a conservative, disabled-by-default PR 4 foundation.

## Authority boundary

The Tasks master list, task scheduling metadata, and existing
`[[note_uuid|📓]]` links remain unchanged. Only the Markdown body addressed by
the stable `note_uuid` can move to a bound Co-work document.

Each task note, Running Note, and logical-day Log has its own authority epoch.
A rollout cohort may decide which entities are eligible, but it is not an
authority. Task-note cutover requires all of the following:

1. an exact file-origin Source captured by `work-buddy-file-import` with
   authorship explicitly unknown;
2. a bound shadow Co-work document;
3. recorded normalized parity (BOM and newline encoding are the only
   normalization; byte parity is retained separately);
4. current persisted structured-Journal exit evidence, including the exact
   production-callsite digest;
5. the task-note cutover gate; and
6. an explicit, still-open rollback deadline.

The task-note cutover gate ships closed. Journal migration cutover is also
disabled by deployment configuration; the task-note operator accepts only a
current Journal-owned exit receipt and cannot set a task-local substitute.
Merely enabling either adapter does not cut over any entity.

## Production content seam

`TaskNoteContentAdapter` now owns task-note body reads/writes for:

- task creation, verification, deletion, `task_read`, and `task_assign`;
- context drill-down;
- task-note IR discovery and parsing;
- density heuristics;
- historical creator-session backfill; and
- email-thread append-to-task-note.

Path-shaped observability and provenance detectors continue to recognize
`tasks/notes/<uuid>.md`; they observe access and never read or mutate the note
body. The master list continues through its existing Tasks/Obsidian pipeline.

## External Markdown edits

After cutover, Markdown is an externally editable compatibility projection.
Every projection records:

- the prior file hash;
- result file hash;
- authority epoch;
- projection generation; and
- bound document head.

The projection contains a content-free generation marker. A retry after an
ambiguous write recognizes the exact marker/result and records the missing
receipt. If the current file differs from the expected base, the worker
captures the exact changed file through `work-buddy-file-import`, pauses that
note, and does not overwrite it.

## Recovery and retirement

Creation, deletion, retirement, and recovery are represented by idempotent
sagas with stable idempotency keys and explicit completed steps. The existing
task retry path can replay the same operation without minting another UUID.
Binding rollback fences the exact Co-work epoch, increments the authority
epoch, fails prepared projection intents, and clears the cursor head so stale
workers cannot publish after rollback.

## Intentionally gated follow-up

Arbitrary legacy append operations still fail closed for a
Co-work-authoritative task-note body. Bound reads, shadow import, authority
cutover/rollback, source-backed whole-document replacement, projection,
divergence capture, and retirement are implemented. Append remains gated until
it can use the same crash-safe source reservation, document-change, usage, and
projection receipts. This is why the shipped cutover gate remains closed
rather than silently creating two writers.
