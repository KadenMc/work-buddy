# t-554b574d — Decomposed Sub-LLM Calls + Singular Pattern: Decisions

Branch: `feat/decomposed-llm-judgment`
Plan source: `~/.claude/plans/t-554b574d-wb-dev-effervescent-toast.md`

This document records **what shipped**, **what changed during build**, and **what was deferred with intent**. It complements the plan file, which described intent up-front; this records ground truth.

## Stages shipped

| Stage | Commit | What |
|---|---|---|
| Part 1 — Decomposed-judgment framework | `eb5e3c0d` | `work_buddy/llm/decomposed.py`: `SubCall`, `MainCall`, `DecomposedJudgment`, `run_subcall`. Frozen-dataclass declaration; config-driven dials (tier_chain, max_tokens, temperature, cache_ttl_minutes) under `triage.<name>`. |
| Part 1 — Deadline pre-pass migration | `eb5e3c0d` | `clarify/deadline_extract.py`: `DEADLINE_HINTS_SUBCALL`. Tier change FRONTIER_FAST → LOCAL_FAST first lives in `TRIAGE_DEFAULTS["deadline_extract"]`, not code. |
| Part 1 — Project picker | `051b174e` | `clarify/project_picker.py`: hedged ranked candidates, `null` first-class. Verdict schema gains `project_tag` + `project_candidates` audit field. No Python thresholds — verdict LLM decides. |
| Part 1 — Misc fixes | `8b31f033`, `5fcc544e`, `ae94cad5` | Strict-schema + triage-config-key routing fixes surfaced in live testing. |
| Part 1 — Inline perf | `989f4369`, `ed3006d2` | Trim verdict context (drop redundant project block; IR-rank tasks). Plug pick_projects LLM-call leak in tests. |
| UX — Umbrella titles | `208002bb` | "Daily journal scan: 2026-05-09" → "Daily note: 2026-05-09". Email triage / inline selection titles tightened similarly. |
| Stage 1 — Singular pattern (render-only) | `875ad71d` | `parent_relationship='singular'` on inline umbrella; render hoists children's actions onto parent card; sub-threads section suppressed. journal_date defaults to today. Whole-card click navigates to child thread. |
| Stage 2 — Text-segmenter + per-matter spawn | `3e2c47d9` | `clarify/text_segmenter.py` SubCall (generic, reusable); `pipelines/singular.py:spawn_thread_for_matter` (source-agnostic primitive). One inline capture → 1+ matters → 1+ independent threads. |
| Phase 3 — Sub-LLM outputs as ContextItems | `585bf664` | Deadline + picker outputs attached to every spawned thread (and child of a singular umbrella) as `source='subcall'` ContextItems. Generic across the four spawn shapes. |
| Phase 4 — Smart DONE/DISMISSED cascade | `d5c2dd0f` | Singular umbrella with all-DISMISSED children → DISMISSED. Any DONE/HANDED_OFF child → DONE. Decompose / group umbrellas unaffected. |
| Phase 5 — Per-action redirect (scoped re-inference) | `abdae019` + `a60c7182` | Hoisted action chip on singular umbrella gets a Redirect button. POST /api/threads/<host>/redirect_action with feedback → records KIND_ACTION_REDIRECTED → transitions to AWAITING_INFERENCE with target=action → bootstrap inference runner reads unresolved redirect feedback and surfaces it on the LLM prompt. render._latest picks the newest action_inferred as active. |
| Implied consent on user clicks | `abdae019` | _post_thread_action and threads.group._run_child_accept wrap engine.transition in `consent.user_initiated()` for user-click triggers (execute, confirmed, etc.). Capabilities invoked via state-entry side effects no longer re-prompt for consent the user already gave by clicking Approve. |
| Empty-umbrella spawn fix | `d5f6f334` | pipelines/runner.py early-returns a no-op PipelineRun when items=[]. Stops the hourly journal-triage cron from spawning empty "Daily note: <date>" umbrellas. |

## Decisions made during build (vs the plan)

### 1. Singular pattern as render-only at v1

**Plan tension:** earlier discussion explored a backend FSM refactor that would store one Thread with N `action_inferred` events.

**Decision:** ship render-only — `parent_relationship='singular'` umbrella + N children, render-time hoist of children's actions onto parent. Data model has known redundancy (selection ContextItem duplicated across siblings); user-visible model is one thread with N actions because of the render hoist. Documented honestly in `threads/grouping`.

**Why:** ships in hours, not days. Redundancy is harmless. If v2 (per-action redirect with scoped re-inference) demands it, we collapse children into one Thread with N events at that point.

### 2. One selection ≠ one matter — split into two stages

**Plan correction:** the user pointed out that a right-click selection's *content* may span multiple unrelated matters, and the singular pattern would conflate them. The render-only fix is correct for one-matter / multi-action; it's wrong for multi-matter.

