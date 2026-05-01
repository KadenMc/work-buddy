"""Slice 7: develop-at-pickup decomposition flow.

When a task fails :func:`compute_pickup_readiness` (sparse + low
involvement OR density signals fire OR explicit ``/wb-task-develop``
invocation), the engage flow proposes a decomposition into action
items.  The user reviews edit-each-item and the approved subset is
written to :mod:`work_buddy.obsidian.tasks.action_items` with
``user_authored=0, approved_at=<now>`` per the safety rule
(ROADMAP §7).

Two phases:

1. :func:`propose_decomposition` -- LLM call producing a structured
   list of action-item proposals.  No state mutation.  Returns a
   :class:`DevelopProposal` carrying the proposals + the task
   context + raw LLM output.
2. :func:`apply_decomposition` -- writes the user-approved items
   into the table.  Tier-aware via the existing Slice-4 risk
   resolver: tier-1 callers see the proposal + must explicitly
   approve; tier-3+/4 doesn't apply here because decomposition is
   ALWAYS user-reviewed (ROADMAP §7 hallucination gate is a hard
   NO regardless of tier).

The proposal LLM call uses the existing ``call_for_verdict`` helper
from :mod:`work_buddy.clarify.verdict_call` so retry / escalation
behavior matches the Slice 3 multi-record pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


DEVELOP_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {
            "type": "string",
            "description": (
                "One to three sentences explaining the decomposition.  "
                "Cite the task title + any signals (deadline, density) "
                "that motivated the breakdown."
            ),
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "Concrete action -- one verb, one object, "
                            "one outcome.  Plain prose; the markdown "
                            "bullet wraps this verbatim."
                        ),
                    },
                    "definition_of_done": {
                        "type": ["string", "null"],
                        "description": (
                            "Optional closing signal for THIS step.  "
                            "Null when the step is self-evidently "
                            "complete."
                        ),
                    },
                    "agent_required_contexts": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "Slice-5a per-item override.  Null = "
                            "inherit from parent task."
                        ),
                    },
                    "user_required_contexts": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "Slice-5a per-item override."
                        ),
                    },
                    "risk_profile": {
                        "type": ["object", "null"],
                        "description": (
                            "Slice-4 per-item risk profile.  Optional; "
                            "null inherits from the parent task."
                        ),
                    },
                },
                "required": ["description"],
                "additionalProperties": False,
            },
            "description": (
                "Ordered action items.  Empty list = the LLM "
                "concluded no decomposition was warranted (the task is "
                "atomic at its current grain)."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0 self-assessed confidence.",
        },
    },
    "required": ["rationale", "items"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class DevelopProposal:
    """Result of :func:`propose_decomposition`.

    ``items`` is empty when the LLM concluded no decomposition is
    warranted (tasks are sometimes correctly atomic at their grain).
    The engage flow surfaces ``items=[]`` as "no decomposition needed
    -- proceed."
    """

    task_id: str
    rationale: str
    items: tuple[dict[str, Any], ...]
    confidence: float = 0.0
    raw_llm_output: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_DEVELOP_SYSTEM_PROMPT = """\
You are decomposing one task into ordered action items.  The user has
just picked this task up and the system flagged it as needing
development before execution (sparse capture + low involvement, OR
the density-promotion heuristic fired, OR the user invoked
/wb-task-develop explicitly).

Produce a JSON object matching DEVELOP_VERDICT_SCHEMA.  Each action
item is a single concrete physical step -- one verb, one object,
one outcome.  Plain prose; the markdown bullet stores it verbatim.

## Rules

- Quality bar: each item is a step a tired user could execute without
  re-reading the parent task.  No "investigate the X situation" --
  that's another decomposition.
- Order matters.  Items are sequenced 1, 2, 3 ... in the order the
  user should execute.
- Empty list IS valid.  Return items=[] when the task is correctly
  atomic at its current grain.  V1a (attention scarcity) -- don't
  manufacture sub-actions to look productive.
- Per-item risk_profile / contexts are OPTIONAL overrides.  Omit them
  to inherit the parent task's profile.
- Cite specific task content in the rationale (a phrase or sentence
  from the task description / note) so the user can verify your
  understanding.

## Hallucination gate (ROADMAP section 7)

