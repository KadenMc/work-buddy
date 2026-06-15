"""Session registry must key on the MCP session object's identity, not id().

Regression guard for a consent-routing bug: the gateway's
``_SESSION_REGISTRY`` mapped MCP-session → agent-session. When it was keyed
on ``id(ctx.session)`` (a CPython memory address, reused after GC) and never
evicted, a reconnected connection whose new session object landed at a freed
address would resolve to the *previous* session's agent id. Consent requests
were then stamped with the dead session's id (``callback_session_id``), so
out-of-band approvals wrote grants to a ``consent.db`` the live agent never
queried — re-prompting until ``_MAX_CONSENT_RETRIES`` was exhausted.

Keying on the session object itself via a ``WeakKeyDictionary`` fixes both
halves: object identity (a reused address is a distinct key, so no
cross-resolution) and auto-eviction (a dead session drops out, so an
unregistered connection resolves to ``None`` and ``_require_init`` forces a
clean re-init instead of inheriting a stale mapping).
"""

import gc
import weakref

import pytest

from work_buddy.mcp_server.tools import gateway


class _FakeSession:
    """Stand-in for an MCP ServerSession — any weak-referenceable object."""


class _FakeCtx:
    """Minimal Context stand-in exposing the ``.session`` the registry keys on."""

    def __init__(self, session):
        self.session = session


@pytest.fixture
def isolated_registry(monkeypatch):
    """Give each test a fresh registry and a no-op conductor.

    ``_register_session`` calls ``_conductor().reconcile_workflow_consent`` as
    a post-restart sweep; stub it so the test exercises only the registry
    semantics (and doesn't pull in networkx or touch a consent DB).
    """
    reg: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
    monkeypatch.setattr(gateway, "_SESSION_REGISTRY", reg)

    class _StubConductor:
        @staticmethod
        def reconcile_workflow_consent(_sid):
            return {"swept": False, "reason": "no_blanket"}

    monkeypatch.setattr(gateway, "_conductor", lambda: _StubConductor())
    return reg


def test_register_resolve_round_trip(isolated_registry):
    ctx = _FakeCtx(_FakeSession())
    gateway._register_session(ctx, "agent-A")
    assert gateway._resolve_session(ctx) == "agent-A"


def test_unregistered_session_resolves_none(isolated_registry):
    # A connection that never called wb_init must resolve to None so that
    # _require_init forces a fresh wb_init — never silently adopt an id.
    ctx = _FakeCtx(_FakeSession())
    assert gateway._resolve_session(ctx) is None


def test_dead_session_auto_evicts(isolated_registry):
    # Register, then drop every strong reference to the session object.
    ctx = _FakeCtx(_FakeSession())
    gateway._register_session(ctx, "agent-A")
    assert len(isolated_registry) == 1

    del ctx
    gc.collect()

    # The weak key is gone — a future object reusing that memory address
    # cannot inherit the stale mapping.
    assert len(isolated_registry) == 0


def test_distinct_sessions_do_not_cross_resolve(isolated_registry):
    ctx_a = _FakeCtx(_FakeSession())
    ctx_b = _FakeCtx(_FakeSession())
    gateway._register_session(ctx_a, "agent-A")
    gateway._register_session(ctx_b, "agent-B")

    assert gateway._resolve_session(ctx_a) == "agent-A"
    assert gateway._resolve_session(ctx_b) == "agent-B"

    # A third, unregistered connection resolves to None regardless of the
    # other live registrations.
    ctx_c = _FakeCtx(_FakeSession())
    assert gateway._resolve_session(ctx_c) is None


def test_reregistration_overwrites_same_session(isolated_registry):
    # Re-running wb_init on the same connection (e.g. corrected session id)
    # overwrites rather than accumulating.
    session = _FakeSession()
    ctx = _FakeCtx(session)
    gateway._register_session(ctx, "agent-A")
    gateway._register_session(ctx, "agent-A-corrected")

    assert gateway._resolve_session(ctx) == "agent-A-corrected"
    assert len(isolated_registry) == 1
