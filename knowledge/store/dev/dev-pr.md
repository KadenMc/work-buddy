---
name: Dev PR
kind: workflow
description: Commit work-buddy code changes with test verification, chained doc update, PII scan, cleanup review, and commit metadata recording. Replaces the prose /wb-commit directions.
workflow_name: dev-pr
execution: main
allow_override: false
steps:
- id: assess
  name: Snapshot git state (branch, changed files, test candidates)
  step_type: code
  depends_on: []
  auto_run:
    callable: work_buddy.dev.commit.assess_state
    kwargs: {}
    timeout: 20
  invokes: []
- id: branch_guard
  name: Confirm branch (or create one off main)
  step_type: reasoning
  depends_on:
  - assess
  result_schema:
    required_keys:
    - branch
    - ready
    key_types:
      branch: str
      ready: bool
  invokes: []
- id: test
  name: Run relevant tests; record pass/fail
  step_type: reasoning
  depends_on:
  - branch_guard
  result_schema:
    required_keys:
    - tests_run
    - tests_passed
    - tests_failed
    - blocking
    key_types:
      tests_run: list
      tests_passed: int
      tests_failed: int
      blocking: bool
  invokes: []
- id: document
  name: Update knowledge-store docs via /wb-dev-document (or explicitly skip with rationale)
  step_type: reasoning
  depends_on:
  - test
  workflow_ref: dev-document
  result_schema:
    required_keys:
    - skipped
    key_types:
      skipped: bool
  invokes: []
- id: pii_check
  name: Scan changed files for PII patterns
  step_type: code
  depends_on:
  - document
  auto_run:
    callable: work_buddy.dev.commit.pii_check
    kwargs: {}
    timeout: 30
  visibility:
    mode: summary
    include_keys:
    - files_scanned
    - hits
    - clean
  invokes: []
- id: cleanup
  name: Review own changes for debug code, stale refs, PII hits
  step_type: reasoning
  depends_on:
  - pii_check
  result_schema:
    required_keys:
    - ready
    key_types:
      ready: bool
  invokes: []
- id: commit
  name: Stage precisely and commit
  step_type: reasoning
  depends_on:
  - cleanup
  result_schema:
    required_keys:
    - commit_hash
    - message
    key_types:
      commit_hash: str
      message: str
    min_items:
      commit_hash: 7
  invokes: []
- id: record
  name: Record structured commit metadata via commit_record
  step_type: code
  depends_on:
  - commit
  visibility:
    mode: summary
    include_keys:
    - status
    - commit_hash
    - summary
  invokes:
  - commit_record
- id: push
  name: Ask user about push + PR; act on the answer
  step_type: reasoning
  depends_on:
  - record
  invokes: []
command: wb-dev-pr
tags:
- dev
- commit
- git
- testing
- knowledge-docs
- workflow
- pii
aliases:
- commit work-buddy
- dev commit
- commit with doc update
- wb dev commit
- dev-pr workflow
parents:
- dev
- dev
dev_notes: |-
  The `document` step uses `workflow_ref` for agent-mediated sub-workflow chaining (the conductor exposes the ref in `current_step.workflow_file`; the agent starts the sub-workflow and advances with its result). This is the task-new-style pattern: the parent workflow's step is a reasoning step, not an auto_run, so the agent can decide whether to run the sub-workflow or take the skip path.

  Skip enforcement is currently prose-level: `result_schema` only requires `skipped: bool`. A future tightening could add a step hook that requires `skip_rationale` when `skipped: true`, but the current design trusts the agent to follow the step_instruction.

  `pii_check` is scoped to the current diff + untracked set rather than a staged view. Running it before staging lets the agent fix hits in-place; if you run it after staging you'd need `git diff --cached` plumbing.
---

Commit work-buddy code with test verification, doc hygiene (via chained /wb-dev-document), PII scan, cleanup review, structured commit metadata recording, and optional push + PR.

## Philosophy

