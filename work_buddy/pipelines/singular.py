"""Singular-input pipeline primitive.

The "singular" pattern: one *matter* (a coherent subject the user
captured as one thing) is processed through deadline pre-pass → project
picker → multi-record verdict → spawn one Thread (flat or singular
umbrella, depending on verdict's record count). Used today by
:mod:`work_buddy.pipelines.inline` for right-click "Send to agent"
captures, but parameterised on ``source`` so future per-message email
triage and any other one-input-at-a-time pipeline can call into the
same primitive.

Three shapes returned from ``spawn_thread_for_matter``, decided by the
verdict's output:

- **flat**: 1 actionable record → ONE root Thread with the action.
- **singular_umbrella**: 2+ actionable records → ONE umbrella with
  ``parent_relationship='singular'`` + N children, each with one action.
  The dashboard render hoists the children's actions onto the umbrella's
  card (see ``threads/grouping`` and ``threads/render._per_action_state_from_fsm``).
- **dismissed**: all-delete records → one DISMISSED root Thread for
  the audit trail.
- **refusal**: verdict refused → one root Thread in
  ``AWAITING_INTENT_CLARIFICATION`` with the refusal payload.

Stage 2 of the singular-pattern fix introduces this module; the
inline pipeline calls it once per matter (where matters are detected
upstream by :mod:`work_buddy.clarify.text_segmenter`). Stage 1's
helpers in ``pipelines.inline`` are reused via import — this module
does NOT duplicate the spawn logic; it orchestrates the per-matter
sub-calls and dispatches to the right spawn shape.

A future cleanup may consolidate the spawn helpers themselves into
this module; for now they remain importable from
``pipelines.inline.*`` to minimise blast radius.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date_cls
from typing import Any

from work_buddy.threads.models import ContextItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sub-LLM outputs as durable thread context
# ---------------------------------------------------------------------------


def _build_subcall_context_items(
    *,
    deadline_hints: dict[str, Any] | None,
    project_candidates: list[dict[str, Any]] | None,
) -> tuple[ContextItem, ...]:
    """Convert per-matter sub-LLM outputs into ContextItems.

    Each spawned Thread (umbrella, child, flat, refusal, dismissed)
    carries these alongside the captured selection so the user can
    inspect what the deadline pre-pass extracted and what the project
    picker hedged on. Read by the dashboard's standard context-items
    section.

    The convention:

    - ``source="subcall"`` — distinguishes from selection / file /
      task context items.
    - ``type=<subcall_name>`` — ``deadline_extract`` /
      ``project_picker`` etc. The frontend can badge by type.
    - ``label`` — short human-readable summary (top deadline /
      top-confidence project / etc.).
    - ``payload`` — the FULL structured output for inspection.

    A future extension generalises this further: any ``DecomposedResult``
    from the framework provides ``sub_audits``, which a generic helper
    could convert to ContextItems automatically. For now the inline
    pipeline calls back here per known sub-call.
    """
    items: list[ContextItem] = []

    if deadline_hints is not None:
        # Build a terse label from whatever the pre-pass found.
        label_parts: list[str] = []
        if deadline_hints.get("has_deadline"):
            d = deadline_hints.get("deadline_date") or "(date unspecified)"
            label_parts.append(f"deadline: {d}")
        if deadline_hints.get("has_dependency"):
            dep = deadline_hints.get("dependency_hint") or "(unspecified)"
            label_parts.append(f"depends: {dep}")
        if deadline_hints.get("hint_extraction_failed"):
            label = "Deadline extraction failed (graceful degradation)"
        elif label_parts:
            label = "Deadline hints — " + "; ".join(label_parts)
        else:
            label = "Deadline hints — none detected"
        items.append(ContextItem(
            id="subcall:deadline_extract",
            source="subcall",
            type="deadline_extract",
            label=label,
            payload=dict(deadline_hints),
        ))

    if project_candidates:
        # Top-pick label so users see the headline at a glance.
        top = project_candidates[0]
        if top.get("project_tag") is None:
            label = (
                f"Project picker — top: no-project "
                f"(conf {top.get('confidence', 0.0):.2f})"
            )
        else:
            label = (
                f"Project picker — top: {top['project_tag']} "
                f"(conf {top.get('confidence', 0.0):.2f})"
            )
        items.append(ContextItem(
            id="subcall:project_picker",
            source="subcall",
            type="project_picker",
            label=label,
            payload={"candidates": list(project_candidates)},
        ))

    return tuple(items)


@dataclass(frozen=True)
class ThreadSpawnResult:
    """Return shape from :func:`spawn_thread_for_matter`.

    Attributes:
        kind: ``"flat"`` (one actionable record, no umbrella),
            ``"singular_umbrella"`` (umbrella + children),
            ``"dismissed"`` (all-delete, one terminal thread),
            ``"refusal"`` (verdict refused, one clarification thread),
            or ``"error"`` (verdict couldn't run; nothing spawned).
        thread_id: Primary thread id — the user-facing thread the user
            sees on the dashboard. For ``singular_umbrella`` this is
            the umbrella; for ``flat`` / ``dismissed`` / ``refusal``
            it's the spawned root thread. ``None`` for ``error``.
        child_thread_ids: For ``singular_umbrella`` only — the per-record
            child Thread ids. Empty for other kinds.
        verdict: The structured verdict dict that drove the spawn (for
            audit / debugging). May be ``None`` for ``error``.
        deadline_hints: Per-matter deadline pre-pass output.
        project_candidates: Per-matter project-picker candidates.
        dropped_count: Number of ``destination=delete`` records the
            verdict produced (counted, not spawned).
        error: Error string if ``kind="error"``; ``None`` otherwise.
    """

    kind: str
    thread_id: str | None = None
    child_thread_ids: tuple[str, ...] = ()
    verdict: dict[str, Any] | None = None
    deadline_hints: dict[str, Any] | None = None
    project_candidates: list[dict[str, Any]] | None = None
    dropped_count: int = 0
    error: str | None = None


def spawn_thread_for_matter(
    *,
    matter_text: str,
    matter_label: str = "",
    item_id: str,
    source: str = "inline",
    hint: str = "",
    file_path: str = "",
    cursor_line: int = 0,
    triage_context: dict[str, Any] | None = None,
    tier_chain: list[str] | None = None,
) -> ThreadSpawnResult:
    """Process one matter end-to-end: pre-passes → verdict → spawn.

    Encapsulates the per-matter pipeline so any singular-input source
    (inline-capture, future per-message email triage, etc.) can call
    one function and get back a ``ThreadSpawnResult``. Top-level
    pipelines (e.g. ``inline_capture``) detect matter-count via the
    text segmenter and call this primitive once per matter.

    Args:
        matter_text: The matter's captured text — drives deadline
            extraction, project picking, and the verdict prompt.
        matter_label: Optional short label for the matter (from the
            segmenter's output). Used for logging only.
        item_id: Stable id for the matter (typically a content hash).
            Used in escalation-log trace ids and as the spawned
            ContextItem id.
        source: Source-pipeline name carried on the inciting summary
            (e.g. ``"inline"``). Used by the dashboard to badge the
            thread's origin.
        hint: Optional user-typed intent hint (from the inline modal).
            Empty when absent. Forwarded into the verdict prompt and
            into the project picker.
        file_path: Vault-relative path the matter came from (inline
            only; empty for non-inline sources). Carried on metadata
            for click-back affordances.
        cursor_line: 0-indexed cursor line in the source file (inline
            only; 0 for non-inline). Carried on metadata.
        triage_context: Pre-built triage-context dict (active tasks /
            contracts / projects / commits) shared across matters
            from a single capture. ``None`` defaults to empty.
        tier_chain: Optional override for the verdict's tier chain.
            Defaults to ``triage.refine_clusters.tier_chain`` from
            config.

    Returns:
        :class:`ThreadSpawnResult` describing what was spawned.
    """
    # Local imports to avoid module-level side effects + import cycles.
    from work_buddy.clarify.deadline_extract import (
        extract_deadline_hints,
        merge_hints_into_records,
    )
    from work_buddy.clarify.items import TriageItem
    from work_buddy.clarify.project_picker import pick_projects
    from work_buddy.pipelines.inline import (
        _action_payload_for_record,  # noqa: F401 — used via _spawn_record_thread
        _call_multi_record_verdict,
        _spawn_dismissed_thread,
        _spawn_inline_umbrella,
        _spawn_record_thread,
        _spawn_refusal_thread,
    )

    triage_context = triage_context or {}

    # Build a per-matter TriageItem so the existing spawn helpers (which
    # parameterise on ``item``) can be reused without modification.
    item = TriageItem(
        id=item_id,
        text=matter_text,
        label=matter_label or _derive_label(matter_text),
        source=source,
        metadata={
            "file_path": file_path or "",
            "cursor_line": int(cursor_line or 0),
            "hint": hint or "",
            "matter_label": matter_label or "",
        },
    )

    # ---- Pre-passes (deadline + project picker) -----------------------
    deadline_hints = extract_deadline_hints(
        matter_text or "",
        message_date=_date_cls.today(),
        item_id=item_id,
    )

    project_candidates_payload = pick_projects(
        matter_text or "",
        active_projects=triage_context.get("active_projects") or [],
        hint=hint,
        item_id=item_id,
    )
    project_candidates = project_candidates_payload.get("candidates") or []

    # ---- Build sub-call ContextItems ---------------------------------
    # Phase 3 of the singular work: each spawned Thread carries the
    # deadline / project-picker outputs as durable ContextItems on top
    # of the captured selection. Renders in the dashboard's
    # context-items section so the user can inspect the model
    # reasoning that produced the spawned thread.
    subcall_items = _build_subcall_context_items(
        deadline_hints=deadline_hints,
        project_candidates=project_candidates,
    )

    # ---- Verdict ------------------------------------------------------
    verdict, verdict_error = _call_multi_record_verdict(
        item=item,
        deadline_hints=deadline_hints,
        triage_context=triage_context,
        project_candidates=project_candidates,
        tier_chain=tier_chain,
    )
    if verdict_error is not None:
        return ThreadSpawnResult(
            kind="error",
            error=verdict_error,
            deadline_hints=deadline_hints,
            project_candidates=project_candidates,
        )

    # ---- Refusal path -------------------------------------------------
    refusal = (verdict or {}).get("refusal")
    if isinstance(refusal, dict) and refusal.get("question"):
        thread_id = _spawn_refusal_thread(
            item=item, verdict=verdict, deadline_hints=deadline_hints,
            extra_context_items=subcall_items,
        )
        return ThreadSpawnResult(
            kind="refusal",
            thread_id=thread_id,
            verdict=verdict,
            deadline_hints=deadline_hints,
            project_candidates=project_candidates,
        )

    # ---- Filter actionable / dropped records --------------------------
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

    # ---- Dismissed (all-delete) path ----------------------------------
    if not actionable:
        thread_id = _spawn_dismissed_thread(
            item=item, verdict=verdict, dropped=dropped,
            extra_context_items=subcall_items,
        )
        return ThreadSpawnResult(
            kind="dismissed",
            thread_id=thread_id,
            verdict=verdict,
            deadline_hints=deadline_hints,
            project_candidates=project_candidates,
            dropped_count=len(dropped),
        )

    # ---- Flat (single actionable record) ------------------------------
    if len(actionable) == 1:
        thread_id = _spawn_record_thread(
            item=item, record=actionable[0],
            verdict=verdict, parent_id=None,
            extra_context_items=subcall_items,
        )
        return ThreadSpawnResult(
            kind="flat",
            thread_id=thread_id,
            verdict=verdict,
            deadline_hints=deadline_hints,
            project_candidates=project_candidates,
            dropped_count=len(dropped),
        )

    # ---- Singular umbrella (2+ actionable records) --------------------
    umbrella_id = _spawn_inline_umbrella(
        item=item, verdict=verdict,
        extra_context_items=subcall_items,
    )
    child_ids: list[str] = []
    for rec in actionable:
        cid = _spawn_record_thread(
            item=item, record=rec, verdict=verdict, parent_id=umbrella_id,
            extra_context_items=subcall_items,
        )
        if cid:
            child_ids.append(cid)
    return ThreadSpawnResult(
        kind="singular_umbrella",
        thread_id=umbrella_id,
        child_thread_ids=tuple(child_ids),
        verdict=verdict,
        deadline_hints=deadline_hints,
        project_candidates=project_candidates,
        dropped_count=len(dropped),
    )


def _derive_label(text: str, *, max_chars: int = 72) -> str:
    """First non-empty line of the text, stripped + truncated."""
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-*+# ").strip()
        if stripped:
            if len(stripped) > max_chars:
                return stripped[: max_chars - 1] + "…"
            return stripped
    return "(empty matter)"


__all__ = [
    "ThreadSpawnResult",
    "spawn_thread_for_matter",
]
