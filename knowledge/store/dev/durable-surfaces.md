---
name: Durable surfaces — no transient narrative
kind: directions
description: 'Authoring rule for code and agent docs: describe the system''s current behavior, not the journey of how it got there. No commit hashes, branch names, PR numbers, dates, agent-session tags, stage labels, or ''after X'' framing in code identifiers, comments, log strings, tests, knowledge units, slash-command text, or CLAUDE.md.'
trigger: When authoring or updating code (identifiers, comments, log strings, tests) or agent docs (knowledge units, slash-command text, CLAUDE.md) during /wb-dev or /wb-dev-document. Surfaced via cross-reference from dev/dev-mode, dev/dev-document-directions, and dev/dev-pr-directions so the rule travels with every authoring context.
tags:
- allow-transient-labels
- dev
- doc-hygiene
- durable-surfaces
- transient-narrative
- evergreen
- code-quality
aliases:
- durable surfaces rule
- no transient narrative
- evergreen content rule
- no commit hashes
- no stage labels
- no session dates
- evergreen docs
parents:
- dev
- dev
---

Code (identifiers, comments, log strings, tests, error messages) and agent docs (knowledge units, slash-command text, CLAUDE.md) are **durable surfaces** — future agents (operational and developmental) re-read them every time they touch the area. They must describe the system's *current* behavior, not how the change arrived.

## Forbidden in durable surfaces

- Branch names, PR numbers, commit hashes
- Dates, agent-session tags
- "after the X migration", "Slice 3"-style stage labels (`Stage 1`, `Phase 4`, etc.)
- "for now", "temporary workaround", "the new approach"
- Variable names that read as archaeology: `legacy_*`, `_after_migration_shim`, `pre_2026_04_26_*`, `new_*` (the new from when?)

## Versioned labels

`v5`, schema versions, protocol versions etc. survive only when they name a documented interface, schema, migration, or compatibility boundary. Otherwise rename to stable domain terms.

## Exempt surfaces (journal-shape, write-once)

These surfaces ARE allowed transient narrative because they are explicitly journal-shape — written once, scoped to a moment, never re-read as system documentation:

- **Commit messages** — `git log` is the canonical journal of what changed.
- **CHANGELOG.md** — by definition.
- **DECISIONS.md and session-summary docs in repo root** — single-session narratives.

If you find yourself wanting to write "this was added because…" in a durable surface, rewrite to describe the *resulting state* ("The wrapper exists because consumer X invokes capability Y in side effects, which would re-prompt for consent without it.") and put the historical narrative in the commit message.

## The cheap-fix test

When authoring, ask: *would this sentence still read correctly six months from now, written by an agent who has no memory of this session, this branch, or this PR?* If the answer is "no, because it references an event," rewrite to describe the resulting state.

## Audit

Two mechanisms enforce this rule, with different scopes:

- **Store-wide, programmatic.** The `durable_surfaces` check in `work_buddy/knowledge/validate.py` scans every knowledge unit's prose fields (name, description, tags, summary, full content, dev_notes, capability parameter schemas, workflow step text) for stage labels, dates, VCS references, task ids, and migration-narrative phrasing. It runs on every `docs_validate` invocation, including the dev-document workflow's validate step, so archaeology in units no commit touches still surfaces. Findings are advisory warnings, and the open warning list doubles as the cleanup backlog. Units whose subject matter legitimately contains such patterns (a documented numbered interface, or this rule's own quoted examples) opt out with the `allow-transient-labels` tag.
- **Diff-scoped, programmatic + judgment.** `/wb-dev-pr`'s `transient_check` auto_run step (`work_buddy.dev.commit.transient_check`) scans the change set, code and docs alike, with the same pattern table plus an identifier-form pattern for code names. Its hits feed the cleanup step, where the committing agent judges each one: quoted examples and versioned interface names are legitimate, rollout labels are not. The agent also self-reviews for what regex cannot see. Code files outside the current change set have no repo-wide scan; the knowledge store is the only corpus scanned exhaustively.

The cheap fix is not writing it in the first place: at authoring time, with one rule loaded and the words still in front of you, not at PR time when you have to grep your own work for date-shaped strings.
