---
schedule: "7 * * * *"  # hourly at :07, off the top-of-hour pile-up
recurring: true
type: capability
capability: journal_triage_scan
params: {}
---
Background triage of the current day's Running Notes.

Segments the same-day section into threads via the local LLM,
enriches each with hybrid-IR context, and invokes a local agent
that submits one verdict per thread into the pending-review pool.

The cadence (`schedule:` above) is a job-level concern — change it
here without touching the capability. The capability itself has no
opinion on how often it runs.

Never mutates the vault. The user reviews accumulated proposals
on demand via `wb_run("triage_review_pool", ...)` — no modals
fire automatically from this job.

Idempotent: if the same-day content hash is unchanged since the
last run, the job returns status=skipped without calling the LLM.
