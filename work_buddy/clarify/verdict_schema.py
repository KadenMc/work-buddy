"""Shared JSON schemas for Clarify (formerly triage) verdicts.

Two schemas live here, paired with two reading paths in the
presentation builder:

- :data:`VERDICT_SCHEMA` — the legacy 5-action shape (Slice 1 and
  earlier). Kept as the literal definition because pool entries
  written before Slice 3 still parse/validate against it. New verdict
  passes do NOT use this schema; the migration script re-parses old
  entries against the new schema.
- :data:`MULTI_RECORD_VERDICT_SCHEMA` — the GTD-shaped multi-record
  schema (Slice 3 onward). One captured item produces N records; each
  record is typed by ``destination`` and carries a destination-specific
  proposal. A ``refusal`` field is mutually exclusive with ``records``
  for low-confidence verdicts that need human routing.

Both schemas live in this single module to avoid drift between
Clarify producers (``journal_triage_scan``, ``inline_triage_scan``)
and ``triage_submit``. The submit allowlist (``_shape_verdict`` in
``background.py``) accepts the union of both shapes' fields during
the Slice 1→3 migration window.
"""

from __future__ import annotations

from typing import Any

from work_buddy.clarify.items import TRIAGE_ACTIONS, TRIAGE_DESTINATIONS


