---
name: Threads — parent-child relationship patterns (decompose / group / singular)
kind: concept
description: 'Three parent-child relationship patterns. Decompose: parent has an action, children FSM-execute, cascade-on-terminal advances parent. Group: umbrella holds N cluster sub-threads with item-level drag-drop reorganization (Chrome / journal / email scans). Singular: umbrella holds N children whose actions render hoisted onto the parent''s card so the user sees one thread with N proposals (inline-capture multi-record path).'
summary: 'Stage 5 introduces a second parent-child pattern. Decompose (Stage 4 default): parent has an action, children FSM-execute, cascade-on-terminal advances parent to DONE. Group (new): parent is a container; items can move between sibling parents (same originating_scrape_id); empty parent auto-DISMISSES. Currently used by Chrome scrape (one parent per Louvain cluster).'
tags:
- grouping
- chrome-scrape
- drag-and-drop
- singular
- inline-capture
aliases:
- group-parent
- group view
- parent_relationship
- drag and drop sub-thread
- move_thread_to_parent
- originating_scrape_id
- sibling group
- singular umbrella
- singular pattern
- render hoist
parents:
- threads
- threads
dev_notes: |-
  ## Data model vs user-visible model

  The singular pattern is render-only: the data model stores umbrella + N children, the user-visible model is one thread with N actions because the render hoists. Children duplicate the captured selection as a ContextItem across siblings — harmless redundancy (cheap, identical content), no correctness implications. If a future per-action redirect feature ever demands a cleaner data model, the umbrella + children can collapse into one Thread with N action_inferred events; the `parent_relationship='singular'` value persists as a documented marker either way.

  ## Per-action redirect — design simplifications

  The redirect path is composed entirely from existing primitives — no new event kinds, no autonomy bypass, no new helper:
  - `awaiting_inference_handler` already reads `data.get('target')` and enqueues only that target. Passing `{'target': 'action'}` on `TRIG_REDIRECTED` gives action-only re-inference for free.
  - `render._latest(events, KIND_ACTION_INFERRED)` already picks the newest event as the active proposal. The prior `action_inferred` is naturally superseded in the rendered view; no `KIND_ACTION_SUPERSEDED` event needed.
  - After action inference fires, the FSM lands at `AWAITING_CONFIRMATION` via `action_review_or_execute`. It never walks back to intent / context — autonomy gates already passed for those.

  ## Consent on Approve clicks

  `_post_thread_action` (and `threads.group._run_child_accept` for cluster Approve-All) wrap `engine.transition` in `consent.user_initiated()` for user-click triggers. The click IS the consent boundary; capabilities invoked via state-entry side effects don't re-prompt. See `notifications/consent` (Canonical wiring: thread-approve clicks) for the policy + trigger allowlist + footgun ("if you bypass `_post_thread_action` your endpoint must add its own `user_initiated` wrapper, or the user gets a `ConsentRequired` re-prompt after they already clicked Approve").

  ## Per-matter routing for inline captures

  `pipelines/inline.py:inline_capture` segments the captured text into matters via the `clarify/text-segmenter` SubCall BEFORE running the verdict, then calls `pipelines/singular.py:spawn_thread_for_matter` once per matter. The text-segmenter biases toward 'one matter' and a coverage check rejects hallucinated boundaries; on segmenter soft-fail or short-text bypass the input is treated as a single matter. One matter → one root thread (flat or singular umbrella, depending on record count). N matters → N independent root threads with no umbrella conflation.
---

Three parent-child relationship patterns coexist. The discriminator is `Thread.parent_relationship` (free-string column on the threads table).

## Decompose (`parent_relationship='decompose'`, the default)

- Parent thread has its own `action_inferred` event.
- Children each carry one ContextItem and one own `action_inferred`; FSM-execute independently.
- Cascade-on-terminal: when every child is terminal, parent advances to DONE via `cascade_terminal_to_parent` (`work_buddy/threads/decompose.py`).
- Used when an agent decides 'this work needs to be broken down' (the `decompose` Standard Action).

## Group (`parent_relationship='group'`)

- Umbrella container; no action of its own. Lands in `MONITORING` immediately on spawn.
- Children carry their items as `context_items` (a tuple of ContextItem rows). Items move between sibling group-parents at item granularity via `threads.group.move_item`.
- 'Sibling' = group-parents sharing the same `originating_scrape_id`.
- Cascade-on-terminal still fires.
- Empty group children do NOT auto-DISMISS; manual X-button delete via `threads.group.delete_group_subthread`.
- Frontend: custom multi-column drag-drop view (`window.renderGroupSubThreads`).
- Used by source-pipeline scrapes: chrome triage, journal backlog, email scan.

## Singular (`parent_relationship='singular'`)

