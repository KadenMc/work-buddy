---
name: Durable surfaces — no transient narrative
kind: directions
description: 'Authoring rule for code and agent docs: describe the system''s current behavior, not the journey of how it got there. No commit hashes, branch names, PR numbers, dates, agent-session tags, stage labels, or ''after X'' framing in code identifiers, comments, log strings, tests, knowledge units, slash-command text, or CLAUDE.md.'
trigger: When authoring or updating code (identifiers, comments, log strings, tests) or agent docs (knowledge units, slash-command text, CLAUDE.md) during /wb-dev or /wb-dev-document. Surfaced via cross-reference from dev/dev-mode, dev/dev-document-directions, and dev/dev-pr-directions so the rule travels with every authoring context.
tags:
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

The audit fires programmatically in `/wb-dev-pr`'s cleanup step. The cheap fix is not writing it in the first place — at authoring time, with one rule loaded and the words still in front of you, not at PR time when you have to grep your own work for date-shaped strings.