**Decision:** Stage 1 ships render-only (treats every selection as one matter; the rare multi-matter case renders as one umbrella, less wrong than today's broken UX). Stage 2 adds the upstream `text_segmenter` SubCall to detect matter count and route N matters → N independent threads.

### 3. Generic text-segmenter SubCall, not inline-specific

**User constraint:** "the segmenter shouldn't be tied into just one pipeline."

**Decision:** built `clarify/text_segmenter.py` as a generic SubCall (sibling of `deadline_extract.py`, `project_picker.py`). `pipelines/singular.py:spawn_thread_for_matter` is also source-parameterised (`source="inline"` today, future `source="email_message"` etc.). Future per-message email triage and other singular-input pipelines reuse both primitives without rebuilding.

### 4. `_segment_with_escalation` not migrated

**Plan note:** journal's hand-rolled segmenter has post-parse semantic-validation-driven escalation (line-range coverage, banner-date alignment) that the SubCall framework can't currently express.

**Decision:** journal's segmenter coexists with the new generic one. Migration would require adding a `validate_post_parse` hook to `SubCall`. Future framework extension; not on this branch.

### 5. ContextItems for sub-LLM outputs is generic, not jury-rigged

**User ask (from earlier):** sub-LLM outputs as durable thread context "set up properly such that we could (optionally) have this across any arbitrary thread/sub-thread."

**Decision:** Phase 3 wires this through `pipelines/singular.py:_build_subcall_context_items` once, then threads it through all four spawn helpers (`_spawn_record_thread`, `_spawn_inline_umbrella`, `_spawn_refusal_thread`, `_spawn_dismissed_thread`) via an `extra_context_items` parameter. Children of a singular umbrella inherit the same audit ContextItems. Generic across spawn shapes. Adding a third SubCall to inline triage automatically extends the audit trail with no per-pipeline plumbing.

### 6. Phase 4 implementation: branch resolver, not new trigger

**Decision:** rather than introducing a new FSM trigger (e.g. `TRIG_ALL_DISMISSED`), the existing `TRIG_EXECUTION_DONE` branch resolver `done_when_all_subthreads_terminal` was extended to read `data['all_dismissed_singular']` and route to `DISMISSED` when set. `cascade_terminal_to_parent` decides the flag based on `parent.parent_relationship == 'singular'` AND every child in `FSMState.DISMISSED`. HANDED_OFF children count as progress (parent → DONE).

**Why:** smaller blast radius. Decompose / group umbrellas keep their behavior. Reachable-state set widened to include DISMISSED for that branch label; transition table unchanged otherwise.

### 7. Whole-card click on singular hoisted actions

**User feedback during live test:** "I don't want 'Open thread' buttons — I want the whole thing clickable."

**Decision:** the action card itself becomes a button (`role='button'`, onclick → `threadsPushPath(host_thread_id)`, onkeydown for Enter/Space). Inner Approve/Reject buttons get `event.stopPropagation()` so they don't double-fire. Matches the existing context-card click style.

### 8. Phase 5 design simplifications

The original plan called for:
- A new helper `infer_replacement_action(...)` for action-only inference.
- A new event kind `KIND_ACTION_SUPERSEDED` to mark the prior `action_inferred`.
- An autonomy bypass to skip intent/context gates.

What actually shipped used three observations from the existing code:

- **No new helper needed.** `awaiting_inference_handler` already reads `data.get('target')` and enqueues only that target's inference. Passing `{"target": "action"}` on the existing `TRIG_REDIRECTED` transition gives action-only re-inference without a new entry point.
- **No new event kind needed.** `render._latest(events, KIND_ACTION_INFERRED)` already picks the newest event for surfacing. Recording a new `action_inferred` automatically supersedes the old one in the rendered view. The prior one stays in event history for audit.
- **No autonomy bypass needed.** After action inference fires, the FSM lands at AWAITING_CONFIRMATION via the existing `action_review_or_execute` branch resolver — it never walks back to intent / context.

The new pieces are minimal: an endpoint that emits `KIND_ACTION_REDIRECTED` + fires the transition; a `_build_redirect_feedback_block` helper that surfaces *unresolved* redirect feedback into the LLM prompt (with a "resolved" check: a newer `action_inferred` event id means the feedback already got addressed); a frontend Redirect button. Total backend diff < 100 lines.

### 9. Implied consent on user clicks

Subagent audit found that `consent.user_initiated` already existed with full tests, but was never wired to the dashboard thread-approve path. So clicking Approve on a task_create action chip surfaced a fresh `ConsentRequired` and dumped the thread to AWAITING_REDIRECT — double-prompting consent the user already gave on the confirmation card.

**Decision:** wrap `engine.transition` in `_post_thread_action` and `threads.group._run_child_accept` with `user_initiated()` for the user-click triggers (`execute`, `confirmed`, `review_accepted`, `provided`, `redirected`, `retry_cleanup`, `accept_cleanup_failure`). This counts the dashboard click as the consent boundary; capabilities invoked via state-entry side effects no longer re-prompt. `USER_INITIATED_COVERED` audit entries record what was subsumed.

This came up because Test 2 of Phase 4's live verification hit the consent re-prompt and stalled in AWAITING_REDIRECT — a real bug, not a Phase 4 regression. Fixing the consent wiring unblocks the path.

### 10. Empty-umbrella spawn guard

The hourly journal-triage cron was spawning a fresh "Daily note: <date>" umbrella every fire, even when the journal had zero triageable content. The user accumulated 18+ identical empty threads on the dashboard.

**Decision:** in `pipelines/runner.py:run_pipeline`, early-return a no-op `PipelineRun` when `items=[]`, before `_spawn_umbrella`. The original "operator-visible signal the pipeline ran" goal is served by the pipeline log; dashboard threads are wrong granularity. Cross-fire dedup (skip when a non-dismissed umbrella for the same journal_date exists) is the broader fix, deferred — this commit kills the symptom.

## Deferred (with intent)

### Other deferrals (from the plan, unchanged)

- `refine_clusters` migration to `DecomposedJudgment` — wait for the parallel legacy-clarify migration owner.
- Email verdict refactor — no real sub-calls there yet.
- Parallel sub-call execution — no consumer needs it.
- `validate_post_parse` hook on `SubCall` — would unlock journal segmenter migration. No current consumer demands it.
- Cluster umbrellas hoisting children's actions — speculative; user explicitly said keep cluster drag-drop UX as-is.
- Singular's data-model collapse (single Thread + N events) — deferred until per-action redirect (Phase 5) actually demands it.

## Knowledge units updated

- `architecture/llm-runner/decomposed-judgment` — created in Part 1 (declaration of `SubCall` / `MainCall` / `DecomposedJudgment`, when to reach for this vs raw `LLMRunner.call`).
- `architecture/llm-runner` — declares the new child link.
- `clarify/deadline-extract` — tier change recorded.
- `clarify/project-picker` — created with the hedged-output / null-first-class invariants.
- `clarify/text-segmenter` — created in Stage 2 (generic prose-segmentation SubCall).
- `threads/grouping` — added the third `singular` pattern; updated for Phase 3 (ContextItems) and Phase 4 (cascade refinement); whole-card click documented.

## Verification status

- Unit tests: `tests/unit/threads/` (482 passed), `tests/unit/clarify/` (full pass), `tests/unit/pipelines/test_inline_pipeline.py` + `test_spawn_thread_for_matter.py` (full pass), `tests/unit/threads/test_decompose.py` (24 passed including 5 new singular-cascade tests).
- Pre-existing test failures (`test_knowledge_index`, `test_llm_local_inference`, missing-dep import errors) are unrelated to this branch.
- Live smoke tests run during build:
  1. One-line right-click → flat thread (Stage 1 sanity).
  2. Multi-action capture ("Buy gift for Sarah's birthday May 12") → singular umbrella with two hoisted actions.
  3. Multi-matter capture → split successfully (Stage 2).
- The 18 stale `Daily journal scan: unknown` threads were dismissed during the journal_date fix verification.

## File map (new + heavily modified)

**New:**
- `work_buddy/llm/decomposed.py`
- `work_buddy/clarify/deadline_extract.py` (rewritten on top of SubCall)
- `work_buddy/clarify/project_picker.py`
- `work_buddy/clarify/text_segmenter.py`
- `work_buddy/pipelines/singular.py`

**Heavily modified:**
- `work_buddy/clarify/config.py` (new TRIAGE_DEFAULTS blocks)
- `work_buddy/clarify/verdict_schema.py` (project_tag + project_candidates)
- `work_buddy/clarify/capabilities/inline_triage_scan.py`
- `work_buddy/clarify/capabilities/journal_triage_scan.py`
- `work_buddy/pipelines/inline.py` (refactored into pre-pass + segment + per-matter loop)
- `work_buddy/pipelines/journal.py` (journal_date default)
- `work_buddy/threads/render.py` (singular hoist branch)
- `work_buddy/threads/decompose.py` (smart cascade)
- `work_buddy/threads/engine.py` (branch resolver extension)
- `work_buddy/dashboard/frontend/scripts/tabs/threads/card.py` (action gray-out, status badges, whole-card click, sub-threads suppression)

## Outstanding work for the user / next session

1. ~~**Cross-cron umbrella dedup for journal-triage**~~ — **Shipped.** `SourcePipeline.dedup_key` is now a Protocol-level hook; `JournalBacklogPipeline.dedup_key` returns `journal_backlog:<journal_date>` and the runner short-circuits the spawn when an open MONITORING umbrella carries the same key in its `inciting_event_summary`. Partial-day refresh (route newly-captured items into the existing umbrella instead of skipping entirely) remains the open follow-up if the user wants in-day pickup of new running notes.
2. **Knowledge unit for implied-consent policy** — `architecture/consent/implied-consent` should be authored to document the click-as-consent rule, which triggers participate, the `USER_INITIATED_COVERED` audit, and the risk policy (no current capability is high-blast enough to override the click). Cited in commit `abdae019` but not yet in the store.
3. **Pre-existing test environment hygiene** — `freezegun` / `lmstudio` / `knowledge_index` import errors in unrelated test modules are environment hygiene, not regressions on this branch.
4. **Live verification of Test 2** — with implied consent now wired, the "Approve one + Reject one → DONE" path should run cleanly through to terminal. The previous live test stalled at AWAITING_REDIRECT because of the consent re-prompt; that bug is now fixed.
