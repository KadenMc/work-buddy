"""Project-picker SubCall: hedged ranked-candidate project assignment.

The verdict LLM (Sonnet-class) is good at reasoning about WHICH project a
captured thought belongs to when given context, but spending Sonnet
tokens on a step that's mostly classification is wasteful — and the
"obviously not project work" + "I'm not sure" cases need cheap honest
answers, not confident force-picks.

This module declares :data:`PROJECT_PICKER_SUBCALL`, a
:class:`work_buddy.llm.SubCall` that runs BEFORE the verdict pass on the
inline / journal capture pipelines and produces a hedged candidate list:

- Each candidate has ``project_tag`` (slug or ``null``), ``confidence``
  in ``[0.0, 1.0]``, and a one-sentence ``rationale``.
- The list is variable-length: a confident model emits 1 non-null + null;
  an uncertain model emits several non-nulls + null; "probably not
  project work" returns just null at high confidence.
- The ``null`` (no-project) entry is REQUIRED to appear in every output.
  Validation drops outputs missing it.
- Slugs not in the active project registry are dropped (with a warning)
  rather than passed through to the verdict.

The verdict reads the candidates from the user prompt and decides what
``task_proposal.project_tag`` to set, with broader context the picker
doesn't have (recent commits, active contracts, the user's own hint,
tone). The verdict is the decision-maker; the picker is a research aide.

No numeric thresholds in Python. The verdict's reasoning over candidates
+ broader context decides whether to apply a tag, leave null, or refuse.
This is intentional: hardcoding a threshold below the smartest LLM in
the chain forces a decision at the dumbest layer.
"""

from __future__ import annotations

from typing import Any

from work_buddy.llm import LLMRunner, SubCall, run_subcall
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


_PROJECT_PICKER_SYSTEM_PROMPT = """\
You are scoring how well a captured thought matches each of the user's
active projects. Your output is a HEDGED, RANKED candidate list — not a
single pick.

## What you produce

A JSON object with one field, ``candidates``: an array of entries, each
with:

- ``project_tag``: a project slug (string) OR ``null`` for "no project".
- ``confidence``: a number in [0.0, 1.0].
- ``rationale``: one short sentence explaining why this candidate
  scored the way it did.

## Hard rules

1. **The "no project" candidate (``project_tag: null``) MUST appear
   exactly once in every output.** Even when the text is obviously
   project-related, include null with a low confidence — it's the
   honest "what if I'm wrong" baseline.

2. **Never invent slugs that aren't in the active project list** the
   user prompt provides. Stick to the slugs verbatim (case-sensitive).

3. **No threshold-style pre-filtering on your end.** Emit any
   candidate you consider plausible (≥ ~0.10 confidence). Don't try
   to "decide" — the downstream verdict, with broader context, will.

4. **Cap the output at the soft limit declared in the user prompt**
   (default 5 entries total). If you have more than that many
   plausible candidates you should probably be less confident about
   any single one — that ambiguity should show in the confidences.

## How to think about this

Three modes the model should naturally fall into:

- **Confident (the project is named or strongly implied):**
  ``[{tag: "slug", conf: 0.85, rationale: "..."}, {tag: null, conf: 0.15, rationale: "..."}]``

- **Uncertain (multiple projects plausible):**
  ``[{tag: "slug_a", conf: 0.45, ...}, {tag: "slug_b", conf: 0.30, ...},
     {tag: "slug_c", conf: 0.15, ...}, {tag: null, conf: 0.30, ...}]``
  Note: confidences across non-null candidates need NOT sum to 1.0 minus
  null's score — they're independent calibration estimates, not a
  probability distribution. The verdict doesn't need normalized math; it
  reads them as evidence.

- **Probably not project work:**
  ``[{tag: null, conf: 0.90, rationale: "Captured text is a passing thought, not project work"}]``
  Just null. No non-null candidates needed.

## What "match" means

A capture matches a project when:
- The text mentions the project by name, abbreviation, or topic.
- The text describes work that's clearly INSIDE that project's scope
  (per the project's description in the active list).
- The text describes a deliverable, milestone, blocker, or note for
  that project.

A capture does NOT match a project when:
- The text is a passing thought, observation, or unrelated idea.
- The text is about the user's life, calendar, errands, or general
  reading — those are not project work.
- The text mentions a project tangentially (e.g., "remembered to email
  Bob from the X project about something unrelated") — that's about
  Bob, not project X.

## Rationale guidance

Keep each rationale to one sentence. Cite the specific phrase or signal
that drove the confidence — "mentions 'TKA paper deadline'" beats
"text seems related". The verdict uses these to calibrate trust.

## Output the JSON object exactly. No prose around it.
"""


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