# ---------------------------------------------------------------------------
# Legacy single-action schema (Slice 1 and earlier).
# ---------------------------------------------------------------------------

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommended_action": {
            "type": "string",
            "enum": list(TRIAGE_ACTIONS),
            "description": (
                "One of: create_task, record_into_task, leave, close, group."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "One to three sentences explaining the decision.",
        },
        "group_intent": {
            "type": "string",
            "description": (
                "Short noun phrase (≤8 words) naming the underlying intent. "
                "Shown as the card title in the Review view."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0.0–1.0 self-assessed confidence.",
        },
        "suggested_task_text": {
            "type": "string",
            "description": (
                "Required when recommended_action == 'create_task'. "
                "A concise task title suitable for the master task list."
            ),
        },
        "target_task_id": {
            "type": "string",
            "description": (
                "Required when recommended_action == 'record_into_task'. "
                "Must match a task_id from the user's current-context block."
            ),
        },
        "related_item_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Required when recommended_action == 'group'. Other "
                "pool-item IDs this item clusters with."
            ),
        },
    },
    "required": ["recommended_action", "rationale", "group_intent"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Slice 3 multi-record schema.
# ---------------------------------------------------------------------------

# Sub-schemas for destination-specific proposals. Pulled out as named
# dicts so the test fixtures can assemble valid records without
# re-deriving the structure.

_TASK_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # ---- Slice 2 metadata fields (REQUIRED on new task records) ----
        # These fields land on Obsidian's task store via the
        # ``tasks_create`` capability — see Slice 2 (commit be29eed).
        "kind": {
            "type": "string",
            "enum": ["task", "project", "stub", "habit", "reminder"],
            "description": (
                "GTD task kind. ``task`` is the default; ``project`` "
                "is multi-step work; ``stub`` is a placeholder; "
                "``habit``/``reminder`` are recurring/temporal."
            ),
        },
        "outcome_text": {
            "type": "string",
            "description": (
                "What the user wants to be true once this is done. "
                "Often the same as task title for simple tasks; for "
                "projects, may differ (title='Ship feature X', "
                "outcome='X is in production with monitoring')."
            ),
        },
        "next_action_text": {
            "type": "string",
            "description": (
                "The very next concrete physical action. Optional — "
                "leave blank if pickup-time evaluation will produce it."
            ),
        },
        "definition_of_done": {
            "type": "string",
            "description": (
                "How the user will know it's complete. Optional for "
                "sparse captures."
            ),
        },
        "creation_effort": {
            "type": "string",
            "enum": ["sparse", "developed"],
            "description": (
                "How thoroughly the agent was able to develop this "
                "task at capture time. ``sparse`` means \"just a "
                "title; pickup-time evaluation will need to develop "
                "it.\" ``developed`` means the metadata is filled in."
            ),
        },
        "user_involvement": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": (
                "Calibration on whether the user was actively engaged "
                "or the agent inferred most of this. Affects the "
                "pickup-time evaluation signal — high involvement + "
                "fresh capture → execute directly."
            ),
        },
        "creation_provenance": {
            "type": "string",
            "enum": ["manual", "journal-inferred", "chrome-inferred",
                     "inline-inferred", "agent-derived"],
            "description": (
                "What flow produced this task. Affects pickup-time "
                "evaluation."
            ),
        },
        # ---- Deadline / dependency hints (Slice 3 cheap extraction) ----
        # Populated by the Haiku deadline-extraction step before the
        # Sonnet verdict pass. Slice 8 will use these for resurfacing.
        "has_deadline": {
            "type": "boolean",
            "description": (
                "Did the captured text mention a deadline? Detected by "
                "the cheap deadline-extraction step before the verdict."
            ),
        },
        "deadline_date": {
            "type": ["string", "null"],
            "description": (
                "ISO8601 date string when ``has_deadline`` is true. "
                "Null otherwise. May be approximate (e.g., 'next week' "
                "→ a representative date the agent picked)."
            ),
        },
        "has_dependency": {
            "type": "boolean",
            "description": (
                "Did the captured text mention something that must "
                "happen before this can start (waiting on someone, "
                "blocked by another task, etc.)?"
            ),
        },
        "dependency_hint": {
            "type": ["string", "null"],
            "description": (
                "Free-text description of the dependency when "
                "``has_dependency`` is true. Null otherwise."
            ),
        },
        # ---- Routing fields ----
        "suggested_task_text": {
            "type": "string",
            "description": (
                "Concise task title for the master task list. The "
                "thing that gets written next to the [ ] checkbox."
            ),
        },
        "target_task_id": {
            "type": ["string", "null"],
            "description": (
                "Set when this record is recording INTO an existing "
                "task (the old record_into_task case). Must match a "
                "task_id from the user's current-context block. Null "
                "for new tasks."
            ),
        },
        # ---- Forward-compat optional fields (Slices 4, 5b) ----
        "tier": {
            "type": ["string", "null"],
            "description": (
                "Slice 4 forward-compat: automation tier (1-3). "
                "Optional; Slice 4 populates this."
            ),
        },
        "risk_profile": {
            "type": ["object", "null"],
            "description": (
                "Slice 4: composite risk dimensions + amplifiers. "
                "Four dimensions (financial_cents, privacy, accuracy, "
                "compute) + three amplifiers (reversibility, "
                "regret_potential, inference_uncertainty). The "
                "automation/risk resolver reads this against the "
                "user's tolerance to compute the operating tier."
            ),
            # Anthropic strict structured-output mode rejects object-type
            # fields without an explicit ``additionalProperties: false``,
            # even when the field itself is nullable. Authored at Slice 4
            # without this; surfaced as a 400 on the first live verdict
            # call against Sonnet/Haiku once the schema's total size grew
            # past the threshold where Anthropic deeply re-validates.
            "additionalProperties": False,
            "properties": {
                "financial_cents": {
                    "type": "integer",
                    "description": (
                        "Estimated max spend in cents if the agent acts "
                        "autonomously. 0 for non-spending tasks."
                    ),
                },
                "privacy": {
                    "type": "string",
                    "enum": ["none", "internal", "public"],
                    "description": (
                        "Action-exposure level. ``none``: never leaves "
                        "the user's local data. ``internal``: shared with "
                        "trusted external services (calendar, vault). "
                        "``public``: visible to anyone (sent email, "
                        "public commit, posted to web)."
                    ),
                },
                "accuracy": {
                    "type": "string",
                    "enum": ["low_stakes", "consequential", "critical"],
                    "description": (
                        "Blast radius if the output is wrong. "
                        "``low_stakes``: tab close, summary draft — "
                        "trivial to reverse. ``consequential``: "
                        "structural change, code refactor — costly to "
                        "fix. ``critical``: medical, legal, financial "
                        "decisions — must be right the first time."
                    ),
                },
                "compute": {
                    "type": "string",
                    "enum": ["instant", "background", "expensive"],
                    "description": (
                        "Resource consumption. ``instant``: <5s. "
                        "``background``: <5min cron-class. "
                        "``expensive``: full ML training run, large "
                        "Anthropic call sweep, ≥$1 cost."
                    ),
                },
                "reversibility": {
                    "type": "string",
                    "enum": ["trivial", "moderate", "irreversible"],
                    "description": (
                        "How hard is it to undo this action? Sending an "
                        "email is irreversible; closing a tab is "
                        "trivial; editing a file is moderate (git "
                        "revert is possible but disruptive)."
                    ),
                },
                "regret_potential": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": (
                        "How bad would the user feel if this action "
                        "fired wrongly? Sending email under user's "
                        "identity is high-regret regardless of "
                        "accuracy. Closing tabs is low-regret. Setting "
                        "a calendar appointment is medium."
                    ),
                },
                "inference_uncertainty": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": (
                        "Your calibration on whether you correctly "
                        "understand the user's intent here. Default to "
                        "``medium`` for any task you didn't explicitly "
                        "see the user invoke. Set ``high`` when you're "
                        "guessing about which project this belongs to, "
                        "what tone is expected, or whether the user "
                        "wants this done at all. Set ``low`` only when "
                        "the user message itself unambiguously "
                        "specifies the action and target."
                    ),
                },
            },
        },
        "required_contexts": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "DEPRECATED — kept for Slice 1→4 backwards compat. "
                "Slice 5a splits into agent_required_contexts + "
                "user_required_contexts; Clarify should populate the "
                "two new fields and leave this null."
            ),
        },
        "agent_required_contexts": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "Slice 5a: tokens describing the environment the AGENT "
                "needs to act on this task autonomously. Examples: "
                "``@filesystem`` (read/write project files), "
                "``@vault`` (Obsidian bridge), ``@email_send`` (send "
                "email under the user's identity), ``@web_public`` "
                "(WebFetch / WebSearch). Empty array = no agent-side "
                "requirements (the user does this work). The resolver "
                "treats unknown tokens as user-only (forward-compat)."
            ),
        },
        "user_required_contexts": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "Slice 5a: tokens describing the environment the USER "
                "needs to be in. Examples: ``@user_workstation`` (at "
                "their dev machine), ``@phone_voice`` (making a call), "
                "``@user_creds`` (signed into a portal), ``@in_person`` "
                "(physically present somewhere). Empty array = no "
                "user-side requirements (the agent does this work "
                "autonomously). Two lists may overlap (e.g., "
                "``@chrome_active`` requires both)."
            ),
        },
        "required_contexts_source": {
            "type": ["string", "null"],
            "enum": ["agent_inferred", "user_authored", None],
            "description": (
                "Slice 5a provenance for the two context lists. "
                "Clarify writes ``agent_inferred``; the dashboard "
                "flips to ``user_authored`` once the user edits the "
                "list so future Clarify re-runs don't clobber the "
                "edit."
            ),
        },
        # ---- Project picker (sub-LLM evidence + verdict's pick) -----
        "project_tag": {
            "type": ["string", "null"],
            "description": (
                "The project this task belongs to, as a slug under "
                "``#projects/<slug>``. Null means no project — a "
                "first-class option, not a fallback. The verdict "
                "(THIS prompt) decides this by reasoning over "
                "``project_candidates`` (from the project-picker "
                "sub-LLM) plus broader context. Lean toward null "
                "when genuinely uncertain; declining to assign a "
                "project is preferable to a wrong assignment. The "
                "downstream ``triage_submit``/``task_create`` path "
                "applies ``#projects/<slug>`` iff this is non-null."
            ),
        },
        "project_candidates": {
            "type": ["array", "null"],
            "description": (
                "Audit field: the ranked candidate list from the "
                "project-picker sub-LLM. Each entry has "
                "``project_tag`` (slug or null), ``confidence`` "
                "(0-1), and ``rationale``. The verdict pre-pass "
                "interpolates these into the user prompt as "
                "evidence; the verdict reasons over them and sets "
                "``project_tag`` accordingly. Pass through verbatim "
                "from the user message unless overriding (drop "
                "spurious candidates allowed; never invent new ones)."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project_tag": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["project_tag", "confidence", "rationale"],
            },
        },
    },
    "required": ["suggested_task_text"],
    "additionalProperties": False,
}

