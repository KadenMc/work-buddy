---
name: Inline Commands Overview
kind: concept
description: Architecture of the inline command framework — surfaces, dispatcher, handler registration
summary: 'Two surfaces: menu (ephemeral, right-click) and tag (#wb/cmd/*, can be persistent). The primary implemented command is `send-to-agent` — selection + optional hint → Sonnet verdict (via LLMRunner with Opus escalation) → Review pool. Both surfaces route through work_buddy.inline.dispatcher.dispatch. Handlers declare consume_mode and persistence.'
tags:
- inline
- architecture
aliases:
- inline architecture
- inline framework
parents:
- inline
- inline
---

# Inline Commands

Let the user trigger agent actions from inside Obsidian — either by right-clicking on a selection (ephemeral, one-shot) or by typing a `#wb/cmd/*` tag in a note (can be one-shot OR persistent).

## Activation surfaces

| Surface | Trigger | Persistence | Use cases |
|---|---|---|---|
| `menu` | Right-click in editor | None — one-shot | Send selection to agent, ask about this, summarize |
| `tag` | `#wb/cmd/<name>` in a note, detected via `metadataCache.changed` | Per-handler (one-shot OR persistent watcher) | In-document triggers, recurring watchers |

## Implemented commands

- **`send-to-agent`** (primary) — the user's selection + optional hint goes through `work_buddy.pipelines.inline.inline_capture`: a local-first deadline pre-pass extracts `has_deadline` / `deadline_date` / `has_dependency` / `dependency_hint` from the text, the active-work context block is built (tasks / contracts / projects / commits via `architecture/context-pipeline`), and the multi-record verdict LLM (local-first tier_chain) emits zero-or-more typed records (`task` / `reference` / `calendar_only` / `delete`). The pipeline then spawns 1+ Threads carrying the inferred actions; the user resolves them via the dashboard's Threads tab.

The old `task/new` stub is removed — `send-to-agent` absorbs the use case.

## Pipeline

1. Plugin detects activation (`editor-menu` event or `metadataCache.changed`).
2. Plugin POSTs to `http://127.0.0.1:<dashboard_port>/inline/invoke` with `{command, surface, payload}` (the modal may have captured an optional hint string).
3. Dashboard forwards to `work_buddy.inline.dispatcher.dispatch_sync`.
4. Dispatcher looks up the command in the registry, builds an `InlineContext`, and either:
   - Runs the handler and applies the declared consume mode (one-shot, strip/annotate/replace/leave), OR
   - Registers a `PersistentWatcher` row in `inline.db` for a `persistent=True` handler.
5. `send-to-agent` kicks off `pipelines.inline.inline_capture` in a background thread so the plugin POST returns immediately with `{"status": "queued"}`. Threads spawn asynchronously.
6. Sidecar job `inline-sync.md` reconciles vault ↔ watcher store every 10 min.

## Spawn shapes (per `inline_capture` result)

- **1 actionable record** → standalone Thread in `AWAITING_CONFIRMATION`.
- **2+ actionable records** → umbrella Thread (`MONITORING`) + N children, each in `AWAITING_CONFIRMATION` carrying its destination-specific action proposal.
- **0 actionable records (all `delete`)** → single Thread auto-DISMISSED with the agent's drop rationale on its inciting summary (so the capture is auditable rather than silently lost).
- **Refusal** → single Thread in `AWAITING_INTENT_CLARIFICATION` carrying the agent's open question.

## Packages

- `work_buddy/inline/` — Python framework (models, registry, dispatcher, consume, store, sync, handlers).
- `work_buddy/inline/handlers/send_to_agent.py` — the currently-implemented primary command.
- `work_buddy/pipelines/inline.py` — the `inline_capture` entry point (deadline pre-pass + multi-record verdict + Thread spawn).
- `obsidian-work-buddy/src/inlineMenu.ts` + `tagWatcher.ts` — plugin-side detection.
- `sidecar_jobs/inline-sync.md` — reconciliation cron.

## Capabilities

- `inline_invoke` — execute a command (called by dashboard endpoint).
- `inline_list_commands` — list registered commands (optional surface filter).
- `inline_menu_manifest` — menu-shaped list for plugin consumption.
- `inline_tag_removed` — cleanup when a persistent tag is deleted.
- `inline_list_watchers`, `inline_cancel_watcher` — watcher management.
- `inline_sync` — reconcile vault ↔ watcher store.

## Related

- `architecture/llm-runner` — the tier-driven LLM layer `send-to-agent` uses for verdict generation.
- `architecture/context-pipeline` — the context-enrichment layer that feeds the verdict prompt.
- Threads tab on the dashboard — where spawned inline-capture Threads land for resolution.
