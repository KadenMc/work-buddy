"""Confirm the historical bubble: ``wb_run("consent_grant", ...)`` calls
landed their grants in the WRONG session DB.

Question this test is answering: the activity-ledger audit (#18) showed
28 historical agent invocations of ``wb_run("consent_grant", ...)`` —
every one returned ``result_summary: null``. Two hypotheses to
distinguish:

  H1.  The Python function ``consent.grant_consent`` is genuinely
       no-op — broken, silently failing. The "null" return is the
       symptom of a bug deep in the cache layer.

  H2.  The Python function works correctly; it returns ``None`` by
       design (it's a side-effect function with no return value). The
       grants DID write — they just wrote to the WRONG database
       because the gateway dispatch path for ``consent_grant`` never
       injected the agent's session id, so the cache fell back to its
       default DB (typically the MCP server's bootstrap session).

If H2 is correct, then the function is fine but the historical pattern
of writes landing in the bootstrap session DB (invisible to the
agent's subsequent ``consent_list`` and ``@requires_consent`` checks)
explains why those 28 calls accomplished nothing. The capability
deletion in commit 4ca9f9f9 already removed the broken dispatch — but
confirming H2 vs H1 matters because H1 would mean the underlying
function ALSO needs fixing (internal callers like the sidecar router
and Telegram handler use it; if H1 is true, those flows would also
silently fail).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cache(tmp_agents_dir, monkeypatch):
    """Reset the module-level consent cache for each test.

    Mirrors the shape used in test_consent_composable.py:
    redirect agents/ dir, reinstall the canonical get_agents_dir, reset
    the cache's bound db_path so it re-resolves under the test fixture.
    """
    import work_buddy.agent_session as asmod

    def _canonical_get_agents_dir():
        return asmod.data_dir("agents")

    monkeypatch.setattr(asmod, "get_agents_dir", _canonical_get_agents_dir)
    monkeypatch.setattr(asmod, "_cached_session_dir", None)

    from work_buddy.consent import _cache
    _cache._db_path = None
    _cache._initialized = False

    return _cache


def test_grant_consent_returns_none_by_design(cache, monkeypatch) -> None:
    """First step: verify the Python function returns None. This explains
    the ledger's result_summary:null — it's the function's design, not a
    silent failure."""
    import os
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "test-session-A")

    from work_buddy.consent import grant_consent

    ret = grant_consent("test.op", mode="once")
    assert ret is None, (
        f"grant_consent should return None; got {ret!r}. The historical "
        f"ledger pattern of result_summary:null is explained by this "
        f"design — not a silent-failure bug."
    )


def test_grant_consent_with_session_id_lands_in_that_session(
    cache, monkeypatch,
) -> None:
    """Control case for H2: explicit session_id routes the write to the
    target session's DB, and list_consents(agent_session_id=X) sees it."""
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "test-bootstrap")

    from work_buddy.consent import grant_consent, list_consents

    # Write with explicit session_id — the way internal callers
    # (gateway auto-consent, sidecar router) DO pass it.
    grant_consent("test.op", mode="once", session_id="alpha-uuid-12345678")

    # Reading from alpha's DB sees the grant.
    alpha_grants = list_consents(agent_session_id="alpha-uuid-12345678")
    assert "test.op" in alpha_grants, (
        f"Grant written with session_id='alpha-uuid-12345678' must be "
        f"visible to list_consents(agent_session_id='alpha-uuid-12345678'). "
        f"Got: {alpha_grants}"
    )


def test_grant_without_session_id_does_not_land_in_agent_db(
    cache, monkeypatch,
) -> None:
    """The empirical proof for H2.  When grant_consent is called WITHOUT
    a session_id (the way ``wb_run("consent_grant", ...)`` historically
    dispatched it — the gateway did NOT inject agent_session_id for
    consent_grant, only for consent_list), the grant lands in the cache's
    default DB.  Subsequent ``list_consents(agent_session_id=X)`` from
    the agent does not see it.

    This is the bug-shape the activity ledger documented: 28 historical
    agent grant attempts that "returned" but never showed up in the
    agent's session.  The function isn't broken — the routing is."""
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "bootstrap-shaped-uuid")

    from work_buddy.consent import grant_consent, list_consents

    # Write WITHOUT session_id — the historical buggy shape.
    grant_consent("test.op", mode="once")

    # The agent's view does NOT see the grant — different session DB.
    agent_grants = list_consents(agent_session_id="agent-X-uuid-87654321")
    assert "test.op" not in agent_grants, (
        f"Grant without session_id should NOT appear in a different "
        f"session's view.  If this assertion fires, the cache layer is "
        f"doing something different than the docstring says — "
        f"re-investigate.  Got agent_grants={agent_grants}."
    )

    # The grant DID write SOMEWHERE — the cache's default.
    # list_consents() without agent_session_id sees it (resolves to the
    # same default the cache used for the write).
    default_grants = list_consents()
    assert "test.op" in default_grants, (
        f"H1 candidate: the function is silently no-op.  Got "
        f"default_grants={default_grants}.  If this fires, grant_consent "
        f"itself is the bug, not just the routing — escalate."
    )