The previous `/wb-commit` was a prose checklist loaded by a slash command. Agents skipped the doc-update step under time pressure and silently left stale knowledge units in place. This workflow makes the doc check a DAG step whose skip requires explicit rationale, recorded in the commit body — so skipping is auditable, not invisible.

## What this workflow is NOT

- Not a general code-review step. Use `/security-review` or a PR reviewer for deeper passes.
- Not a substitute for the agent's judgment on what 'precise staging' means. You still need to read your own diffs.
- Not a gate on operational work. Only use from dev mode (after `/wb-dev` orient) when changing the work-buddy codebase itself.

## Doc-update skip policy

`document.skipped: true` is allowed but expensive: the skip rationale lands in the commit body, visible forever. Use only when the change genuinely documents nothing observable (pure typos, whitespace, test-only internals). When in doubt, run dev-document — empty `proposals: []` is cheap and honest.

## assess

Auto-run. The conductor calls `work_buddy.dev.commit.assess_state()` and passes the result forward. Returned fields:
- `current_branch`, `is_main`: branch info.
- `changed_files`, `classified`: what you're about to commit, by bucket (module / knowledge / slash / tests / config / other).
- `test_candidates`: test files that *might* apply, heuristically derived from changed module names.
- `warnings`: soft signals (on main, direct JSON edits, empty diff).

## branch_guard

Reasoning step. Read `assess.is_main`. If true, tell the user they're on a protected branch and create a new one:

```bash
git checkout -b <branch-name>
```

Naming:
- `fix/<short>` — bug fixes
- `feat/<short>` — new features or capabilities
- `docs/<short>` — documentation-only
- `chore/<short>` — config / CI / deps

If on a feature branch already, confirm the name matches the work and proceed.

Advance with `{"branch": "<current branch name>", "ready": true}`. Set `ready: false` only if the user explicitly aborted.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## test

Reasoning step. For each module file you changed (`assess.classified.module`), run its matching test file(s). The conductor suggested candidates in `assess.test_candidates` — check them first, but also look for test files that import the modules you changed if the heuristic missed them.

Run via:

```bash
export WORK_BUDDY_SESSION_ID='test-session-00000000' && \
  python -m pytest <test_files> -q --tb=short
```

Record: which files ran, pass count, fail count. If any failure is on code **you changed**, advance with `blocking: true` and fix before retrying the step. If failures are on code you didn't touch, note them and advance with `blocking: false`.

If no test files apply (e.g. pure doc change), advance with `tests_run: []`, `tests_passed: 0`, `tests_failed: 0`, `blocking: false`.

Advance shape:
```json
{
  "tests_run": ["tests/unit/test_foo.py"],
  "tests_passed": 12,
  "tests_failed": 0,
  "blocking": false,
  "rationale": "optional — e.g. 'no tests apply for pure doc change'"
}
```

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## document

Reasoning step with a sub-workflow reference. The parent workflow expects you to run `/wb-dev-document` (workflow name `dev-document`) to propagate your code changes into the knowledge store before committing.

## The normal path

```
mcp__work-buddy__wb_run("dev-document")
```

Run the sub-workflow to completion (scan → propose → confirm → apply → validate → report). Then advance this step with:

```json
{
  "skipped": false,
  "doc_result": { /* the dev-document apply step's result */ },
  "units_updated": ["<unit path>", ...]
}
```

If dev-document's `validate` step reports failures, resolve them **before** advancing this step — don't commit with a broken store.

## The escape hatch (use sparingly)

If the commit is genuinely trivial — a typo, formatting, a test-only change that documents nothing observable — you may skip. `skip_rationale` is required and must be specific; 'trivial' alone is not acceptable:

```json
{
  "skipped": true,
  "skip_rationale": "Typo fix in a docstring; no behavior change."
}
```

The rationale will be included in the commit body at the `record` step so the skip is auditable. If you find yourself writing a long rationale, that's a signal the change is not trivial — run dev-document instead.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## pii_check