The user reviews every item before any agent executes it.  Your job
is PROPOSAL only.  The user clicks accept on each item; only accepted
items get persisted with approved_at set.  Unaccepted items are
discarded (NOT silently kept as "agent-proposed pending review").
The downstream is_executable check refuses agent execution of items
that lack approved_at, so you cannot accidentally cause an unsafe
write by being too generous.

## Refusal

Set items=[] when:
- The task description is too vague to break down ("clean up the
  inbox" with no other context).
- The task is genuinely atomic at the right grain ("call mom").
- You don't have enough context (rather than guess, refuse).

In any of these cases, set rationale honestly and set
confidence < 0.3.
"""


def _render_user_prompt(*, task: Mapping[str, Any], note_body: str | None) -> str:
    parts: list[str] = []
    parts.append(f"Task id: {task.get('task_id', 'unknown')}")
    parts.append(f"Task description: {task.get('description', '(unknown)')}")
    parts.append(f"State: {task.get('state', 'unknown')}")
    parts.append(f"Density: {task.get('density', 'sparse')}")
    parts.append(f"Creation provenance: {task.get('creation_provenance', 'manual')}")
    if task.get("has_deadline"):
        parts.append(f"Deadline: {task.get('deadline_date', '(unspecified)')}")
    if task.get("has_dependency"):
        parts.append(f"Dependency hint: {task.get('dependency_hint', '')}")
    if task.get("contract"):
        parts.append(f"Contract: {task['contract']}")
    if task.get("agent_required_contexts"):
        parts.append(f"Agent contexts: {task['agent_required_contexts']}")
    if task.get("user_required_contexts"):
        parts.append(f"User contexts: {task['user_required_contexts']}")
    parts.append("")
    if note_body:
        parts.append("---- Note body ----")
        parts.append(note_body.strip())
        parts.append("---- End note ----")
    else:
        parts.append("(No linked note body.)")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# propose_decomposition
# ---------------------------------------------------------------------------


def propose_decomposition(
    task_id: str,
    *,
    runner=None,
    tier=None,
    note_body: str | None = None,
) -> DevelopProposal:
    """LLM-driven decomposition of one task into action items.

    Args:
        task_id: The parent task id (must exist in the store).
        runner: A :class:`work_buddy.llm.LLMRunner` instance.  Required.
        tier: Optional model tier override
            (defaults to FRONTIER_BALANCED via call_for_verdict).
        note_body: Optional pre-loaded note body.  When None, the
            function attempts to load via the bridge -- failures
            degrade silently (the LLM still has the task description).

    Returns:
        A :class:`DevelopProposal`.  ``items`` is the proposed list;
        ``error`` is set on LLM failure with items=[] for safety.
    """
    if runner is None:
        return DevelopProposal(
            task_id=task_id, rationale="",
            items=(),
            error="propose_decomposition requires a runner",
        )

    # Load task + note context.
    try:
        from work_buddy.obsidian.tasks import store as tasks_store
        task = tasks_store.get(task_id) or {}
    except Exception as exc:
        return DevelopProposal(
            task_id=task_id, rationale="", items=(),
            error=f"task load failed: {exc}",
        )
    if not task:
        return DevelopProposal(
            task_id=task_id, rationale="", items=(),
            error=f"task {task_id} not found",
        )
    task = dict(task)
    task["task_id"] = task_id

    if note_body is None and task.get("note_uuid"):
        try:
            from work_buddy.obsidian import bridge
            note_body = bridge.read_file(f"tasks/notes/{task['note_uuid']}.md")
        except Exception as exc:
            logger.debug("propose_decomposition: note load failed: %s", exc)
            note_body = None

    user_prompt = _render_user_prompt(task=task, note_body=note_body)

    try:
        from work_buddy.clarify.verdict_call import call_for_verdict
        from work_buddy.llm import ModelTier
        resp = call_for_verdict(
            runner=runner,
            tier=tier or ModelTier.FRONTIER_BALANCED,
            system=_DEVELOP_SYSTEM_PROMPT,
            user=user_prompt,
            output_schema=DEVELOP_VERDICT_SCHEMA,
            required_fields=("rationale", "items"),
            caller="develop_at_pickup",
            item_id=task_id,
        )
    except Exception as exc:  # pragma: no cover -- defensive
        return DevelopProposal(
            task_id=task_id, rationale="", items=(),
            error=f"LLM call failed: {exc}",
        )

    if resp.is_error():
        return DevelopProposal(
            task_id=task_id, rationale="", items=(),
            raw_llm_output=resp.content,
            error=resp.error,
        )

    output = resp.structured_output or {}
    raw_items = output.get("items") or []
    parsed_items: list[dict[str, Any]] = []
    for it in raw_items:
        if not isinstance(it, Mapping):
            continue
        desc = it.get("description")
        if not isinstance(desc, str) or not desc.strip():
            continue
        parsed_items.append({
            "description": desc.strip(),
            "definition_of_done": it.get("definition_of_done") or None,
            "agent_required_contexts": it.get("agent_required_contexts"),
            "user_required_contexts": it.get("user_required_contexts"),
            "risk_profile": it.get("risk_profile"),
        })

    return DevelopProposal(
        task_id=task_id,
        rationale=str(output.get("rationale") or ""),
        items=tuple(parsed_items),
        confidence=float(output.get("confidence") or 0.0),
        raw_llm_output=resp.content,
    )


# ---------------------------------------------------------------------------
# apply_decomposition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of :func:`apply_decomposition`."""
    task_id: str
    items_created: int
    item_ids: tuple[int, ...] = field(default_factory=tuple)
    set_current: int | None = None
    error: str | None = None


def apply_decomposition(
    task_id: str,
    *,
    approved_items: list[dict[str, Any]],
    set_current_to_first: bool = True,
) -> ApplyResult:
    """Persist user-approved action items.

    Args:
        task_id: Parent task id.
        approved_items: List of dicts the user accepted.  Each carries
            description (required) + optional definition_of_done /
            agent_required_contexts / user_required_contexts /
            risk_profile.  Items NOT in this list are discarded -- the
            user explicitly rejected them.
        set_current_to_first: When True (default), points
            ``current_action_item_id`` at the first newly-created item
            so the master-list "step N of M" badge lights up
            immediately.

    Per ROADMAP §7 safety rule, every persisted item lands with
    ``user_authored=0, approved_at=<now>`` -- the user reviewed and
    approved an agent-proposed item.  ``is_executable`` admits items
    in this state.

    Bumps ``density`` from 'sparse' to 'developed' on the parent task
    when the first action item lands (so future pickup-readiness
    checks see the developed signal).
    """
    if not approved_items:
        return ApplyResult(
            task_id=task_id,
            items_created=0,
            error="no items approved (refusing to write empty decomposition)",
        )

    try:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        from work_buddy.obsidian.tasks import action_items, store
        from work_buddy.automation.contexts import serialize_context_list
        from work_buddy.automation.risk import parse_risk_profile
    except Exception as exc:  # pragma: no cover -- defensive
        return ApplyResult(
            task_id=task_id, items_created=0,
            error=f"import failed: {exc}",
        )

    created_ids: list[int] = []
    for it in approved_items:
        desc = (it.get("description") or "").strip()
        if not desc:
            continue
        risk_json = None
        rp = it.get("risk_profile")
        if isinstance(rp, dict):
            try:
                risk_json = parse_risk_profile(rp).to_json()
            except Exception:  # pragma: no cover
                pass
        agent_ctx = serialize_context_list(it.get("agent_required_contexts"))
        user_ctx = serialize_context_list(it.get("user_required_contexts"))
        result = action_items.create(
            task_id=task_id,
            description=desc,
            definition_of_done=it.get("definition_of_done"),
            risk_profile_json=risk_json,
            agent_required_contexts=agent_ctx,
            user_required_contexts=user_ctx,
            user_authored=False,           # agent-proposed origin
            approved_at=now_iso,           # user's explicit approval
        )
        created_ids.append(int(result["id"]))

    if not created_ids:
        return ApplyResult(
            task_id=task_id, items_created=0,
            error="no valid items in approved_items (descriptions empty)",
        )

    set_current_id: int | None = None
    if set_current_to_first:
        set_current_id = created_ids[0]
        action_items.set_current(task_id, set_current_id)

    # Bump density sparse -> developed once items exist.
    try:
        existing = store.get(task_id) or {}
        if (existing.get("density") or "sparse") == "sparse":
            store.update(
                task_id,
                density="developed",
                reason="develop-at-pickup: action items created",
            )
    except Exception as exc:  # pragma: no cover
        logger.debug("apply_decomposition: density bump failed: %s", exc)

    return ApplyResult(
        task_id=task_id,
        items_created=len(created_ids),
        item_ids=tuple(created_ids),
        set_current=set_current_id,
    )