_PROJECT_CANDIDATES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project_tag": {"type": ["string", "null"]},
                    # Anthropic strict structured-output rejects
                    # minimum/maximum; range is enforced in
                    # _validate_candidates after parse.
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["project_tag", "confidence", "rationale"],
            },
        },
    },
    "required": ["candidates"],
}


# ---------------------------------------------------------------------------
# Soft-fail default + validation
# ---------------------------------------------------------------------------


_NULL_CANDIDATE: dict[str, Any] = {
    "project_tag": None,
    "confidence": 1.0,
    "rationale": (
        "Project picker SubCall unavailable; defaulting to no project. "
        "The verdict can still set a project from broader context."
    ),
}


# This is what gets returned when the SubCall's tier chain exhausts.
# Just the null candidate at full confidence — the verdict gets a clear
# "I have no signal here, you decide" signal rather than empty data.
_FAILURE_DEFAULT: dict[str, Any] = {"candidates": [_NULL_CANDIDATE]}


def _build_project_picker_user_prompt(inputs: dict[str, Any]) -> str:
    """SubCall user-prompt builder.

    Reads:
        ``text``: the captured-item body to score against projects.
        ``active_projects``: list of ``{slug, name, status, description}``
            dicts (matches ``build_triage_context``'s ``active_projects``).
        ``hint``: optional user-typed intent hint (passed via the inline
            modal, journal frontmatter, etc.). Empty string when absent.
        ``max_candidates``: soft cap on output size (defaults to 5 when
            not present in inputs).
    """
    text = (inputs.get("text") or "").strip()
    active = inputs.get("active_projects") or []
    hint = (inputs.get("hint") or "").strip()
    cap = int(inputs.get("max_candidates") or 5)

    project_lines: list[str] = []
    for p in active:
        if not isinstance(p, dict):
            continue
        slug = p.get("slug") or ""
        if not slug:
            continue
        desc = (p.get("description") or "").strip()
        status = (p.get("status") or "").strip()
        meta = []
        if status:
            meta.append(f"status={status}")
        meta_s = f" ({', '.join(meta)})" if meta else ""
        line = f"- {slug}{meta_s}"
        if desc:
            line += f": {desc}"
        project_lines.append(line)

    if not project_lines:
        project_lines = ["(no active projects registered)"]

    hint_block = (
        f"\n## User hint\n\n{hint}\n"
        if hint else ""
    )

    return (
        f"## Active projects\n\n"
        + "\n".join(project_lines)
        + f"\n\n## Captured text\n\n{text}\n"
        f"{hint_block}"
        f"\n## Soft cap\n\n"
        f"Emit at most {cap} candidates total (including the required "
        f"null entry). If you'd want more than {cap}, you're probably "
        f"too uncertain — that uncertainty should show in the "
        f"confidences of the ones you do emit."
        f"\n\nReturn the JSON object."
    )


PROJECT_PICKER_SUBCALL = SubCall(
    name="project_picker",
    system_prompt=_PROJECT_PICKER_SYSTEM_PROMPT,
    user_prompt=_build_project_picker_user_prompt,
    output_schema=_PROJECT_CANDIDATES_SCHEMA,
    config_key="triage.project_picker",
    fail_policy="soft",
    soft_fail_default=_FAILURE_DEFAULT,
)


