"""Inline-capture pipeline.

User-initiated single-selection path: when the user right-clicks in
Obsidian and picks "Send to agent" (see
``work_buddy.inline.handlers.send_to_agent``), one selection comes
through. This module turns that one selection into ZERO OR MORE
Threads via the existing Slice 3 Clarify multi-record verdict +
deadline pre-pass.

Why not a SourcePipeline? The SourcePipeline shape (chrome / journal /
email) is "scan a backlog → cluster → spawn one Thread per cluster."
Inline-capture is "one input → multi-record verdict → 1+ Threads
per record." Different shape, sibling module.

End-to-end flow
---------------

1. Build a single :class:`TriageItem` from the selection.
2. Run the deadline pre-pass (local-first tier_chain via
   ``triage.deadline_extract.tier_chain``) to extract
   ``has_deadline``, ``deadline_date``, ``has_dependency``,
   ``dependency_hint``. Failure-tolerant: produces an all-false
   sentinel that the verdict pass tolerates.
3. Build the user-context block (active tasks/contracts/projects/
   recent commits) via :func:`work_buddy.clarify.recommend.build_triage_context`.
4. Call the multi-record verdict LLM against
   :data:`MULTI_RECORD_VERDICT_SCHEMA`. The verdict can return:
   - ``records: list[Record]`` — N proposed actions, OR
   - ``refusal: {question, missing_context}`` — agent declined; the
     selection becomes a single Thread in
     ``AWAITING_*_CLARIFICATION``.
5. For each emitted record, spawn a Thread:
   - ``task`` records → standard action ``task_create`` with the
     task_proposal as parameters; Thread → ``AWAITING_CONFIRMATION``.
   - ``reference`` records → ``kind="suggestion"`` action with the
     reference_proposal as parameters; Thread → ``AWAITING_CONFIRMATION``.
     (Slice 6 of the legacy roadmap will wire a real reference-filing
     capability; until then the suggestion is read-only on the card.)
   - ``calendar_only`` records → ``kind="suggestion"`` action with the
     calendar_proposal as parameters; Thread → ``AWAITING_CONFIRMATION``.
   - ``delete`` records → no Thread; counted as dropped in the result.
6. If exactly one actionable record → standalone Thread.
   If 2+ actionable records → umbrella + decompose into N children.
   If 0 actionable records (all-delete or refusal-with-zero-records)
   → spawn a single dismissed Thread that records the agent's
     decision in audit.

Bypassing the inference loop
----------------------------

The standard FSM flow is PROPOSED → AWAITING_INFERENCE → INFERRING_*
→ AWAITING_CONFIRMATION. We already have the verdict (intent + action),
so we skip the inference round-trip and stamp the synthetic events
directly — same trick the unified runner uses for pipeline-spawned
group children (see ``runner._initialize_group_child_state``).
"""

from __future__ import annotations

