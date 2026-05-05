"""Slice 5a: action-context resolution layer.

Tasks declare what environments **the agent** and **the user** each
need to fire an action.  The resolver answers "who can act now?"
against the live tool-status cache (``work_buddy.tools._TOOL_STATUS``,
populated by ``probe_all`` and refreshed by the dashboard every 60s).

Design (per ROADMAP §3.2 + Slice 5a task note):

* Two ordered lists per task — ``agent_required_contexts`` and
  ``user_required_contexts``.  May overlap, may diverge.
* Context tokens (``@filesystem``, ``@email_send``, …) live in
  :data:`CONTEXT_REGISTRY` mapped to the tool IDs that satisfy them.
* Three sentinel meanings on a token's mapping:

  - ``None`` — user-only context (``@physical``, ``@user_creds``,
    ``@cluster``).  Agent never satisfies regardless of tool state.
  - ``[]``   — universally available (``@filesystem``, ``@web_public``,
    ``@llm``).  Both actors satisfy without a probe.
  - ``["tool_id", ...]`` — agent satisfies iff ALL listed tool IDs are
    available *now*; the user is assumed to satisfy when they're in
    the relevant context.

* Unknown tokens (forward-compat per ROADMAP P5 — Clarify may invent
  one before the registry catches up) are treated as user-only and
  added to ``unmet`` on the agent side.

* Resolution is **lazy** (ROADMAP P7).  Operating tier and
  who-can-act are computed every read; storing a stale answer would
  defeat the live tool-status feedback loop the dashboard relies on.

The Slice 5a function plugs into ``automation.risk.resolve_achievable_tier``
(the ``contexts`` kwarg).  When the agent can't satisfy its required
contexts, the achievable ceiling drops to 1 — the agent can suggest
but cannot autonomously execute.

Example::

    >>> decision = resolve_who_can_act(
    ...     agent_required=["@filesystem"],
    ...     user_required=["@user_workstation"],
    ... )
    >>> decision.agent
    True
    >>> decision.user
    True
    >>> decision.blocked
    False
    >>> decision.agent_handoff_eligible
    False

When the agent lacks a tool but the user is in context, ``blocked`` is
False and ``agent_handoff_eligible`` is True — the surface should
render a *handoff* card ("agent prepared X; user takes from here")
rather than a "task waiting" badge.  This is the "agent is blocked,
not user" framing from ROADMAP §3.2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from work_buddy.tools import is_tool_available


# ---------------------------------------------------------------------------
# Registry: context token → list of tool IDs that satisfy it for the agent
# ---------------------------------------------------------------------------
#
# Sentinels:
#   None : user-only context (no tool ever satisfies)
#   []   : universally available (both actors satisfy without a probe)
#   [...]: agent satisfies iff ALL listed tool IDs are available
#
# Tool IDs come from work_buddy.tools._register_default_probes — keep in
# sync if a probe is renamed/removed.

CONTEXT_REGISTRY: dict[str, list[str] | None] = {
    # ── User-only contexts (no probe satisfies the agent) ───────────
    "@physical":         None,   # body-in-space; never agent
    "@in_person":        None,   # synchronous human presence
    "@phone_voice":      None,   # voice calls
    "@user_creds":       None,   # banking, CRA, healthcare portals, 2FA
    "@user_workstation": None,   # user-only workstation access
    "@cluster":          None,   # HPC; agent has no SSH today

    # ── Universally available (no probe needed; both actors satisfy)
    "@filesystem":   [],   # agent always has FS via tooling; user too
    "@web_public":   [],   # agent has WebFetch built in; user has browser
    "@llm":          [],   # agent always has Anthropic; LM Studio is offload
    "@github":       [],   # agent has gh CLI / WebFetch; user has browser

    # ── Probe-gated contexts ────────────────────────────────────────
    "@vault":         ["obsidian"],
    "@email_send":    ["thunderbird"],
    "@email_read":    ["thunderbird"],
    "@chrome_active": ["chrome_extension"],
}


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WhoCanActDecision:
    """Result of :func:`resolve_who_can_act`.

    Attributes:
        agent: True iff the agent satisfies every token in
            ``agent_required_contexts``.
        user: True iff the user satisfies every token in
            ``user_required_contexts`` (the resolver assumes the user
            satisfies their own contexts when they declare them; engage-
            view filtering against ``user_current_contexts`` is a
            separate layer — see :func:`user_satisfies_against`).
        blocked: ``not agent and not user``.  Both sides cannot proceed.
        agent_unmet: tokens the agent could not satisfy (ordered for
            stable rendering).
        user_unmet: tokens the user could not satisfy.
        agent_handoff_eligible: ``not agent and user`` — user can act
            but the agent prepared nothing.  Surfaces should render a
            handoff card per ROADMAP §3.2 ("agent is blocked, not
            user").
        unknown_tokens: tokens absent from :data:`CONTEXT_REGISTRY`.
            Forward-compat: Clarify may invent a token before the
            registry adds it; we still resolve (treat as user-only)
            but report so the dashboard can warn.
    """

    agent: bool
    user: bool
    blocked: bool
    agent_unmet: tuple[str, ...] = field(default_factory=tuple)
    user_unmet: tuple[str, ...] = field(default_factory=tuple)
    agent_handoff_eligible: bool = False
    unknown_tokens: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_context_list(raw: str | list | tuple | None) -> list[str]:
    """Coerce a stored context-list value into a ``list[str]``.

    Accepts the shapes the store / Clarify produces: a JSON-encoded
    string, a Python list/tuple of strings, or ``None`` (legacy /
    not-yet-classified — returns ``[]``).  Unknown / non-string
    members are dropped silently rather than raising — better a quiet
    drop than a crash on a live engage flow.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [str(t) for t in data if isinstance(t, str)]
    if isinstance(raw, (list, tuple)):
        return [str(t) for t in raw if isinstance(t, str)]
    return []


def serialize_context_list(tokens: Iterable[str] | None) -> str | None:
    """Inverse of :func:`parse_context_list` for store writes.

    Returns ``None`` for ``None`` (preserves NULL in the column) and
    a deterministic JSON-encoded string otherwise.  Empty list is
    encoded as ``"[]"`` so the caller can distinguish "no contexts
    declared" from "not yet classified."
    """
    if tokens is None:
        return None
    seen: list[str] = []
    for t in tokens:
        if isinstance(t, str) and t not in seen:
            seen.append(t)
    return json.dumps(seen)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_who_can_act(
    agent_required: Iterable[str] | str | None,
    user_required: Iterable[str] | str | None,
    *,
    tool_status: Mapping[str, Any] | None = None,
) -> WhoCanActDecision:
    """Compute who-can-act-now for a task.

    Pure-ish function: the only I/O is the ``is_tool_available`` cache
    lookup, which is in-memory and sub-millisecond.  Unit tests inject
    ``tool_status`` to bypass the global cache; production callers
    leave it None and let the resolver consult the live state.

    The user side is treated as **always satisfiable for declared
    contexts** at this layer.  "Is the user *currently* in this
    context?" is the engage-view filter (see
    :func:`user_satisfies_against`) — a separate concern from "did the
    user declare these contexts as required."

    Args:
        agent_required: tokens the agent must currently satisfy.
            Accepts a list/tuple/None or the raw JSON string from
            ``task_metadata.agent_required_contexts``.
        user_required: same but for the user side.
        tool_status: optional dict overriding the live tool cache.
            Each entry is read with ``.get(tool_id, {}).get('available',
            False)`` so the shape matches the persisted
            ``<data_root>/runtime/tool_status.json``.

    Returns:
        :class:`WhoCanActDecision` with booleans + per-side unmet
        token lists + the ``agent_handoff_eligible`` flag.
    """
    agent_tokens = parse_context_list(agent_required)
    user_tokens = parse_context_list(user_required)

    agent_unmet: list[str] = []
    user_unmet: list[str] = []
    unknown: list[str] = []

    for tok in agent_tokens:
        ok, was_unknown = _agent_satisfies(tok, tool_status=tool_status)
        if was_unknown:
            unknown.append(tok)
        if not ok:
            agent_unmet.append(tok)

    for tok in user_tokens:
        ok, was_unknown = _user_satisfies_declared(tok)
        if was_unknown and tok not in unknown:
            unknown.append(tok)
        if not ok:
            user_unmet.append(tok)

    agent = not agent_unmet
    user = not user_unmet
    blocked = not agent and not user
    handoff = (not agent) and user

    return WhoCanActDecision(
        agent=agent,
        user=user,
        blocked=blocked,
        agent_unmet=tuple(agent_unmet),
        user_unmet=tuple(user_unmet),
        agent_handoff_eligible=handoff,
        unknown_tokens=tuple(unknown),
    )


def user_satisfies_against(
    user_required: Iterable[str] | str | None,
    user_current: Iterable[str] | None,
) -> tuple[bool, list[str]]:
    """Engage-view filter: does the user's declared current context
    cover the task's required user-context list?

    Returns ``(satisfied, unmet_tokens)``.  Empty required list is
    trivially satisfied.  This is *separate* from
    :func:`resolve_who_can_act` because the engage view runs it on
    every render against a transient localStorage value, whereas
    ``resolve_who_can_act`` only knows about declared task context.
    """
    required = parse_context_list(user_required)
    if not required:
        return True, []
    current = set(parse_context_list(list(user_current) if user_current else None))
    unmet = [t for t in required if t not in current]
    return (not unmet), unmet


def list_known_context_tokens() -> list[str]:
    """Stable-ordered list of every context token the registry knows.

    Used by the Clarify prompt builder + the dashboard's context-
    declaration UI so they see the same vocabulary the resolver does.
    Sorted for deterministic UI rendering.
    """
    return sorted(CONTEXT_REGISTRY.keys())


def context_tokens_blocked_by_tool(tool_id: str) -> list[str]:
    """Reverse lookup: which context tokens currently require ``tool_id``?

    Powers the daily-log nudge ("X tasks blocked on @email_send — set
    up email integration?") and Slice 11's reactive surfacing.
    """
    out: list[str] = []
    for tok, mapped in CONTEXT_REGISTRY.items():
        if mapped is None:
            continue  # user-only context; tool-state irrelevant
        if tool_id in mapped:
            out.append(tok)
    return sorted(out)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _agent_satisfies(
    token: str,
    *,
    tool_status: Mapping[str, Any] | None,
) -> tuple[bool, bool]:
    """Return ``(satisfied, was_unknown)`` for the agent side.

    Sentinels in :data:`CONTEXT_REGISTRY`:
        None  → user-only context; agent never satisfies.
        []    → universal; agent always satisfies (no probe).
        list  → agent satisfies iff every tool in the list is available.

    Unknown tokens (not in the registry) → treated as user-only +
    flagged ``was_unknown=True``.
    """
    if token not in CONTEXT_REGISTRY:
        return False, True

    mapped = CONTEXT_REGISTRY[token]
    if mapped is None:
        return False, False
    if not mapped:  # empty list → universally available
        return True, False

    if tool_status is not None:
        for tid in mapped:
            entry = tool_status.get(tid)
            available = bool(entry.get("available")) if isinstance(entry, Mapping) else False
            if not available:
                return False, False
        return True, False

    # Live cache path
    return all(is_tool_available(tid) for tid in mapped), False


def _user_satisfies_declared(token: str) -> tuple[bool, bool]:
    """User-side satisfaction at the *declaration* layer.

    The user is assumed to satisfy any context they (or Clarify on
    their behalf) declared as required — this resolver answers "given
    this task's user-context list, can the user theoretically act?"
    and the answer is always yes for known + universal tokens.
    "Right now?" is :func:`user_satisfies_against`.

    Unknown tokens still flag ``was_unknown=True`` so the dashboard
    can warn — but they don't block (we trust Clarify's intent).
    """
    if token not in CONTEXT_REGISTRY:
        return True, True
    return True, False