_REFERENCE_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "Short summary of the reference content. Slice 6 "
                "wires up actual filing; Slice 3 just persists the "
                "summary so the user can see what was inferred."
            ),
        },
        "suggested_path": {
            "type": ["string", "null"],
            "description": (
                "Slice 6 forward-compat: where in the vault the "
                "reference should land. Optional — Slice 6 will "
                "infer this when not pre-supplied."
            ),
        },
    },
    "required": ["summary"],
    "additionalProperties": False,
}

_CALENDAR_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Calendar event title.",
        },
        "datetime": {
            "type": ["string", "null"],
            "description": (
                "ISO8601 datetime when known. Null for ambiguous "
                "captures (e.g., 'next Thursday' the agent couldn't "
                "resolve to a concrete date)."
            ),
        },
        "duration_minutes": {
            "type": ["integer", "null"],
            "description": "Optional duration in minutes.",
        },
        "all_day": {
            "type": "boolean",
            "description": (
                "Whole-day event (e.g., birthdays, holidays). "
                "Defaults to false."
            ),
        },
    },
    "required": ["title"],
    "additionalProperties": False,
}

_RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "enum": list(TRIAGE_DESTINATIONS),
            "description": (
                "Which destination this record routes to. ``task`` "
                "and ``delete`` are fully wired in Slice 3; "
                "``reference`` and ``calendar_only`` parse but are "
                "not yet executed (Slices 6, 10)."
            ),
        },
        "task_proposal": {
            **_TASK_PROPOSAL_SCHEMA,
            "description": (
                "Required when destination == 'task'. Carries the "
                "Slice 2 metadata + deadline/dependency hints."
            ),
        },
        "reference_proposal": {
            **_REFERENCE_PROPOSAL_SCHEMA,
            "description": (
                "Required when destination == 'reference'. Slice 6 "
                "fully wires reference filing."
            ),
        },
        "calendar_proposal": {
            **_CALENDAR_PROPOSAL_SCHEMA,
            "description": (
                "Required when destination == 'calendar_only'. "
                "Slice 10 fully wires calendar destinations."
            ),
        },
        "delete_reason": {
            "type": "string",
            "description": (
                "Required when destination == 'delete'. One sentence "
                "explaining why it's safe to drop. Persisted on the "
                "pool entry for audit."
            ),
        },
        "linked_record_indexes": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Slice 10 forward-compat: indexes of other records "
                "in this verdict's records[] that this record is "
                "linked to. Used for the gift-task ↔ birthday-event "
                "case (one capture → linked task + calendar)."
            ),
        },
    },
    "required": ["destination"],
    "additionalProperties": False,
}

_REFUSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "What the agent needs from the user to proceed. The "
                "Resolution Surface renders this as a clarification "
                "card (Slice 3 wires the clarification resolution-"
                "type to consume this field)."
            ),
        },
        "missing_context": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "Optional list of named context dimensions that "
                "would unblock the refusal (e.g., 'project', "
                "'deadline', 'who'). Slice 4+ will use these to "
                "structure the redirect prompt."
            ),
        },
    },
    "required": ["question"],
    "additionalProperties": False,
}

_PIPELINE_BLOCKER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": (
                "One of the typed blocker kinds (see "
                "work_buddy.clarify.resolution.PIPELINE_BLOCKERS)."
            ),
        },
        "detail": {
            "type": ["string", "null"],
            "description": (
                "Free-text elaboration of the blocker. Surfaced as "
                "the badge tooltip on the Resolution Surface card."
            ),
        },
    },
    "required": ["kind"],
    "additionalProperties": False,
}

MULTI_RECORD_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {
            "type": "string",
            "description": "One to three sentences explaining the verdict.",
        },
        "group_intent": {
            "type": "string",
            "description": (
                "Short noun phrase (≤8 words) naming the underlying "
                "intent. Shown as the card title in the Resolution "
                "Surface."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0.0–1.0 self-assessed confidence.",
        },
        "records": {
            "type": "array",
            "items": _RECORD_SCHEMA,
            "description": (
                "Zero or more records produced from this captured "
                "item. Multi-record output handles the 'birthday + "
                "gift' case: one captured item produces a calendar "
                "event AND a linked task. Empty array means \"no "
                "record produced\" (equivalent to the old leave "
                "action). Mutually exclusive with refusal."
            ),
        },
        "refusal": {
            **_REFUSAL_SCHEMA,
            "description": (
                "Set when confidence is too low to commit to records. "
                "The Resolution Surface renders this as a "
                "clarification card; the user's answer re-queues the "
                "Clarify pass. Mutually exclusive with records."
            ),
        },
        "pipeline_blocker": {
            **_PIPELINE_BLOCKER_SCHEMA,
            "description": (
                "Optional typed blocker explaining why the agent "
                "stopped (ROADMAP §3.3). Surfaced as a typed badge "
                "on the Resolution Surface card."
            ),
        },
    },
    "required": ["rationale", "group_intent"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Submit-payload extractors
# ---------------------------------------------------------------------------


def verdict_to_submit_kwargs(verdict: dict[str, Any]) -> dict[str, Any]:
    """Filter a parsed verdict down to ``triage_submit``'s named kwargs.

    Handles BOTH schemas during the Slice 1→3 migration window:

    - Multi-record verdicts pass ``records``, ``refusal``,
      ``pipeline_blocker`` (the new fields).
    - Legacy verdicts pass ``recommended_action``,
      ``suggested_task_text``, ``target_task_id``,
      ``related_item_ids`` (the pre-Slice-3 fields).
    - Both shapes share ``rationale``, ``group_intent``,
      ``confidence``.

    ``triage_submit`` accepts both shapes; it relies on
    ``_shape_verdict`` (in background.py) for storage normalization
    and on ``_build_presentation_from_pool`` (in the read path) to
    pick the right rendering branch.
    """
    allowed = {
        # Shared (both schemas)
        "rationale",
        "group_intent",
        "confidence",
        # Multi-record (Slice 3+)
        "records",
        "refusal",
        "pipeline_blocker",
        # Legacy (Slice 1)
        "recommended_action",
        "suggested_task_text",
        "target_task_id",
        "related_item_ids",
    }
    return {k: v for k, v in verdict.items() if k in allowed and v is not None}


def is_multi_record_verdict(verdict: dict[str, Any]) -> bool:
    """Discriminator: does this verdict use the Slice 3 multi-record shape?

    True when ``verdict.records`` is a list (possibly empty) or
    ``verdict.refusal`` is a dict. False otherwise (legacy, raw, or
    malformed).
    """
    if not isinstance(verdict, dict):
        return False
    if isinstance(verdict.get("records"), list):
        return True
    if isinstance(verdict.get("refusal"), dict):
        return True
    return False
