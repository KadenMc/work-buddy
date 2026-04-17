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

from typing import Iterable


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


def is_capability_allowed(session_id: str | None, capability: str) -> bool:
    """Check whether ``capability`` is allowed for ``session_id``.

    Returns True when no ACL is registered for the session (the
    default-open path). Returns True/False per membership when an ACL
    is present.
    """
    acl = get_session_acl(session_id)
    if acl is None:
        return True
    return capability in acl