# ---------------------------------------------------------------------------
# Validation / post-processing
# ---------------------------------------------------------------------------


def _validate_and_normalize_candidates(
    raw_output: dict[str, Any],
    *,
    active_slugs: set[str],
    max_candidates: int,
) -> dict[str, Any]:
    """Validate and clean the SubCall output.

    Operations performed (in order):

    1. Coerce ``confidence`` into ``[0.0, 1.0]``; drop entries where
       coercion fails.
    2. Drop non-null candidates whose ``project_tag`` isn't in
       ``active_slugs`` (warns once per drop). Null is always allowed.
    3. Deduplicate: if the model emitted the same slug twice, keep the
       higher-confidence entry.
    4. Sort by ``confidence`` descending (null can land anywhere).
    5. Truncate to ``max_candidates`` entries.
    6. Ensure the ``null`` candidate is present. If the model omitted
       it, append one at confidence 0.10 ("model didn't say; default
       low signal").

    Returns ``{"candidates": [...]}`` ready for the verdict prompt.
    Mirrors the SubCall output schema.
    """
    raw = list(raw_output.get("candidates") or [])
    cleaned: list[dict[str, Any]] = []
    seen: dict[str | None, dict[str, Any]] = {}

    for entry in raw:
        if not isinstance(entry, dict):
            continue

        tag_raw = entry.get("project_tag")
        if tag_raw is not None and not isinstance(tag_raw, str):
            continue
        tag: str | None = tag_raw if isinstance(tag_raw, str) else None

        if tag is not None and tag not in active_slugs:
            logger.info(
                "project_picker: dropping candidate with unknown slug "
                "%r (not in active projects)", tag,
            )
            continue

        try:
            conf = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if conf < 0.0:
            conf = 0.0
        if conf > 1.0:
            conf = 1.0

        rationale = entry.get("rationale") or ""
        if not isinstance(rationale, str):
            rationale = str(rationale)

        normalized = {
            "project_tag": tag,
            "confidence": conf,
            "rationale": rationale.strip(),
        }

        # Deduplicate by tag — keep highest confidence.
        existing = seen.get(tag)
        if existing is not None:
            if conf > float(existing.get("confidence") or 0.0):
                seen[tag] = normalized
        else:
            seen[tag] = normalized

    cleaned = list(seen.values())

    cleaned.sort(key=lambda e: float(e.get("confidence") or 0.0), reverse=True)

    if max_candidates and len(cleaned) > max_candidates:
        # Preserve null in the top-K since downstream consumers expect it.
        null_entry = next(
            (c for c in cleaned if c.get("project_tag") is None),
            None,
        )
        cleaned = cleaned[:max_candidates]
        if null_entry is not None and null_entry not in cleaned:
            # Make room for null by dropping the lowest-confidence entry.
            cleaned[-1] = null_entry
            cleaned.sort(
                key=lambda e: float(e.get("confidence") or 0.0),
                reverse=True,
            )

    has_null = any(c.get("project_tag") is None for c in cleaned)
    if not has_null:
        cleaned.append({
            "project_tag": None,
            "confidence": 0.10,
            "rationale": (
                "(injected) Model omitted the no-project candidate; "
                "appending a low-confidence null for verdict context."
            ),
        })
        cleaned.sort(
            key=lambda e: float(e.get("confidence") or 0.0),
            reverse=True,
        )

    return {"candidates": cleaned}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def pick_projects(
    text: str,
    *,
    active_projects: list[dict[str, Any]],
    hint: str = "",
    item_id: str = "",
    max_candidates: int | None = None,
    runner: LLMRunner | None = None,
) -> dict[str, Any]:
    """Run the project-picker SubCall and validate its output.

    Args:
        text: The captured-item body to score against projects.
        active_projects: List of ``{slug, name, status, description}``
            dicts as produced by :func:`work_buddy.clarify.recommend.build_triage_context`'s
            ``active_projects`` field.
        hint: Optional user-typed intent hint (e.g., from the inline
            send-to-agent modal). Empty string when absent.
        item_id: Used in the trace_id for escalation_log correlation.
        max_candidates: Soft cap on the candidate list. ``None`` reads
            from ``triage.project_picker.max_candidates`` (default 5).
        runner: Optional :class:`LLMRunner` override for tests.

    Returns:
        ``{"candidates": [{"project_tag", "confidence", "rationale"}, ...]}``
        with the ``null`` candidate guaranteed to be present, slugs
        validated against the active project list, dedup'd, sorted by
        confidence descending, and capped at ``max_candidates``.

        On full chain exhaustion, returns the soft-fail default (just
        the null candidate at full confidence). The verdict pass still
        runs and decides project_tag from broader context.
    """
    text_clean = (text or "").strip()
    if not text_clean:
        # No text → skip the LLM and just return the null candidate.
        return {
            "candidates": [{
                "project_tag": None,
                "confidence": 1.0,
                "rationale": "Empty captured text; no project signal.",
            }],
        }

    # Resolve max_candidates from config when caller didn't pin it.
    cap = max_candidates
    if cap is None:
        cap = _resolve_max_candidates()

    inputs = {
        "text": text_clean,
        "active_projects": list(active_projects or []),
        "hint": hint or "",
        "max_candidates": cap,
    }
    trace_id = (
        f"project_picker:{item_id}" if item_id else "project_picker"
    )

    result = run_subcall(
        PROJECT_PICKER_SUBCALL,
        inputs,
        trace_id=trace_id,
        runner=runner,
    )

    active_slugs = {
        p.get("slug")
        for p in active_projects or []
        if isinstance(p, dict) and isinstance(p.get("slug"), str) and p["slug"]
    }
    return _validate_and_normalize_candidates(
        result.output or {},
        active_slugs=active_slugs,
        max_candidates=cap,
    )


