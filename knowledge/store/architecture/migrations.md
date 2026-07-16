---
name: Schema Migration Ladder
kind: concept
description: 'Per-DB versioned SQLite migration runner: PRAGMA user_version as authority, _migration_history for audit, AST-based hashing (ignores cosmetic edits, catches behavioral ones, stable across Python versions), downgrade guard, transaction-wrapped apply, baseline-stamp for adopting on legacy DBs.'
tags:
- migrations
- schema
- sqlite
- version
- pragma-user-version
- ddl
- ladder
- data-safety
aliases:
- migration runner
- schema versioning
- user_version
- _migration_history
- MigrationRunner
- schema ladder
- migration framework
parents:
- architecture
- architecture
dev_notes: |-
  ## Hashing pipeline

  `_hash_callable` runs: `inspect.getsource(fn)` returns the function text; `ast.parse` produces a syntax tree; `_strip_docstrings` removes docstring `Expr` nodes (the AST's only carrier of docstring text); `_normalize_outer_fn_name` rewrites the top-level `FunctionDef.name` to `<fn>` so the Python binding identifier doesn't change the hash; `ast.unparse` round-trips the tree back to canonical source text; SHA-256 reduces that text to the stored digest. The migration's identity is its position in the runner's migrations list, not its symbol name.

  ## Why `ast.unparse`, not `ast.dump` or bytecode

  `ast.unparse` is load-bearing for cross-Python stability -- verified by hashing the `TASK_MIGRATIONS` ladder identically under Python 3.11 and 3.13. `ast.dump` would have worked too, but its empty-field default changed in 3.13 and `_fields` itself shifts between minor releases when new fields like `type_params` are added; `ast.unparse` sidesteps both because the unparser only emits syntax it can re-parse. The previous `bytecode_v1` format (`dis.get_instructions` argvals) produced different hashes on Python 3.11 vs 3.13 for the same source, since the CPython compiler emits subtly different instruction streams between releases -- that was the regression `ast_v1` exists to fix.

  ## What's not normalised

  Argument names and local variable names are still part of the hash, matching the conservative posture of the previous bytecode-based implementation. Verified for cosmetic-vs-behavioural edge cases by `tests/unit/test_storage_migrations.py::TestHashCallable` (19 tests). If a future ergonomic change wants to also normalise locals, that's a deliberate decision -- the test `test_local_variable_rename_is_detected` locks the current contract.

  ## Format-bump path

  The `HASH_FORMAT_CURRENT` constant in `work_buddy/storage/migrations.py` is the version identifier; bump it when the algorithm changes -- legacy rows with a prior identifier get silently re-stamped on first open via `_verify_history_hashes`. This is the same mechanism that handles the bootstrap of the `hash_format` column on pre-`ast_v1` DBs.

  ## Test helper requirement

  `inspect.getsource` is the source-extraction mechanism, so migrations defined inside `exec`-d strings are invisible to it. Tests that need to compile functions dynamically must register source in `linecache` under a unique synthetic filename before `exec` -- see `tests/unit/test_storage_migrations.py::_compile_fn` for the pattern.

  ## Audit-hash memoization

  `_hash_callable` memoizes its result, weak-keyed on the migration callable. `MigrationRunner.run()` runs on the hot path of every projects / entities / tasks store `get_connection`, and a callable's source-AST hash is invariant across a process lifetime, so re-deriving it (`inspect.getsource` + `ast.parse` + `ast.unparse` + SHA-256) on every open was wasted work. The weak key lets test-compiled throwaway callables be garbage-collected. See `architecture/hot-path-discipline`.

  ## Truth-store specialization

  `work_buddy/truth/migrations.py` subclasses `MigrationRunner` rather than using the shared runner unchanged. A scoped `.wb-truth/store.db` has dual version markers (`PRAGMA user_version` and `store_info.schema_version`), rejects unversioned partial schemas, and snapshots every existing version before its bump. The specialization preserves the shared transaction and downgrade guarantees while adding the permanent-identity and append-only-ledger contracts described in `architecture/truth`.

  Released Truth schema fixtures are immutable. Add a new `tests/fixtures/truth/frozen_vN/` directory for a new release instead of regenerating an old fixture. Supported older recovery streams are upcast during staged import; durable ledger rows and record IDs are not rewritten in place to simulate a new format.
---

Per-DB versioned migration framework built on `PRAGMA user_version` plus a `_migration_history` audit table. Lives in `work_buddy/storage/migrations.py`. Used by every vital DB store module and by the `data_restore` pipeline to forward-roll a staged snapshot to current schema.

## Why we wrote our own

The canonical alternative was sqlite-migrate. Rejected: it provides no transaction wrapping, no race lock, no PRAGMA handling, no hash check, and no downgrade guard, and is self-described as alpha. Every safety item would have to be added on top with no advantage over a small in-tree implementation. ~250 LOC is the right size; a vendored dep would mean owning all the same code plus a third-party surface.

Larger tools (Alembic, Flyway, Goose, yoyo-migrations, Liquibase) are correct in shape but oversized for SQLite-only single-process work-buddy. We adopted their *invariants* without their machinery -- the safety rules below are what survives the down-scoping.

## Per-DB ladder shape

Each store module declares an ordered list of migration callables and constructs a `MigrationRunner`:

```python
TASK_MIGRATIONS = MigrationRunner("task_metadata", migrations=[
    Migration(1, "initial schema",                       _m001_initial),
    Migration(2, "GTD vocabulary columns",               _m002_slice2_gtd),
    Migration(3, "description column",                   _m003_description),
    Migration(4, "risk profile + tier + last_actor",     _m004_risk_tier_actor),
    Migration(5, "agent/user required context arrays",   _m005_context_arrays),
    Migration(6, "action_items table + current pointer", _m006_action_items),
    Migration(7, "task_sync_status freshness table",     _m007_task_sync_status),
    Migration(8, "soft-delete deleted_at columns",       _m008_soft_delete),
    Migration(9, "drop ON DELETE CASCADE from FKs",      _m009_drop_cascade),
    Migration(10, "lww_meta write-provenance sidecar",   _m010_lww_meta),
])
```

The tasks ladder lives in `work_buddy/obsidian/tasks/migrations.py`. Each migration callable takes `sqlite3.Connection`, performs idempotent DDL, returns nothing.

## Project invariants

Enforced by code where possible and by reviewer discipline where not:

- **Never edit a shipped migration callable's behaviour.** The runner hashes each callable's *source AST* (see below) and refuses to proceed on mismatch. Cosmetic edits (docstring rewording, comment changes, whitespace, formatting, outer function rename) are explicitly tolerated; any change to control flow, string literals, or referenced names trips the audit.
- **One logical change per migration step.** Bundles get split.
- **Migration + version bump are one transaction.** A mid-migration crash that leaves the DB in a state that doesn't match the stamped version is unrecoverable.
- **Migration 1 = fresh-install schema** -- the "fresh install = fully migrated install" invariant. A DB created from migration 1 must arrive at the same schema as a long-lived DB walked through every migration.
- **`PRAGMA foreign_key_check` returns 0 rows** after every table-rebuild migration. Run inside the transaction, before commit.
- **Validate data before applying constraint.** For migrations that tighten an invariant (`NOT NULL`, `CHECK`, etc.), scan the data first.

## Runner behaviour

Load-bearing details in `work_buddy/storage/migrations.py`:

- **`PRAGMA foreign_keys = OFF` outside the transaction.** SQLite treats it as a no-op inside an open transaction -- a documented quirk. Always set before `BEGIN`.
- **`BEGIN IMMEDIATE` not `BEGIN`.** Acquires a write lock immediately; without it, two concurrent openers can both read the same `user_version` and both attempt to apply.
- **`PRAGMA user_version` is authoritative.** 32-bit integer in the SQLite header, transactional with the DDL, no table scan. `_migration_history` is for audit and hash checks only -- never queried for version.
- **Table-rebuild migrations follow the 12-step pattern** from SQLite's ALTER TABLE docs: create new table, copy data, recreate indexes/triggers/views, `PRAGMA foreign_key_check`, drop old, rename new. Used for column drops on SQLite <3.35, renames, and FK changes (`_m009_drop_cascade` is the canonical in-tree example).
- **Per-migration transaction** wraps the callable + the `INSERT INTO _migration_history` + the `PRAGMA user_version = N` bump. Mid-migration crash rolls back atomically.

## Hash audit: what the audit actually compares

The runner hashes each migration callable's source AST and verifies the stored hash on every open. Cosmetic edits are tolerated; behavioural edits trip the audit. A `MigrationHashMismatch` means someone edited a shipped migration in place -- the fix is to add a *new* migration step that corrects whatever the edit was for, never to patch the old step.

What the audit IGNORES (zero behavioural impact):

- Docstring text changes, additions, removals.
- Comment edits, blank lines, whitespace, reformatting.
- Renaming the migration's outer function (the `def <name>` line).

What the audit CATCHES (real behavioural changes):

- Any change to a string literal that the function loads (e.g. the DDL inside `execute()`).
- Any change to control flow, calls, or operators.
- Any change to which globals or builtins are referenced.
- Renames of arguments or local variables.

The audit's stored format is identified by a `hash_format` column on `_migration_history` (currently `'ast_v1'`). Rows with any other value -- including NULL -- are treated as legacy stamps and silently re-stamped with the deployed source's hash on first open. The framework's bootstrap path adds the `hash_format` column to pre-existing tables idempotently. This same re-stamp mechanism doubles as the algorithm-upgrade path: bumping `HASH_FORMAT_CURRENT` is how a future hashing-algorithm change rolls out without manual intervention.

## Baseline-detect (adopting the framework on legacy DBs)

When the runner opens a DB with `PRAGMA user_version = 0` and tables that already exist, the heuristic is: "this is a real production DB that was running the latest informal schema before the framework existed -- adopt it as fully-migrated." The runner stamps `user_version = target` without re-running migrations, and writes a `_migration_history` row marked as baseline-adoption.

This is correct for production. It is *wrong* for tests that hand-craft a partial-schema DB to exercise a specific migration -- those tests must explicitly `PRAGMA user_version = N` to tell the runner where to start.

## Integration with restore

The `data_restore` pipeline (see `architecture/backups`) opens each staged snapshot DB through the same `MigrationRunner.run()`. The manifest's `schema_versions[db]` becomes the source of truth for refusing forward-time travel -- restore refuses any snapshot whose recorded version exceeds the current code's max migration. Older snapshots roll forward through the ladder.

## Status across vital DBs

The `tasks` DB (`task_metadata`, through migration 10) and the `projects` DB (through migration 7) both have built-out ladders. `messages` and `threads` currently sit at `user_version = 0` with no migration list; they pick up the framework by writing migration 1 = fresh-install schema once their schemas need to change. The `projects` ladder lives in `work_buddy/projects/migrations.py`.

## Scoped Truth stores

Truth stores use a specialization of this migration framework because each `.wb-truth/store.db` is a portable, independently versioned ledger rather than a shared data-root database. A store migrates on open, refuses a future schema before any mutation or snapshot, and creates a pre-version snapshot before each version bump. `PRAGMA user_version` and the store's own schema marker must agree.

Schema evolution is additive with respect to durable history: ledger content, permanent store identity, record IDs, and `wb-truth:` references survive. Checked-in fixtures from every released schema provide the upgrade contract, while supported older JSONL recovery formats are upcast and rebuilt under the current engine in a staged store. See `architecture/truth` for the broader authority and portability contract.

## See also

- `architecture/backups` -- uses this framework to forward-roll staged DBs during restore.
- `work_buddy/storage/migrations.py` -- the `MigrationRunner` class.
- `work_buddy/obsidian/tasks/migrations.py` -- the tasks ladder (canonical reference implementation).
- `tests/unit/test_storage_migrations.py` -- the audit-behaviour contract tests.
- `architecture/truth` -- scoped Truth-store identity, integrity, recovery, and migration guarantees.
- `work_buddy/truth/migrations.py` -- the Truth-specific migration specialization.
