---
name: Picking a Vault Write Path
kind: concept
description: When to use bridge.write_file_raw directly vs. vault_write — the safe/fallback distinction explained
summary: Two vault-write entry points exist on purpose. vault_write (with direct-write fallback) for content the agent fully owns. bridge.write_file_raw (bridge-only) for files the Tasks plugin has live state for.
tags:
- obsidian
- bridge
- vault
- write
- fallback
- tasks-plugin
- architecture
- typed-exceptions
aliases:
- vault write decision
- which write helper
- _write_note vs write_file_raw
- fallback rules
parents:
- obsidian
- obsidian
---

## Two paths, on purpose

Work-buddy has two ways to write a vault file from Python. They look similar; pick the wrong one and you either lose data on bridge outages or corrupt plugin-owned state.

| Helper | Behavior on bridge down | Use for |
|---|---|---|
| `work_buddy.obsidian.vault_writer.vault_write(path, abs_path, content, *, write_mode='replace', content_hint=None)` | Direct filesystem fallback **only when the Obsidian process is down** (`ObsidianNotRunning` / `is_obsidian_running()` is False). Re-raises every other failure — other `ObsidianUnreachable` subclasses (`ObsidianStartupRace`, plugin missing / disabled), `ObsidianEditorConflict`, `ObsidianPostWriteUncertain`, `ObsidianRefused`, `ObsidianServerError`. | Journals, knowledge units, capture, generic content the user typed (no plugin owns state for these files) |
| `work_buddy.obsidian.bridge.write_file_raw(path, content, *, write_mode='replace', content_hint=None)` | Raises typed `ObsidianError` subclasses; no fallback | Master task list (`tasks/master-task-list.md`), task notes, archives, contract files — anything the Obsidian Tasks plugin has live cache state for |

## Why the split is principled

The Tasks plugin maintains a runtime cache of every task in the vault. Mutations are supposed to flow through `executeToggleTaskDoneCommand` and `task.handleNewStatus` so recurrence rolls forward, done-dates fire, checkbox transitions propagate, and the cache reindexes correctly (see `obsidian/tasks-plugin`).

A direct `Path.write_text()` to the master task list bypasses ALL of that. The file on disk would be correct momentarily, but the plugin's in-memory cache would be stale until the next vault rescan, and any mutation the plugin then applied would be against pre-write state. Recurring tasks would lose their schedule, done-dates would be wrong, and so on.

Non-task files have no equivalent plugin invariant, so a direct write is safe — **as long as no editor is holding the file**. The deciding predicate is the Obsidian **process** state, not bridge reachability: `vault_write` direct-writes only when `is_obsidian_running()` is False (`ObsidianNotRunning`). If Obsidian is running but the bridge is transiently unreachable (startup race / port not yet bound) or otherwise failing, an editor may have the note open. A direct write would update disk while the open editor keeps its old buffer, leaving the two diverged — and Obsidian then reports the file dirty on every subsequent bridge write, wedging it with a persistent `409 editor_dirty` until the user reloads the note. So those cases re-raise (transient) and the gateway / retry queue replays once the bridge recovers, rather than direct-writing.

Per failure type:

- `ObsidianNotRunning` → process is down, no editor can be open. Direct filesystem fallback is safe. FALL BACK.
- `ObsidianStartupRace` / `ObsidianPluginMissing` / `ObsidianPluginDisabled` (other `ObsidianUnreachable`) → Obsidian is running; an editor may hold the note. RE-RAISE — don't diverge it.
- `ObsidianPostWriteUncertain` → RE-RAISE. Body MAY have been sent; the gateway-side post-write-verify reads the file and decides. Filesystem fallback would risk overwriting a successful write.
- `ObsidianEditorConflict` → RE-RAISE. The user has unsaved typing; a filesystem write would clobber it.
- `ObsidianRefused` → RE-RAISE. Structural refusal (4xx other than 409). No retry will help; falling back to filesystem doesn't change the rejection reason.
- `ObsidianServerError` → RE-RAISE. 5xx is plugin-side. Filesystem write would bypass the plugin's state machine, risking cache divergence.

## Typed exceptions (post-CP1–CP9)