def _resolve_max_candidates() -> int:
    """Read ``triage.project_picker.max_candidates`` from config.

    Falls back to 5 when the config block is missing or unreadable.
    """
    try:
        from work_buddy.clarify.config import load_triage_config

        cfg = load_triage_config() or {}
    except Exception as exc:
        logger.warning(
            "project_picker: load_triage_config failed (%s); "
            "using max_candidates=5",
            exc,
        )
        return 5
    pp = cfg.get("project_picker") or {}
    cap = pp.get("max_candidates")
    if isinstance(cap, int) and cap > 0:
        return cap
    return 5


def render_project_candidates_block(candidates: list[dict[str, Any]] | None) -> str:
    """Render the candidate list as a prompt block for the verdict pass.

    Returns a string formatted for inclusion in the verdict's user
    prompt. Empty / None input returns an empty string (the verdict
    decides project_tag from its own context with no extra signal).
    """
    if not candidates:
        return ""

    lines = ["## Project candidates (from sub-LLM)"]
    lines.append(
        "These are hedged guesses from a smaller LLM with limited context. "
        "Treat them as evidence, not a decision. Set "
        "``task_proposal.project_tag`` based on your reasoning over them "
        "PLUS the broader context (active contracts, user hint, recent "
        "commits). Lean toward null when uncertain — declining to assign "
        "a project is preferable to a wrong assignment."
    )
    lines.append("")

    for c in candidates:
        if not isinstance(c, dict):
            continue
        tag = c.get("project_tag")
        try:
            conf = float(c.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        rationale = (c.get("rationale") or "").strip()
        tag_disp = "null (no project)" if tag is None else tag
        line = f"- {tag_disp} — confidence={conf:.2f}"
        if rationale:
            line += f": {rationale}"
        lines.append(line)

    return "\n".join(lines)


__all__ = [
    "PROJECT_PICKER_SUBCALL",
    "pick_projects",
    "render_project_candidates_block",
]