- Umbrella container with no action of its own. Lands in `MONITORING` immediately.
- Each child carries one ContextItem (the captured selection) and one `action_inferred` — same shape as a single record's spawn.
- Items do NOT move between siblings. There's no reorganization to do.
- Render-time hoist: `work_buddy/threads/render.py:build_render_data` detects `parent_relationship == 'singular'` and surfaces each child's actions inline on the parent's `actions` array, augmented with `host_thread_id`, `state` (derived from the child's `fsm_state`), and `settled` (true when state is done/rejected/failed). Settled actions render gray + status badge on the umbrella card; pending first, settled last.
- The frontend's standard render path renders the parent's umbrella card with the hoisted Actions section. The `Sub-threads (N)` section is suppressed.
- Cascade-on-terminal: when every child is terminal, the parent advances based on the children's terminal mix. All children DISMISSED → parent DISMISSED. Any child DONE or HANDED_OFF → parent DONE. Decompose / group umbrellas keep the simple all-terminal → DONE rule. Implemented via `decompose.cascade_terminal_to_parent` setting `all_dismissed_singular` on `TRIG_EXECUTION_DONE`; `engine._default_branch_resolver` routes the `done_when_all_subthreads_terminal` branch to `DISMISSED` when that flag is set.
- Sub-LLM context items: `pipelines/singular.py:_build_subcall_context_items` attaches the deadline-extract and project-picker SubCall outputs to every spawned thread as ContextItems with `source='subcall'`, `type=<subcall_name>`. Children of a singular umbrella inherit the same audit ContextItems alongside the captured selection, so the dashboard and downstream agents can inspect what the sub-LLMs saw without re-running them. Generic across the four spawn shapes (flat / singular umbrella / refusal / dismissed).
- Per-action redirect: hoisted action chips on a singular umbrella's card carry a Redirect button. POST `/api/threads/<host>/redirect_action` with `{feedback}` records a `KIND_ACTION_REDIRECTED` event and transitions the child `AWAITING_CONFIRMATION → AWAITING_INFERENCE` with `data={'target': 'action'}`, so the inference worker enqueues only action-layer inference (no walk back through intent / context). The bootstrap inference runner's `_build_redirect_feedback_block` surfaces unresolved redirect feedback onto the LLM prompt; resolved feedback (a newer `action_inferred` already landed) is skipped. `render._latest` picks the newest `action_inferred` as the active proposal; the prior one stays in event history for audit.
- Used by the inline-capture multi-record path (`work_buddy/pipelines/inline.py:_spawn_inline_umbrella`): when a right-click selection's verdict produces 2+ actionable records, the umbrella is spawned with `parent_relationship='singular'`. The user sees ONE thread with N actions on the dashboard.
- Future consumers: per-message email triage (when built) reuses the same pattern.

## Choosing the pattern

- One matter, the agent self-decides to fan out → decompose.
- Many items, organized into clusters with item-level drag-drop → group.
- One matter, multiple proposed actions on it → singular (render hoist makes it look like one thread).
- Multiple separate matters → N flat threads (no umbrella). The text-segmenter SubCall (`clarify/text-segmenter`) is the upstream filter that detects multi-matter captures so they don't conflate into one singular umbrella.

## Backend module map

- `work_buddy/threads/group.py` — `group_thread`, `move_item`, `delete_group_subthread`, `cascade_approve_umbrella`.
- `work_buddy/threads/decompose.py` — `cascade_terminal_to_parent` (used by all three patterns).
- `work_buddy/threads/render.py` — `build_render_data`; the singular-hoist branch is here.
- `work_buddy/threads/execution_runner.py` — EXECUTING state-entry handler.
- `work_buddy/pipelines/runner.py` — source-pipeline driver (group umbrellas).
- `work_buddy/pipelines/inline.py` — inline-capture pipeline (singular umbrellas).
- `work_buddy/pipelines/singular.py` — `spawn_thread_for_matter` per-matter spawn primitive (source-agnostic).

## Frontend

Group umbrellas: dispatcher in `scripts/tabs/threads/main.py`'s `renderThreadDetail` checks `thread.parent_relationship === 'group'` and renders the multi-column group view (`scripts/tabs/threads/group.py`). Drag-and-drop columns, action chips with dropdowns, `cascade_approve_umbrella`.

Singular umbrellas: standard render layout applies, but `_renderActionsSection` displays the hoisted children's actions on the parent's card and `_renderSubThreadsSection` is suppressed. Whole-card click — the action card itself acts as a button (`role='button'`, onclick → `threadsPushPath(host_thread_id)`, onkeydown for Enter/Space) so the user can navigate to the child's full thread page where Approve / Edit / Redirect / Reject all available; inner buttons stop propagation so they don't double-fire. Settled actions render gray + with a status badge (done / rejected / failed / executing). Inline Redirect button per pending action invokes `threadCardRedirectAction(hostThreadId)`.