import logging
from datetime import date as _date_cls
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def inline_capture(
    *,
    file_path: str,
    selection: str = "",
    paragraph: str = "",
    cursor_line: int = 0,
    hint: str = "",
    enrich: bool = True,
    tier_chain: list[str] | None = None,
) -> dict[str, Any]:
    """Run the Clarify pipeline on one inline selection and spawn Threads.

    Args:
        file_path: Vault-relative source path (for provenance).
        selection: The user's literal selection. Falls back to
            ``paragraph`` when empty.
        paragraph: Surrounding paragraph (used when selection is
            empty).
        cursor_line: 0-indexed cursor line in the source file.
        hint: Optional user-typed intent hint from the modal.
        enrich: Include the user-context packet (active tasks /
            contracts / projects / recent commits). Default True.
        tier_chain: Optional override for the verdict pass tier walk.
            Defaults to the runtime config's
            ``triage.refine_clusters.tier_chain`` — local-first.

    Returns:
        A dict summarising what was spawned::

            {
              "status": "ok" | "no_records" | "refusal" | "error",
              "umbrella_id": str | None,
              "child_thread_ids": [str, ...],
              "single_thread_id": str | None,
              "dropped_count": int,
              "verdict": dict | None,
              "deadline_hints": dict | None,
              "error": str | None,
            }
    """
    from work_buddy.clarify.adapters.inline import collect_inline_selection
    from work_buddy.clarify.deadline_extract import (
        extract_deadline_hints,
        merge_hints_into_records,
    )

    # ---- 1. Build the TriageItem --------------------------------------
    items, _ch = collect_inline_selection(
        file_path=file_path,
        selection=selection,
        paragraph=paragraph,
        cursor_line=cursor_line,
        hint=hint,
    )
    if not items:
        return {
            "status": "error",
            "error": "collect_inline_selection produced no items",
            "umbrella_id": None,
            "child_thread_ids": [],
            "single_thread_id": None,
            "dropped_count": 0,
            "verdict": None,
            "deadline_hints": None,
        }
    item = items[0]

    # ---- 2. Deadline pre-pass -----------------------------------------
    deadline_hints = extract_deadline_hints(
        item.text or "",
        message_date=_date_cls.today(),
        item_id=item.id,
    )

    # ---- 3. User context ----------------------------------------------
    triage_context: dict[str, Any] = {}
    if enrich:
        try:
            from work_buddy.clarify.recommend import build_triage_context
            triage_context = build_triage_context()
        except Exception as exc:
            logger.warning(
                "inline_capture: build_triage_context failed: %s; "
                "proceeding without",
                exc,
            )

    # ---- 4. Multi-record verdict --------------------------------------
    verdict, verdict_error = _call_multi_record_verdict(
        item=item,
        deadline_hints=deadline_hints,
        triage_context=triage_context,
        tier_chain=tier_chain,
    )

    if verdict_error is not None:
        return {
            "status": "error",
            "error": verdict_error,
            "umbrella_id": None,
            "child_thread_ids": [],
            "single_thread_id": None,
            "dropped_count": 0,
            "verdict": None,
            "deadline_hints": deadline_hints,
        }

    # Refusal: agent declined to commit; spawn one Thread in
    # AWAITING_*_CLARIFICATION carrying the question.
    refusal = (verdict or {}).get("refusal")
    if isinstance(refusal, dict) and refusal.get("question"):
        single_id = _spawn_refusal_thread(
            item=item,
            verdict=verdict,
            deadline_hints=deadline_hints,
        )
        return {
            "status": "refusal",
            "umbrella_id": None,
            "child_thread_ids": [],
            "single_thread_id": single_id,
            "dropped_count": 0,
            "verdict": verdict,
            "deadline_hints": deadline_hints,
        }

    records = list(verdict.get("records") or [])
    if records:
        records = merge_hints_into_records(records, deadline_hints) or records

    actionable = [
        r for r in records
        if isinstance(r, dict) and r.get("destination") != "delete"
    ]
    dropped = [
        r for r in records
        if isinstance(r, dict) and r.get("destination") == "delete"
    ]

    # ---- 5/6. Spawn Threads ------------------------------------------
    if not actionable:
        single_id = _spawn_dismissed_thread(
            item=item, verdict=verdict, dropped=dropped,
        )
        return {
            "status": "no_records" if not dropped else "ok",
            "umbrella_id": None,
            "child_thread_ids": [],
            "single_thread_id": single_id,
            "dropped_count": len(dropped),
            "verdict": verdict,
            "deadline_hints": deadline_hints,
        }

    if len(actionable) == 1:
        # Skip the umbrella for a single record — degenerate case
        # where the umbrella would just be a redirect to the one child.
        single_id = _spawn_record_thread(
            item=item,
            record=actionable[0],
            verdict=verdict,
            parent_id=None,
        )
        return {
            "status": "ok",
            "umbrella_id": None,
            "child_thread_ids": [],
            "single_thread_id": single_id,
            "dropped_count": len(dropped),
            "verdict": verdict,
            "deadline_hints": deadline_hints,
        }

    # 2+ actionable records: spawn umbrella + N children.
    umbrella_id = _spawn_inline_umbrella(item=item, verdict=verdict)
    child_ids: list[str] = []
    for rec in actionable:
        cid = _spawn_record_thread(
            item=item, record=rec, verdict=verdict, parent_id=umbrella_id,
        )
        if cid:
            child_ids.append(cid)

    return {
        "status": "ok",
        "umbrella_id": umbrella_id,
        "child_thread_ids": child_ids,
        "single_thread_id": None,
        "dropped_count": len(dropped),
        "verdict": verdict,
        "deadline_hints": deadline_hints,
    }


# ---------------------------------------------------------------------------
# Verdict call
# ---------------------------------------------------------------------------


