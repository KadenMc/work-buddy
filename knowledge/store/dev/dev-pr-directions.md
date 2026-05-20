---
name: Dev PR Directions
kind: directions
description: How to run /wb-dev-pr — chained doc-update via /wb-dev-document, test verification, PII scan, cleanup review, commit, metadata record, push + PR. Replaces the prose /wb-commit directions (dev/commit).
trigger: When the user invokes /wb-dev-pr or asks to commit work-buddy code changes with test verification and structured metadata recording
command: wb-dev-pr
workflow: dev/dev-pr
capabilities:
- commit_record
tags:
- dev
- commit
- git
- testing
- directions
aliases:
- commit directions
- dev commit directions
- wb dev commit directions
- how to commit work-buddy
parents:
- dev
- dev
---

Run `/wb-dev-pr` to commit work-buddy code changes. The workflow enforces branch check → tests → doc update (via /wb-dev-document) → PII scan → cleanup → commit → metadata record → push.

## When to run

At the end of every dev session, once your changes are ready to ship. Run `/wb-dev-document` standalone first if you want to preview doc edits outside the commit flow.

## Sign-off is mandatory (DCO)

work-buddy enforces a Developer Certificate of Origin: every commit must be signed off with `git commit -s`, which appends a `Signed-off-by` trailer. The `DCO` status check is required on `main`, so a pull request with any unsigned commit cannot merge. The `commit` step's instructions sign off for you; if a commit still lands unsigned, run `git rebase --signoff origin/main` and force-push before the PR can pass.

## Doc update is the teeth

`dev-document` runs as a mandatory step. The skip path requires a specific rationale that gets recorded in the commit body. Do not treat skipping as normal — the default is to run dev-document, even when you expect it will propose nothing.

## Cleanup audits the staged change

The `cleanup` step audits staged code and any updated knowledge units for **transient narrative** — branch names, commit hashes, dates, agent-session tags, stage labels, archaeology in identifiers. The audit is a backstop, not the primary gate; the cheap fix is not writing it in the first place. If `cleanup` flags something, fix it in the same commit (re-stage, don't append a new "cleanup" commit). Full rule:

<<wb:dev/durable-surfaces>>

## Not a general reviewer

This workflow verifies *your own* commit is hygienic: tests run, docs in sync, no PII, staged precisely. It does NOT:
- Review the quality of your code design (use `/security-review` or peer review for that).
- Block on failures of tests you didn't touch (note them, proceed).
- Push without asking (it asks explicitly).

## Subworkflow chaining

The `document` step carries `workflow_ref: "dev-document"`. The agent is expected to run the sub-workflow to completion and advance the parent with `{skipped: false, doc_result: ..., units_updated: [...]}`. Skip with `{skipped: true, skip_rationale: "..."}` only when the change truly documents nothing (typo / whitespace / test-only internals).

## Metadata recording

After the commit lands, `commit_record` is called with structured fields: hash, message, branch, files changed, test results, and `knowledge_units_updated` (populated from the `document` step's `units_updated`). The dashboard renders enriched commit cards from this.