Failures from the bridge are typed (see `obsidian/bridge` for the full hierarchy). Capabilities should NOT try/except by default — let exceptions propagate. The `@bridge_retry` decorator catches transient subclasses, retries, and translates to `bridge_failure(...)` dict at exhaustion. The gateway's outer try/except classifies via `isinstance` and enqueues for retry.

The legacy bool/None-checking pattern (`if not bridge.write_file(...): return bridge_failure(...)`) was removed from all known callers in CP6. New code should call bridge functions directly and let exceptions propagate.

## Post-write-uncertain handling (CP5)

When `write_file_raw` raises `ObsidianPostWriteUncertain` (a PUT timed out client-side after the body was sent), the gateway invokes `work_buddy.obsidian.post_write_verify.verify_post_write` which reads the file from filesystem to decide whether the write actually landed. The `(path, content_hint, write_mode)` carrier on the exception drives the verification:

- `write_mode='replace'`: full sha256 match (content_hint is `'sha256:<hex>'`).
- `write_mode='insert'` or `'append'`: substring witness (content_hint is the unique inserted fragment).

Callers that do section-aware inserts (e.g. `vault_write_at_location`) should pass `write_mode='insert'` and `content_hint=<inserted text>` to vault_write so the verifier can use cheap substring matching instead of full-file hashing.

## EditorConflict (now ObsidianEditorConflict)

Both helpers route through `bridge.write_file_raw` when the bridge is up. That function raises `ObsidianEditorConflict` **immediately** on the first `409` from the plugin's pre-flight dirty-editor check. There is no in-bridge retry: retrying the same payload bytes after the user's typing auto-saves to disk would silently clobber those saved keystrokes. Re-reading + re-computing the payload is the *caller's* job — in practice, the gateway's retry queue (`architecture/retry-queue`).

Capabilities with `retry_policy="verify_first"` or `"replay"` auto-enqueue on transient errors; the sidecar sweep re-invokes the whole capability from scratch on adaptive backoff (10 / 20 / 45 / 90 / 120s), so each attempt reads the file fresh. `vault_write_at_location`, `journal_write`, and the task mutation family all carry `verify_first` for this reason. Out-of-band callers that don't dispatch through `wb_run` (e.g. the Telegram capture handler in the sidecar) enqueue the same way via `enqueue_capability_for_retry` (see `architecture/retry-queue`).

`vault_write` deliberately does NOT fall back to a direct disk write on `ObsidianEditorConflict` — such a write would still be clobbered the moment the user saves. The conflict signal exists precisely to prevent that.

The legacy `EditorConflict` alias was removed in CP9. Callers must import `ObsidianEditorConflict` directly.

## Decision recipe

1. Does the Obsidian Tasks plugin (or any other plugin with a live cache) own state for this file? → `bridge.write_file_raw`.
2. Otherwise → `vault_writer.vault_write`.
3. Either way: be ready for `ConsentRequired` and the typed `ObsidianError` subclasses. Do not catch + ignore them — they are deliberate signals. The `@bridge_retry` decorator handles retries; the gateway handles classification + enqueue.

## Anti-patterns

- **Falling back to a direct write while Obsidian is running.** Even on a transient bridge flap (startup race / timeout), the note may be open in an editor; a direct disk write diverges the editor buffer from disk and wedges the note with a persistent 409 editor_dirty. Direct-write only when `is_obsidian_running()` is False.
- **Retrying a write with the same payload after the user has been typing.** The whole reason `ObsidianEditorConflict` is raised at the first 409 is that a later retry with the original payload would clobber whatever the user auto-saved in the interim. Retry must go through the gateway / sidecar so each attempt re-computes the payload.
- **Falling back to direct write on `ObsidianEditorConflict`.** The conflict signal exists precisely to prevent the disk-clobbers-editor scenario. Bypassing it re-introduces the bug.
- **Falling back to direct write on `ObsidianPostWriteUncertain`.** Risks overwriting a successful-but-unacknowledged plugin write. Let the gateway's verify path decide.
- **Using `vault_write` for `master-task-list.md`.** The fallback on bridge-down would silently corrupt the Tasks-plugin cache.
- **Re-implementing the section-aware logic in callers.** `vault_writer.write_at_location` already handles section parsing; reach for it before duplicating.
- **Wrapping bridge calls in try/except by default.** The decorator + gateway handle exception flow; per-capability try/except recreates the sprawl that CP1–CP9 removed.