def _call_multi_record_verdict(
    *,
    item: Any,
    deadline_hints: dict[str, Any],
    triage_context: dict[str, Any],
    tier_chain: list[str] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Call the multi-record verdict LLM.

    Returns ``(verdict_dict, error_string)``. Exactly one of the two
    is non-None on return.

    Walks ``tier_chain`` (defaults to the same chain used by
    ``refine_clusters`` — local-first via
    ``triage.refine_clusters.tier_chain``). Bypasses the legacy
    ``call_for_verdict`` wrapper because the legacy wrapper has its
    own retry semantics that don't compose with our chain walk.
    """
    from work_buddy.clarify.recommend import render_triage_context_block
    from work_buddy.clarify.verdict_schema import MULTI_RECORD_VERDICT_SCHEMA
    from work_buddy.llm import LLMRunner, ModelTier

    chain = list(tier_chain) if tier_chain is not None else _resolve_verdict_tier_chain()
    if not chain:
        return None, "empty tier_chain"

    user_prompt = _render_item_prompt(
        item=item,
        triage_context=triage_context,
        deadline_hints=deadline_hints,
    )

    runner = LLMRunner()
    last_err: str | None = None
    for tier_str in chain:
        try:
            tier_enum = ModelTier(tier_str)
        except ValueError:
            logger.warning(
                "inline_capture: unknown tier %r in chain; skipping",
                tier_str,
            )
            continue
        try:
            resp = runner.call(
                tier=tier_enum,
                system=_VERDICT_SYSTEM_PROMPT,
                user=user_prompt,
                output_schema=MULTI_RECORD_VERDICT_SCHEMA,
                max_tokens=4096,
                temperature=0.2,
                cache_ttl_minutes=0,
                trace_id=f"inline_capture:{item.id}",
            )
        except Exception as exc:
            logger.warning(
                "inline_capture: runner.call threw at tier=%s: %s; "
                "trying next tier",
                tier_str, exc,
            )
            last_err = str(exc)
            continue

        if resp.is_error():
            logger.info(
                "inline_capture: %s tier=%s — %s; trying next tier",
                resp.error_kind, resp.tier_used, resp.error,
            )
            last_err = resp.error or str(resp.error_kind)
            continue

        verdict = resp.structured_output
        if not isinstance(verdict, dict):
            last_err = "no_structured_output"
            continue

        # Minimal validation — at least one of ``records`` (list) or
        # ``refusal`` (dict with ``question``) must be present.
        has_records = isinstance(verdict.get("records"), list)
        has_refusal = (
            isinstance(verdict.get("refusal"), dict)
            and verdict["refusal"].get("question")
        )
        if not has_records and not has_refusal:
            logger.info(
                "inline_capture: verdict at tier=%s has neither records "
                "nor refusal; trying next tier",
                tier_str,
            )
            last_err = "verdict_missing_records_and_refusal"
            continue

        return verdict, None

    return None, f"all tiers exhausted (last_err={last_err!r})"


def _resolve_verdict_tier_chain() -> list[str]:
    """Return the verdict-pass tier chain.

    Reuses ``triage.refine_clusters.tier_chain`` since the inline
    verdict is the same kind of structured-output classification call.
    Tests can pass an explicit ``tier_chain`` to override.
    """
    try:
        from work_buddy.clarify.config import load_triage_config

        cfg = load_triage_config() or {}
    except Exception as exc:
        logger.warning(
            "inline_capture: load_triage_config failed (%s); using defaults",
            exc,
        )
        from work_buddy.clarify.config import TRIAGE_DEFAULTS

        cfg = TRIAGE_DEFAULTS
    rc = cfg.get("refine_clusters") or {}
    chain = rc.get("tier_chain") or []
    if not isinstance(chain, list):
        return []
    return [str(t) for t in chain if isinstance(t, str)]


# ---------------------------------------------------------------------------
# Verdict prompt
# ---------------------------------------------------------------------------


_VERDICT_SYSTEM_PROMPT = """\
You are running the Clarify step on one selection a user sent from
Obsidian (right-click "Send to agent" or capture-tag). Given the
selection, an optional user hint, the user's current work context
(active tasks, contracts, projects, recent commits), and pre-extracted
deadline / dependency hints, produce a verdict in the multi-record
schema.

A captured selection produces ZERO OR MORE records. Each record has a
``destination`` (one of: ``task``, ``reference``, ``calendar_only``,
``delete``) and a destination-specific proposal.

## Destination guide

- ``task`` — actionable work. Populate ``task_proposal`` with the
  required ``suggested_task_text`` (concise, scannable). Use
  ``user_involvement="high"`` and
  ``creation_provenance="inline-inferred"`` since the user explicitly
  sent this. When ``has_deadline`` or ``has_dependency`` are set in
  the pre-extracted hints, copy them into the task_proposal.
- ``reference`` — knowledge worth filing but not actionable. Populate
  ``reference_proposal.summary`` with a short noun-phrase description.
- ``calendar_only`` — a date-anchored event the user wants on their
  calendar. Populate ``calendar_proposal.title`` plus ``datetime`` /
  ``duration_minutes`` / ``all_day`` when known.
- ``delete`` — selection had no signal worth keeping (test pings,
  stray fragments, etc.). Populate ``delete_reason`` with one
  sentence explaining why.

## When to refuse

If the selection is too ambiguous to commit to any record at all,
populate ``refusal.question`` with a one-sentence question to the
user, plus ``missing_context`` listing the dimensions that would
unblock you (e.g., ``["project", "deadline"]``).

## When N > 1

A single sentence can yield multiple records — e.g. "Buy gift for
Sarah's birthday May 12" → one task (``task_proposal``: "Buy gift for
Sarah") + one calendar entry (``calendar_proposal``: birthday May 12).
Use ``linked_record_indexes`` to associate them.

Always populate ``rationale`` (one short sentence per record OR one
overall sentence) and ``group_intent`` (≤8 words naming the
underlying intent — used as the umbrella thread title when N > 1).
"""


def _render_item_prompt(
    *,
    item: Any,
    triage_context: dict[str, Any],
    deadline_hints: dict[str, Any] | None = None,
) -> str:
    """Compose the verdict user prompt with file + hint + user context + hints."""
    from work_buddy.clarify.recommend import render_triage_context_block

    meta = item.metadata or {}
    file_path = meta.get("file_path", "") or "(unknown)"
    cursor_line = meta.get("cursor_line", 0)
    hint = meta.get("hint", "") or "(none)"

    context_block = render_triage_context_block(triage_context) if triage_context else ""
    context_block = f"\n\n{context_block}\n" if context_block else ""

    hints_block = ""
    if deadline_hints:
        if deadline_hints.get("hint_extraction_failed"):
            hints_block = (
                "\nDeadline hints: extraction failed; rely on the selection "
                "text itself.\n"
            )
        elif (
            deadline_hints.get("has_deadline")
            or deadline_hints.get("has_dependency")
        ):
            parts = []
            if deadline_hints.get("has_deadline"):
                d = deadline_hints.get("deadline_date") or "(date unspecified)"
                parts.append(f"deadline: {d}")
            if deadline_hints.get("has_dependency"):
                dep = deadline_hints.get("dependency_hint") or "(dependency unspecified)"
                parts.append(f"dependency: {dep}")
            hints_block = f"\nDeadline hints (pre-extracted): {'; '.join(parts)}\n"
        else:
            hints_block = "\nDeadline hints: none detected.\n"

    return (
        f"Item id: {item.id}\n"
        f"File: {file_path}:{cursor_line}\n"
        f"Hint: {hint}\n"
        f"{hints_block}"
        f"\n--- Selection ---\n"
        f"{(item.text or '').strip()}\n"
        f"--- End ---"
        f"{context_block}"
    )


# ---------------------------------------------------------------------------
# Thread spawn helpers
# ---------------------------------------------------------------------------


def _selection_to_context_item(item: Any):
    """Convert the inline TriageItem into a ContextItem for Thread storage."""
    from work_buddy.threads.models import ContextItem

    return ContextItem(
        id=item.id,
        source="inline_selection",
        type="selection",
        label=item.label or item.id,
        payload={
            "text": item.text or "",
            "file_path": (item.metadata or {}).get("file_path"),
            "cursor_line": (item.metadata or {}).get("cursor_line"),
            "hint": (item.metadata or {}).get("hint"),
        },
    )


def _spawn_inline_umbrella(*, item: Any, verdict: dict[str, Any]) -> str | None:
    """Spawn the umbrella Thread for a multi-record inline capture.

    Mirrors ``runner._spawn_umbrella`` for the SourcePipeline path but
    inline-shaped — single ContextItem, single inciting summary
    sourced from the captured selection rather than a backlog.
    """
    from work_buddy.threads import store
    from work_buddy.threads.autonomy import default_spawn_policy
    from work_buddy.threads.enums import FSMState
    from work_buddy.threads.events import (
        ACTOR_INCITING,
        KIND_INCITING_EVENT,
        KIND_THREAD_CREATED,
        ThreadEvent,
    )
    from work_buddy.threads.models import Thread

    group_intent = (verdict.get("group_intent") or "").strip() or "Inline capture"
    summary = {
        "source": "inline_capture",
        "title": group_intent,
        "description": group_intent,
        "captured_text": (item.text or "")[:500],
        "file_path": (item.metadata or {}).get("file_path"),
        "cursor_line": (item.metadata or {}).get("cursor_line"),
        "record_count": len(verdict.get("records") or []),
    }

    try:
        umbrella = Thread(
            fsm_state=FSMState.MONITORING,
            inciting_event_summary=summary,
            autonomy_policy=default_spawn_policy(),
            context_items=(_selection_to_context_item(item),),
        )
        store.insert_thread(umbrella)

        e1 = store.append_event(ThreadEvent(
            thread_id=umbrella.thread_id,
            kind=KIND_INCITING_EVENT,
            actor=ACTOR_INCITING,
            data=summary,
        ))
        store.append_event(ThreadEvent(
            thread_id=umbrella.thread_id,
            kind=KIND_THREAD_CREATED,
            actor=ACTOR_INCITING,
            data={"source_pipeline": "inline_capture"},
            parent_event_id=e1.id,
        ))
        store.update_thread_state(
            umbrella.thread_id,
            parent_event_id=store.latest_event_id(umbrella.thread_id),
        )
        return umbrella.thread_id
    except Exception as exc:
        logger.warning(
            "inline_capture: umbrella spawn failed: %s — falling back "
            "to standalone children with no umbrella anchor",
            exc,
        )
        return None


def _spawn_record_thread(
    *,
    item: Any,
    record: dict[str, Any],
    verdict: dict[str, Any],
    parent_id: str | None,
) -> str | None:
    """Spawn one Thread carrying ``record`` as its proposed action.

    The Thread:
    - Has the captured selection as its single ContextItem.
    - Records a synthetic ``intent_inferred`` event using the verdict's
      ``group_intent``.
    - Records a synthetic ``action_inferred`` event whose payload
      maps the record's destination + proposal into the action shape
      ``threads/render`` understands.
    - Lands directly in :data:`FSMState.AWAITING_CONFIRMATION`,
      bypassing the inference loop (we already have the verdict).
    """
    from work_buddy.threads import store
    from work_buddy.threads.autonomy import default_spawn_policy
    from work_buddy.threads.enums import FSMState
    from work_buddy.threads.events import (
        ACTOR_FSM_ENGINE,
        ACTOR_INCITING,
        KIND_ACTION_INFERRED,
        KIND_INCITING_EVENT,
        KIND_INTENT_INFERRED,
        KIND_STATE_TRANSITION,
        KIND_THREAD_CREATED,
        ThreadEvent,
    )
    from work_buddy.threads.models import Thread

    group_intent = (verdict.get("group_intent") or "").strip() or "Inline capture"
    rationale = (verdict.get("rationale") or "").strip()
    destination = record.get("destination") or "task"

    # Destination-specific action payload + thread title.
    action_payload, thread_title = _action_payload_for_record(
        record=record, item=item, group_intent=group_intent, rationale=rationale,
    )
    if action_payload is None:
        return None

    summary = {
        "source": "inline_capture",
        "title": thread_title,
        "description": rationale or thread_title,
        "destination": destination,
        "captured_text": (item.text or "")[:500],
        "file_path": (item.metadata or {}).get("file_path"),
        "cursor_line": (item.metadata or {}).get("cursor_line"),
    }

    try:
        thread = Thread(
            parent_id=parent_id,
            fsm_state=FSMState.PROPOSED,  # transitions to AWAITING_CONFIRMATION below
            inciting_event_summary=summary,
            autonomy_policy=default_spawn_policy(),
            context_items=(_selection_to_context_item(item),),
        )
        store.insert_thread(thread)

        e1 = store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_INCITING_EVENT,
            actor=ACTOR_INCITING,
            data=summary,
        ))
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_THREAD_CREATED,
            actor=ACTOR_INCITING,
            data={"source_pipeline": "inline_capture"},
            parent_event_id=e1.id,
        ))

        # Synthetic intent_inferred — group_intent is the intent.
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_INTENT_INFERRED,
            actor=ACTOR_INCITING,
            data={
                "target": "intent",
                "payload": {"intent": group_intent},
                "confidence": 1.0,
                "tier_used": None,
                "model_used": None,
                "synthetic": True,
                "from_inline_verdict": True,
            },
            parent_event_id=store.latest_event_id(thread.thread_id),
        ))

        # Synthetic action_inferred — the record's proposal.
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor=ACTOR_INCITING,
            data={
                "target": "action",
                "payload": action_payload,
                "confidence": _confidence_for_record(record),
                "tier_used": None,
                "model_used": None,
                "synthetic": True,
                "from_inline_verdict": True,
            },
            parent_event_id=store.latest_event_id(thread.thread_id),
        ))

        # Direct state-cache update to AWAITING_CONFIRMATION + audit.
        store.update_thread_state(
            thread.thread_id,
            fsm_state=FSMState.AWAITING_CONFIRMATION.value,
            parent_event_id=store.latest_event_id(thread.thread_id),
        )
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_STATE_TRANSITION,
            actor=ACTOR_FSM_ENGINE,
            data={
                "from": FSMState.PROPOSED.value,
                "to": FSMState.AWAITING_CONFIRMATION.value,
                "reason": "inline_capture_spawn",
            },
            parent_event_id=store.latest_event_id(thread.thread_id),
        ))
        store.update_thread_state(
            thread.thread_id,
            parent_event_id=store.latest_event_id(thread.thread_id),
        )
        return thread.thread_id
    except Exception as exc:
        logger.warning(
            "inline_capture: record-thread spawn failed for "
            "destination=%s: %s",
            destination, exc,
        )
        return None


def _spawn_refusal_thread(
    *, item: Any, verdict: dict[str, Any], deadline_hints: dict[str, Any],
) -> str | None:
    """Spawn one Thread when the verdict carries a refusal.

    Thread lands in :data:`FSMState.AWAITING_INTENT_CLARIFICATION` so
    the user resolves the agent's open question via the standard
    clarification card. The refusal payload (question + missing
    context) goes on the inciting summary so the renderer can read it.
    """
    from work_buddy.threads import store
    from work_buddy.threads.autonomy import default_spawn_policy
    from work_buddy.threads.enums import FSMState
    from work_buddy.threads.events import (
        ACTOR_FSM_ENGINE,
        ACTOR_INCITING,
        KIND_INCITING_EVENT,
        KIND_STATE_TRANSITION,
        KIND_THREAD_CREATED,
        ThreadEvent,
    )
    from work_buddy.threads.models import Thread

    refusal = verdict.get("refusal") or {}
    question = refusal.get("question") or ""
    missing = refusal.get("missing_context") or []
    group_intent = (verdict.get("group_intent") or "").strip() or "Inline capture"

    summary = {
        "source": "inline_capture",
        "title": group_intent,
        "description": question,
        "refusal": {"question": question, "missing_context": list(missing)},
        "captured_text": (item.text or "")[:500],
        "file_path": (item.metadata or {}).get("file_path"),
        "cursor_line": (item.metadata or {}).get("cursor_line"),
    }

    try:
        thread = Thread(
            fsm_state=FSMState.PROPOSED,
            inciting_event_summary=summary,
            autonomy_policy=default_spawn_policy(),
            context_items=(_selection_to_context_item(item),),
        )
        store.insert_thread(thread)

        e1 = store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_INCITING_EVENT,
            actor=ACTOR_INCITING,
            data=summary,
        ))
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_THREAD_CREATED,
            actor=ACTOR_INCITING,
            data={"source_pipeline": "inline_capture", "refusal": True},
            parent_event_id=e1.id,
        ))

        # Direct transition to AWAITING_INTENT_CLARIFICATION — the
        # user owes us a clarification before any action proposal can
        # land.
        store.update_thread_state(
            thread.thread_id,
            fsm_state=FSMState.AWAITING_INTENT_CLARIFICATION.value,
            parent_event_id=store.latest_event_id(thread.thread_id),
        )
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_STATE_TRANSITION,
            actor=ACTOR_FSM_ENGINE,
            data={
                "from": FSMState.PROPOSED.value,
                "to": FSMState.AWAITING_INTENT_CLARIFICATION.value,
                "reason": "inline_capture_refusal",
            },
            parent_event_id=store.latest_event_id(thread.thread_id),
        ))
        store.update_thread_state(
            thread.thread_id,
            parent_event_id=store.latest_event_id(thread.thread_id),
        )
        return thread.thread_id
    except Exception as exc:
        logger.warning("inline_capture: refusal-thread spawn failed: %s", exc)
        return None


def _spawn_dismissed_thread(
    *,
    item: Any,
    verdict: dict[str, Any],
    dropped: list[dict[str, Any]],
) -> str | None:
    """Spawn one Thread already in DISMISSED for fully-dropped captures.

    Records the agent's decision (delete reasons) on the inciting
    summary so the user has a trace of "I sent this; the agent looked
    at it and decided it wasn't worth keeping" rather than the capture
    silently disappearing.
    """
    from work_buddy.threads import store
    from work_buddy.threads.autonomy import default_spawn_policy
    from work_buddy.threads.enums import FSMState
    from work_buddy.threads.events import (
        ACTOR_FSM_ENGINE,
        ACTOR_INCITING,
        KIND_INCITING_EVENT,
        KIND_STATE_TRANSITION,
        KIND_THREAD_CREATED,
        KIND_THREAD_DISMISSED,
        ThreadEvent,
    )
    from work_buddy.threads.models import Thread

    group_intent = (verdict.get("group_intent") or "").strip() or "Inline capture (dropped)"
    delete_reasons = [
        (r.get("delete_reason") or "").strip()
        for r in dropped
        if isinstance(r, dict)
    ]
    delete_reasons = [r for r in delete_reasons if r]

    summary = {
        "source": "inline_capture",
        "title": group_intent,
        "description": (delete_reasons[0] if delete_reasons else "Agent recommended dropping the capture."),
        "captured_text": (item.text or "")[:500],
        "file_path": (item.metadata or {}).get("file_path"),
        "cursor_line": (item.metadata or {}).get("cursor_line"),
        "dropped_count": len(dropped),
        "delete_reasons": delete_reasons,
    }

    try:
        thread = Thread(
            fsm_state=FSMState.PROPOSED,
            inciting_event_summary=summary,
            autonomy_policy=default_spawn_policy(),
            context_items=(_selection_to_context_item(item),),
        )
        store.insert_thread(thread)

        e1 = store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_INCITING_EVENT,
            actor=ACTOR_INCITING,
            data=summary,
        ))
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_THREAD_CREATED,
            actor=ACTOR_INCITING,
            data={"source_pipeline": "inline_capture", "dismissed_at_spawn": True},
            parent_event_id=e1.id,
        ))
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_THREAD_DISMISSED,
            actor=ACTOR_INCITING,
            data={"reason": "agent_recommended_drop", "delete_reasons": delete_reasons},
            parent_event_id=store.latest_event_id(thread.thread_id),
        ))

        store.update_thread_state(
            thread.thread_id,
            fsm_state=FSMState.DISMISSED.value,
            parent_event_id=store.latest_event_id(thread.thread_id),
        )
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_STATE_TRANSITION,
            actor=ACTOR_FSM_ENGINE,
            data={
                "from": FSMState.PROPOSED.value,
                "to": FSMState.DISMISSED.value,
                "reason": "inline_capture_all_dropped",
            },
            parent_event_id=store.latest_event_id(thread.thread_id),
        ))
        store.update_thread_state(
            thread.thread_id,
            parent_event_id=store.latest_event_id(thread.thread_id),
        )
        return thread.thread_id
    except Exception as exc:
        logger.warning("inline_capture: dismissed-thread spawn failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Action payload mapping
# ---------------------------------------------------------------------------


def _action_payload_for_record(
    *,
    record: dict[str, Any],
    item: Any,
    group_intent: str,
    rationale: str,
) -> tuple[dict[str, Any] | None, str]:
    """Build the action_inferred payload from a verdict record.

    Returns ``(action_payload, thread_title)``. When the destination
    isn't recognized, returns ``(None, "")`` and the caller skips the
    spawn.

    Three live destinations:
    - ``task``: standard action ``task_create`` with task_proposal as
      parameters; the registry already exposes ``task_create`` as a
      first-class capability.
    - ``reference``: kind=suggestion. No registered capability today
      (Slice 6 of the legacy roadmap was to add reference filing);
      until that lands, the action surfaces as a free-form suggestion
      the user can manually act on.
    - ``calendar_only``: kind=suggestion likewise (Slice 10 territory).
    """
    destination = record.get("destination")

    if destination == "task":
        proposal = record.get("task_proposal") or {}
        task_text = (
            proposal.get("suggested_task_text")
            or group_intent
            or "Inline-captured task"
        )[:120]
        # Build parameters dict for task_create. We pass the fields
        # the capability accepts; unknown / forward-compat fields stay
        # in the payload for the audit trail but don't go into params.
        parameters = {
            "task_text": task_text,
            "summary": proposal.get("definition_of_done")
            or proposal.get("outcome_text")
            or None,
            "urgency": "medium",
            "creation_provenance": "inline-inferred",
            "user_involvement": "high",
            "has_deadline": bool(proposal.get("has_deadline")),
            "deadline_date": proposal.get("deadline_date"),
            "has_dependency": bool(proposal.get("has_dependency")),
            "dependency_hint": proposal.get("dependency_hint"),
        }
        action_payload = {
            "kind": "standard",
            "name": "task_create",
            "parameters": {k: v for k, v in parameters.items() if v not in (None, "")},
            "rationale": rationale or proposal.get("rationale") or "",
            "plan_summary": _plan_summary_for_task(proposal, task_text),
            "irreversibility": "low",
            "regret_potential": "low",
            "risk_amplifier": False,
            "intrinsic_amplifiers": {},
        }
        return action_payload, task_text

    if destination == "reference":
        proposal = record.get("reference_proposal") or {}
        ref_summary = (proposal.get("summary") or group_intent)[:120]
        action_payload = {
            "kind": "suggestion",
            "name": "reference_capture_suggested",
            "parameters": {
                "summary": ref_summary,
                "suggested_path": proposal.get("suggested_path"),
            },
            "rationale": rationale or "",
            "plan_summary": (
                f"File this as a reference: {ref_summary}"
                + (f" → {proposal.get('suggested_path')}" if proposal.get("suggested_path") else "")
            ),
            "blocked_on": "no reference-capture capability yet (Slice 6 territory)",
            "irreversibility": "low",
            "regret_potential": "low",
            "risk_amplifier": False,
        }
        return action_payload, ref_summary

    if destination == "calendar_only":
        proposal = record.get("calendar_proposal") or {}
        title = (proposal.get("title") or group_intent)[:120]
        action_payload = {
            "kind": "suggestion",
            "name": "calendar_event_suggested",
            "parameters": {
                "title": title,
                "datetime": proposal.get("datetime"),
                "duration_minutes": proposal.get("duration_minutes"),
                "all_day": bool(proposal.get("all_day")),
            },
            "rationale": rationale or "",
            "plan_summary": (
                f"Add to calendar: {title}"
                + (f" @ {proposal.get('datetime')}" if proposal.get("datetime") else "")
            ),
            "blocked_on": "no calendar capability yet (Slice 10 territory)",
            "irreversibility": "low",
            "regret_potential": "low",
            "risk_amplifier": False,
        }
        return action_payload, title

    # Unknown destination — caller skips.
    logger.warning(
        "inline_capture: unknown record destination %r; skipping spawn",
        destination,
    )
    return None, ""


def _plan_summary_for_task(proposal: dict[str, Any], task_text: str) -> str:
    """Compose a plan_summary line for the consent card rendering."""
    bits = [f'Create task "{task_text}"']
    if proposal.get("has_deadline") and proposal.get("deadline_date"):
        bits.append(f"due {proposal['deadline_date']}")
    elif proposal.get("has_deadline"):
        bits.append("with a deadline (date unspecified)")
    if proposal.get("has_dependency") and proposal.get("dependency_hint"):
        bits.append(f"after: {proposal['dependency_hint']}")
    if proposal.get("kind") and proposal.get("kind") != "task":
        bits.append(f"({proposal['kind']})")
    return " — ".join(bits)


def _confidence_for_record(record: dict[str, Any]) -> float:
    """Best-effort confidence pulled from the record's risk_profile.

    The task_proposal sub-schema carries ``risk_profile.inference_uncertainty``
    (low/medium/high); inverting that into a confidence number is a
    crude mapping but lets the consent card surface a sensible
    confidence pill. Default 0.5 when the field is missing.
    """
    proposal = record.get("task_proposal") or {}
    risk = proposal.get("risk_profile") or {}
    if not isinstance(risk, dict):
        return 0.5
    inv = (risk.get("inference_uncertainty") or "").lower()
    return {"low": 0.85, "medium": 0.6, "high": 0.35}.get(inv, 0.5)
