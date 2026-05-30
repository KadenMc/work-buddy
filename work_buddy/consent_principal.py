"""Consent principals — *who* a consent check resolves against.

Every consent resolution belongs to exactly one **principal**: the human
agent session that initiated an action, the headless sidecar doing its own
autonomous work, or a retry-queue replay of a previously-queued op. Binding
a principal makes "which ``consent.db`` does this check read" an *explicit*
fact rather than an ambient process default.

That ambient default is the bug class this module eliminates: the MCP server
runs under the sidecar's synthetic ``WORK_BUDDY_SESSION_ID``, so a consent
check with no explicit principal would resolve against the *sidecar's* DB —
where a stale ``workflow_run:*`` blanket could authorize an unrelated agent's
operation. With a principal bound, an agent's check resolves against the
agent's own DB and never consults the sidecar's.

Bind one via the :func:`consent_principal` context manager, or pass
``principal=`` explicitly to :meth:`work_buddy.consent.ConsentCache.is_granted`.
The three factories — :func:`human_agent`, :func:`sidecar_self`,
:func:`replay_of` — are the only sanctioned ways to construct a principal.

The carry policy is a *property of the kind*, not a free-form flag:
``REPLAY`` principals ride individual op-grants only (a workflow grant active
when an op was queued must not time-travel to authorize a later replay);
``HUMAN_AGENT`` and ``SIDECAR`` may ride their own live workflow grants.

See the ``notifications/consent`` knowledge unit, section "The three consent
principals", for the full model.
"""

from __future__ import annotations

import enum
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


class PrincipalKind(enum.Enum):
    """Who is performing a consent-gated operation."""

    HUMAN_AGENT = "human_agent"   # an interactive Claude session
    SIDECAR = "sidecar"           # the headless daemon's own autonomous ops
    REPLAY = "replay"             # retry-queue replay of a previously-queued op


@dataclass(frozen=True)
class ConsentPrincipal:
    """A consent principal: the session whose grants a check resolves against,
    plus the carry policy implied by its kind.
    """

    kind: PrincipalKind
    session_id: str

    @property
    def allows_workflow_carry(self) -> bool:
        """Whether a ``workflow_run:*`` / ``workflow_class:*`` grant in this
        principal's DB may authorize the operation.

        ``False`` only for replays: a workflow grant that was live when an op
        was queued must not time-travel to authorize a replay minutes or hours
        later. Live agent and sidecar principals ride their own workflow
        grants normally.
        """
        return self.kind is not PrincipalKind.REPLAY


# The active principal for the current execution context. ``None`` means no
# principal is bound — callers that resolve against this fall back to the
# legacy process-default resolution (see ``ConsentCache.is_granted``).
_active_principal: ContextVar[ConsentPrincipal | None] = ContextVar(
    "wb_active_consent_principal", default=None,
)


def current_principal() -> ConsentPrincipal | None:
    """Return the principal bound to the current context, or ``None``."""
    return _active_principal.get()


@contextmanager
def consent_principal(principal: ConsentPrincipal) -> Iterator[ConsentPrincipal]:
    """Bind ``principal`` for the duration of the ``with`` block.

    Nested ``@requires_consent`` checks inside the block resolve against this
    principal's consent DB. Reentrant and contextvar-backed, so it propagates
    naturally through the call stack within the same thread/task.
    """
    token = _active_principal.set(principal)
    try:
        yield principal
    finally:
        _active_principal.reset(token)


def human_agent(session_id: str) -> ConsentPrincipal:
    """An interactive Claude session that initiated an operation."""
    return ConsentPrincipal(PrincipalKind.HUMAN_AGENT, session_id)


def replay_of(session_id: str) -> ConsentPrincipal:
    """A retry-queue replay on behalf of ``session_id`` (no workflow carry)."""
    return ConsentPrincipal(PrincipalKind.REPLAY, session_id)


def sidecar_self() -> ConsentPrincipal:
    """The sidecar acting on its own behalf (cron jobs, ``agent_spawn``).

    Reads ``WORK_BUDDY_SESSION_ID`` here — the *only* sanctioned place the env
    var is consulted to pick a consent DB. In the sidecar/MCP-server process
    this is the synthetic ``sidecar-<hex>`` session.
    """
    from work_buddy.agent_session import _get_session_id
    return ConsentPrincipal(PrincipalKind.SIDECAR, _get_session_id())