Auto-run. The conductor calls `work_buddy.dev.commit.pii_check()` against the current change set. Returns `{files_scanned, hits, clean}`.
- `clean: true` → proceed.
- `hits` → each entry has `file`, `line`, `label`, `match`, `context`. Fix before committing. Common fixes: replace personal paths with config-driven values (`cfg["vault_root"]`), replace named references to the user with 'the user', move machine-specific paths to gitignored configs.

## cleanup

Reasoning step. Walk each file you changed and look for:
- Debug prints, `TODO`/`FIXME` you left while working.
- Stale imports or references to things you renamed.
- Dead code from a refactor.
- **PII hits** from the previous step — if any, fix them now.
- **Transient narrative in durable surfaces.** Scan changed code (identifiers, comments, log strings, tests) and agent docs (knowledge units, slash-command text, CLAUDE.md) for references whose meaning depends on the *moment* the change was made: branch names, PR numbers, dates, agent-session tags, "after the recent migration", "Slice 3"-style stage labels, "for now", "temporary workaround", or identifiers that read as archaeology (`legacy_*`, `_after_migration_shim`, `pre_2026_04_26_*`). Replace with stable domain terms describing current behavior. Versioned labels are allowed only when they name a documented interface, schema, protocol, migration, or compatibility boundary; otherwise rename. Commit messages are exempt — they're a write-once journal of how the change arrived.

Do not refactor unrelated code. Scope is your own changes only.

Advance with `{"ready": true}` when clean. If you find and fix issues, mention them in your response so the user sees the pass happened.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## commit

Reasoning step. Stage and commit.

## Sign off every commit (DCO)

work-buddy enforces a Developer Certificate of Origin: every commit must be signed off with the `-s` flag (`git commit -s ...`), which appends a `Signed-off-by` trailer. The `DCO` status check is required on `main` — a pull request with any unsigned commit cannot merge. Commit with `-s` from the start; to repair commits already made without it, run `git rebase --signoff origin/main` and force-push.

## Stage precisely

Do NOT use `git add -A` or `git add .`.

- When you own the whole file's diff: `git add <file1> <file2>`.
- When a file mixes your hunks with another agent's: `git add -p <file>` and accept only yours.

Verify with `git diff --cached --stat` that only your intended changes are staged.

## Commit message

Follow the repo's conventional-commit style (check `git log --oneline -5`). Keep the subject short and imperative; put the why in the body.

If the `document` step was skipped, paste the `skip_rationale` into the commit body under a `Doc-update skipped:` line so the skip is visible in history.

Use a HEREDOC for multi-line commit messages, and keep the `-s`:

```bash
git commit -s -m "$(cat <<'EOF'
<subject>

<body>
EOF
)"
```

Advance with:
```json
{
  "commit_hash": "<full or 7+ char hash from git output>",
  "message": "<subject line>",
  "staged_files": ["..."]
}
```

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## record

Code step. Dispatch `commit_record` via `wb_run` with structured metadata. Pull most fields from prior step_results:

```
mcp__work-buddy__wb_run("commit_record", {
    "commit_hash": <commit.commit_hash>,
    "message": <commit.message>,
    "branch": <branch_guard.branch>,
    "files_changed": ",".join(<commit.staged_files>),
    "tests_run": ",".join(<test.tests_run>),
    "tests_passed": <test.tests_passed>,
    "tests_failed": <test.tests_failed>,
    "knowledge_units_updated": ",".join(<document.units_updated or []>),
    "summary": <one-line plain-English summary>,
})
```

Return the result dict unchanged. If `commit_record` fails, surface the error and proceed to the push step — a failed metadata record does not block the commit from being pushed.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## push

Reasoning step. Ask the user: **"Want me to push and create a PR?"**

If yes:

```bash
git push -u origin <branch>
gh pr create --title "<concise title>" --body "$(cat <<'EOF'
## Summary
<1-3 bullets>

## Test plan
- [ ] <what to verify>

Task: <task-id if any>
EOF
)"
```

Report the PR URL back.

If the user declines, confirm the commit is on the local branch and remind them they can push later. Do NOT push without asking — this is a shared-state action that requires explicit consent.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.
