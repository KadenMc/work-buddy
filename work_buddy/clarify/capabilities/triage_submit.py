"""``triage_submit`` capability — record a local-agent verdict into the pool.

Exposed to background-triage agents via the ``triage_agent`` tool
preset. Safe to call from outside a live run: unknown ``run_id`` or
``item_id`` return a structured error and do nothing. That makes it
a real, reusable work-buddy capability rather than a synthetic
"emit_verdict" tool that only makes sense inside one loop.

The capability is intentionally narrow: it validates the run,
validates the payload shape, and writes one :class:`ClarifyEntry`.
All reasoning about what to do with that verdict happens later,
during the on-demand review.
"""

from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.clarify.background import get_pool

logger = get_logger(__name__)


def triage_submit(
    *,
    run_id: str,
    item_id: str,
    rationale: str,
    group_intent: str | None = None,
    confidence: float | None = None,
    # ---- Slice 3 multi-record fields (preferred for new captures) ----
    records: list[dict[str, Any]] | None = None,
    refusal: dict[str, Any] | None = None,
    pipeline_blocker: dict[str, Any] | str | None = None,
    # ---- Legacy single-action fields (Slice 1 compatibility) ----
    recommended_action: str | None = None,
    target_task_id: str | None = None,
    suggested_task_text: str | None = None,
    related_item_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Submit a Clarify verdict for one item of an active background run.

    Accepts BOTH verdict shapes during the Slice 1→3 migration window:

    - **Multi-record (Slice 3+):** pass ``records`` (zero or more
      ``{destination, ...proposal}`` dicts) and/or ``refusal`` (dict
      with a ``question`` field for low-confidence verdicts that need
      human routing). ``records=[]`` is valid — equivalent to the
      legacy ``leave`` action ("nothing to file here").
    - **Legacy (Slice 1):** pass ``recommended_action`` plus the
      action-specific fields (``suggested_task_text`` for
      create_task, ``target_task_id`` for record_into_task,
      ``related_item_ids`` for group). The pool's read path still
      handles these; new producers should prefer the multi-record
      shape.

    Args:
        run_id: The producer-assigned run identifier. The agent
            receives this in its prompt.
        item_id: The id of the item this verdict applies to. Must
            belong to the named run.
        rationale: One-to-three-sentence explanation of the verdict.
            Persisted verbatim.
        group_intent: Short (≤8-word) noun-phrase naming the
            underlying *intent* behind the item. Used as the card
            title in the Resolution Surface.
        confidence: Optional [0,1] self-assessed confidence.
        records: (Slice 3+) List of records. Each is a dict with
            ``destination`` (one of TRIAGE_DESTINATIONS) and a
            destination-specific proposal (``task_proposal`` for
            destination=task, etc.). Empty list means "no record
            produced." Mutually exclusive with ``refusal``.
        refusal: (Slice 3+) ``{"question": "...", ...}`` when the
            agent doesn't have enough context to commit a verdict.
            Renders as a clarification card on the Resolution Surface.
        pipeline_blocker: (Slice 1.5) Typed stop reason per ROADMAP
            §3.3. String (just the kind) or dict with ``kind`` +
            optional ``detail``.
        recommended_action: (Legacy) One of TRIAGE_ACTIONS. Required
            ONLY when records/refusal are not provided.
        target_task_id: (Legacy) For ``record_into_task``.
        suggested_task_text: (Legacy) For ``create_task``.
        related_item_ids: (Legacy) For ``group``.

    Returns:
        ``{"status": "ok", ...}`` on accepted submission.
        ``{"status": "error", "error": ...}`` for any rejection.
    """
    if not run_id or not isinstance(run_id, str):
        return {"status": "error", "error": "run_id must be a non-empty string."}
    if not item_id or not isinstance(item_id, str):
        return {"status": "error", "error": "item_id must be a non-empty string."}
    if not rationale or not isinstance(rationale, str):
        return {"status": "error", "error": "rationale must be a non-empty string."}

    has_multi_record = records is not None or refusal is not None
    has_legacy = bool(recommended_action)

    if not has_multi_record and not has_legacy:
        return {
            "status": "error",
            "error": (
                "verdict must include either 'records'/'refusal' "
                "(multi-record shape) or 'recommended_action' (legacy)."
            ),
            "hint": (
                "For new captures use the multi-record shape: pass "
                "``records=[{'destination': 'task', "
                "'task_proposal': {...}}]`` (or ``records=[]`` for "
                "no-op)."
            ),
        }

    verdict: dict[str, Any] = {
        "rationale": rationale.strip(),
    }
    if group_intent and isinstance(group_intent, str):
        # Keep it short: agents occasionally duplicate the rationale
        # here. Truncate at a generous bound; the Resolution Surface
        # only shows the first ~80 chars anyway.
        verdict["group_intent"] = group_intent.strip()[:160]
    if confidence is not None:
        try:
            c = float(confidence)
        except (TypeError, ValueError):
            return {
                "status": "error",
                "error": "confidence must be a number between 0 and 1.",
            }
        if not (0.0 <= c <= 1.0):
            return {
                "status": "error",
                "error": "confidence must be between 0 and 1.",
            }
        verdict["confidence"] = c

    # Multi-record fields (Slice 3+).
    if records is not None:
        if not isinstance(records, list):
            return {
                "status": "error",
                "error": "records must be a list of record dicts.",
            }
        # Per-record validation deferred to pool.submit() so the
        # destination-enum error message lives in one place.
        verdict["records"] = records
    if refusal is not None:
        if not isinstance(refusal, dict):
            return {
                "status": "error",
                "error": "refusal must be a dict with a 'question' field.",
            }
        if not refusal.get("question"):
            return {
                "status": "error",
                "error": "refusal.question is required when refusal is set.",
            }
        verdict["refusal"] = refusal
    if pipeline_blocker is not None:
        # Accepts either a string kind or a {kind, detail} dict — the
        # pool stores it verbatim, the resolver in
        # work_buddy.clarify.resolution.extract_pipeline_blocker
        # normalizes on read.
        verdict["pipeline_blocker"] = pipeline_blocker

    # Legacy fields (Slice 1). Either-or with multi-record at the
    # validation layer above; producing both is undefined behavior
    # but the pool will store it (the read path picks multi-record
    # first).
    if recommended_action:
        verdict["recommended_action"] = recommended_action
    if target_task_id:
        verdict["target_task_id"] = str(target_task_id)
    if suggested_task_text:
        verdict["suggested_task_text"] = str(suggested_task_text)
    if related_item_ids:
        if not isinstance(related_item_ids, list):
            return {
                "status": "error",
                "error": "related_item_ids must be a list of strings.",
            }
        verdict["related_item_ids"] = [str(x) for x in related_item_ids]

    pool = get_pool()
    result = pool.submit(run_id=run_id, item_id=item_id, verdict=verdict)
    if result.get("status") != "ok":
        logger.info(
            "triage_submit rejected: run=%s item=%s reason=%s",
            run_id, item_id, result.get("error"),
        )
    return result
