"""Per-session capability ACL for gateway-enforced tool access.

work-buddy's MCP surface exposes a small fixed set of top-level tools
(``wb_init``, ``wb_run``, ``wb_search``, ``wb_status``, ``wb_advance``,
``wb_step_result``); every domain capability (``task_briefing``,
``project_get``, etc.) is dispatched *through* ``wb_run``. That means
restricting which capabilities a given MCP client can invoke cannot be
done at LM Studio's ``integrations.allowed_tools`` layer — from its
vantage point there's only one tool (``wb_run``). The whitelist has to
live on the work-buddy gateway.

This module provides a simple in-process map from an agent session id
to a frozen set of capability names that session is permitted to
dispatch via ``wb_run``. ``llm_with_tools`` sets the ACL before firing
its LM Studio request and clears it afterward; the gateway's ``wb_run``
and ``wb_search`` paths consult it to enforce / filter accordingly.

Design notes:
- **Default-open for normal agents.** When a session has no ACL
  registered, the gateway behaves exactly as before (all registered
  capabilities accessible, subject to consent). Only sessions that
  opt-in by registering an ACL are constrained.
- **In-process only.** The MCP gateway and ``llm_with_tools`` run in
  the same process (the MCP sidecar service), so a plain module-level
  dict is sufficient. No IPC, no persistence.
- **No TTL.** The ACL's lifetime is bounded by the try/finally around
  the LM Studio request in ``llm_with_tools``. Callers must clear on
  completion; a stale ACL would constrain later calls through the
  same session id.
"""

from __future__ import annotations

from typing import Any, Iterable


_SESSION_ACL: dict[str, frozenset[str]] = {}


def set_session_acl(session_id: str, allowed_capabilities: Iterable[str]) -> None:
    """Register a capability whitelist for ``session_id``.

    While the ACL is set, ``wb_run`` rejects any capability not in
    ``allowed_capabilities`` and ``wb_search`` filters its results to
    the allowed set. Replacing an existing ACL is allowed (overwrites).
    """
    _SESSION_ACL[session_id] = frozenset(allowed_capabilities)


def clear_session_acl(session_id: str) -> None:
    """Remove the ACL for ``session_id``. Idempotent."""
    _SESSION_ACL.pop(session_id, None)


def get_session_acl(session_id: str | None) -> frozenset[str] | None:
    """Return the ACL for ``session_id``, or None when unconstrained.

    A None return means "no ACL registered — default-open". An empty
    frozenset would mean "nothing is allowed" (unusual but legal).
    """
    if not session_id:
        return None
    return _SESSION_ACL.get(session_id)


def any_acl_registered() -> bool:
    """True if *any* session in this process currently has an ACL.

    Used to fail-closed when a call arrives with an unresolved session
    id but an ACL-scoped llm_with_tools run is active in the same
    process — the safe bet is "this is probably the ACL-scoped caller
    whose session we failed to resolve", so we reject rather than
    silently default-open.
    """
    return bool(_SESSION_ACL)


def is_capability_allowed(session_id: str | None, capability: str) -> bool:
    """Check whether ``capability`` is allowed for ``session_id``.

    Semantics:
    - ``session_id`` resolves to a registered ACL → membership check.
    - ``session_id`` has no ACL registered AND no ACL exists anywhere
      in the process → default-open (normal agents, the common path).
    - ``session_id`` is None/unresolved AND an ACL is registered
      somewhere → **fail-closed**. This is the defense against
      session-resolution races where a local-model call can't be
      tied back to its llm_with_tools ACL via ctx; treating those
      as unconstrained would let the model call anything in the
      registry. Genuinely unrelated default-open callers should
      always be able to resolve their session via wb_init or the
      X-Work-Buddy-Session header — if they can't, something is
      wrong and silently permitting the call is the wrong bet.
    """
    acl = get_session_acl(session_id)
    if acl is not None:
        return capability in acl
    # No ACL for this (possibly None) session id.
    if session_id is None and any_acl_registered():
        return False
    return True


def filter_search_results(
    results: list[dict[str, Any]],
    session_id: str | None,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Apply the session's ACL filter to ``wb_search`` results.

    Returns:
        - the raw list unchanged when there's no ACL in effect
        - the filtered list (still a bare list) when an ACL is in
          effect but nothing got hidden
        - a ``dict`` of ``{results, _acl_filtered, _acl_hidden_count,
          _acl_notice}`` when the ACL trimmed at least one result,
          so the caller knows the empty-or-short list reflects
          authorization, not query mismatch

    Fail-closed bookend: when ``session_id`` is None AND an ACL is
    active anywhere in the process, we treat the ACL as empty and
    surface the trim. This mirrors ``is_capability_allowed``'s
    refuse-by-default stance for unresolved sessions.

    Extracted from the gateway's ``wb_search`` handler so the response
    shape is unit-testable without a live MCP context.
    """
    acl = get_session_acl(session_id)
    acl_active = acl is not None
    if not acl_active and session_id is None and any_acl_registered():
        acl = frozenset()
        acl_active = True
    if not acl_active:
        return results

    before = len(results)
    filtered = [r for r in results if r.get("name") in acl]
    hidden = before - len(filtered)
    if hidden == 0:
        return filtered
    return {
        "results": filtered,
        "_acl_filtered": True,
        "_acl_hidden_count": hidden,
        "_acl_notice": (
            f"{hidden} result(s) matched the query but were hidden by "
            f"your session ACL (the caller restricted this session to "
            f"a named preset). Further searches with reworded queries "
            f"will not reveal them — stick to capabilities you can see."
        ),
    }
