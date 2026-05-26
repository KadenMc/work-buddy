"""Unit tests — composable consent primitives.

Validates the ``grant_workflow_class`` / ``grant_workflow_run`` /
``revoke_workflow_class`` / ``revoke_workflow_run`` /
``is_workflow_authorized`` primitives in isolation, without any
conductor or gateway involvement — pure unit tests on the consent
layer.

Lifecycle properties under test:

- Class grant: TTL-bounded, persists across re-invocations within the
  window, revokable independently of run grants.
- Run grant: no TTL, revoked explicitly at run completion (or via
  cascade from class revoke), idempotent revoke.
- ``is_workflow_authorized`` lookup order: run → class.
- Session routing: grants land in the named session's DB, not the
  current process's session DB.
- ``list_active_workflow_grants`` returns parsed class + run entries.

Test isolation:
  Uses the ``tmp_agents_dir`` fixture from the root ``conftest.py``,
  which monkeypatches ``paths.data_dir`` so every test sees a clean
  agents/ directory under a per-test ``tmp_path``.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cache(tmp_agents_dir, monkeypatch):
    """Reset the module-level consent cache for each test.

    ``tmp_agents_dir`` redirects ``paths.data_dir("agents")`` to a clean
    temp directory. Other test files (e.g. ``test_consent_auto_request``)
    monkey-patch ``agent_session.get_agents_dir`` to a closure-bound
    lambda — that patch survives across tests and silently overrides our
    fixture's path redirection. We defensively re-install the original
    ``get_agents_dir`` (the one that consults ``data_dir("agents")``)
    so this fixture's redirection sticks regardless of what ran first.

    We also reset the per-process dedupe set for the legacy-blanket
    deprecation log so the dedupe-counting test sees a clean slate.
    """
    import work_buddy.agent_session as asmod
    # The canonical ``get_agents_dir`` consults ``data_dir("agents")``;
    # cross-file leaks may have replaced it with a lambda that bypasses
    # our path redirection. Reinstall the real implementation so
    # ``tmp_agents_dir``'s ``data_dir`` monkey-patch applies.
    def _canonical_get_agents_dir():
        return asmod.data_dir("agents")
    monkeypatch.setattr(asmod, "get_agents_dir", _canonical_get_agents_dir)
    monkeypatch.setattr(asmod, "_cached_session_dir", None)

    from work_buddy.consent import _cache
    _cache._db_path = None
    _cache._initialized = False

    # Reset the per-process dedupe set for the legacy-blanket
    # deprecation log so tests that count emits see a clean slate.
    from work_buddy import consent as cmod
    cmod._LEGACY_BLANKET_LOGGED.clear()

    return _cache


# ---------------------------------------------------------------------------
# Key shape & accessors (no DB needed)
# ---------------------------------------------------------------------------


def test_key_helpers():
    from work_buddy.consent import (
        _workflow_class_key, _workflow_run_key,
        WORKFLOW_CLASS_PREFIX, WORKFLOW_RUN_PREFIX,
    )
    assert _workflow_class_key("task-new") == "workflow_class:task-new"
    assert _workflow_run_key("task-new", "wf_abc123") == "workflow_run:task-new:wf_abc123"
    assert WORKFLOW_CLASS_PREFIX == "workflow_class:"
    assert WORKFLOW_RUN_PREFIX == "workflow_run:"


# ---------------------------------------------------------------------------
# Class grants
# ---------------------------------------------------------------------------


def test_class_grant_lifecycle(cache):
    """grant_workflow_class writes; revoke_workflow_class clears."""
    from work_buddy.consent import (
        grant_workflow_class, revoke_workflow_class,
        is_workflow_authorized,
    )

    ok, via = is_workflow_authorized("task-new")
    assert ok is False and via is None

    grant_workflow_class("task-new", ttl_minutes=15)
    ok, via = is_workflow_authorized("task-new")
    assert ok is True and via == "class"

    revoke_workflow_class("task-new")
    ok, via = is_workflow_authorized("task-new")
    assert ok is False


def test_class_grant_is_per_workflow(cache):
    """A class grant for one workflow does not authorize another."""
    from work_buddy.consent import (
        grant_workflow_class, is_workflow_authorized,
    )

    grant_workflow_class("task-new", ttl_minutes=15)
    assert is_workflow_authorized("task-new")[0] is True
    assert is_workflow_authorized("morning-routine")[0] is False
    assert is_workflow_authorized("dev-pr")[0] is False


def test_class_grant_idempotent_revoke(cache):
    """Revoking a missing class grant is a no-op."""
    from work_buddy.consent import revoke_workflow_class
    # Should not raise
    revoke_workflow_class("does-not-exist")
    revoke_workflow_class("does-not-exist")  # second call also fine


# ---------------------------------------------------------------------------
# Run grants
# ---------------------------------------------------------------------------


def test_run_grant_lifecycle(cache):
    """grant_workflow_run writes; revoke_workflow_run clears."""
    from work_buddy.consent import (
        grant_workflow_run, revoke_workflow_run, is_workflow_authorized,
    )

    grant_workflow_run("task-new", "wf_abc")
    ok, via = is_workflow_authorized("task-new", "wf_abc")
    assert ok is True and via == "run"

    revoke_workflow_run("task-new", "wf_abc")
    ok, via = is_workflow_authorized("task-new", "wf_abc")
    assert ok is False


def test_run_grant_is_per_run(cache):
    """A grant for one run does not authorize a different run of the same
    workflow."""
    from work_buddy.consent import (
        grant_workflow_run, is_workflow_authorized,
    )

    grant_workflow_run("task-new", "wf_abc")
    ok_a, _ = is_workflow_authorized("task-new", "wf_abc")
    ok_b, _ = is_workflow_authorized("task-new", "wf_xyz")
    assert ok_a is True
    assert ok_b is False


def test_run_grant_idempotent_revoke(cache):
    """Revoking a missing run grant is a no-op."""
    from work_buddy.consent import revoke_workflow_run
    revoke_workflow_run("task-new", "wf_doesnotexist")
    revoke_workflow_run("task-new", "wf_doesnotexist")


# ---------------------------------------------------------------------------
# Authorization lookup order
# ---------------------------------------------------------------------------


def test_lookup_prefers_run_over_class(cache):
    """When both run and class grants exist, ``via`` reports 'run'."""
    from work_buddy.consent import (
        grant_workflow_class, grant_workflow_run, is_workflow_authorized,
    )

    grant_workflow_class("task-new", ttl_minutes=15)
    grant_workflow_run("task-new", "wf_abc")

    ok, via = is_workflow_authorized("task-new", "wf_abc")
    assert ok is True
    assert via == "run"


def test_lookup_falls_back_to_class_when_run_missing(cache):
    """If only the class grant exists, ``via`` reports 'class'."""
    from work_buddy.consent import (
        grant_workflow_class, is_workflow_authorized,
    )

    grant_workflow_class("task-new", ttl_minutes=15)
    ok, via = is_workflow_authorized("task-new", "wf_abc_not_granted")
    assert ok is True
    assert via == "class"


def test_lookup_without_run_id_only_checks_class(cache):
    """When ``run_id`` is None, the run-grant lookup is skipped."""
    from work_buddy.consent import (
        grant_workflow_run, is_workflow_authorized,
    )

    grant_workflow_run("task-new", "wf_abc")
    # Without a run_id, the run grant should not match.
    ok, via = is_workflow_authorized("task-new", None)
    assert ok is False
    assert via is None


# ---------------------------------------------------------------------------
# Independence of class and run grant lifecycles
# ---------------------------------------------------------------------------


def test_revoke_run_leaves_class_intact(cache):
    """Revoking the run grant should not affect the class grant."""
    from work_buddy.consent import (
        grant_workflow_class, grant_workflow_run,
        revoke_workflow_run, is_workflow_authorized,
    )

    grant_workflow_class("task-new", ttl_minutes=15)
    grant_workflow_run("task-new", "wf_abc")
    revoke_workflow_run("task-new", "wf_abc")

    # Class grant should still satisfy the lookup.
    ok, via = is_workflow_authorized("task-new", "wf_abc")
    assert ok is True
    assert via == "class"


def test_revoke_class_leaves_run_intact(cache):
    """Revoking the class grant (without cascade — cascade lives in the
    conductor) leaves the run grant intact."""
    from work_buddy.consent import (
        grant_workflow_class, grant_workflow_run,
        revoke_workflow_class, is_workflow_authorized,
    )

    grant_workflow_class("task-new", ttl_minutes=15)
    grant_workflow_run("task-new", "wf_abc")
    revoke_workflow_class("task-new")

    # Run grant is unaffected; lookup with a known run still authorized.
    ok, via = is_workflow_authorized("task-new", "wf_abc")
    assert ok is True
    assert via == "run"


# ---------------------------------------------------------------------------
# Diagnostic helper
# ---------------------------------------------------------------------------


def test_list_active_workflow_grants_parses_keys(cache):
    """``list_active_workflow_grants`` returns parsed class and run entries."""
    from work_buddy.consent import (
        grant_workflow_class, grant_workflow_run,
        list_active_workflow_grants,
    )

    grant_workflow_class("task-new", ttl_minutes=15)
    grant_workflow_run("task-new", "wf_abc")
    grant_workflow_run("morning-routine", "wf_xyz")

    snapshot = list_active_workflow_grants()
    assert "class" in snapshot
    assert "run" in snapshot
    assert len(snapshot["class"]) == 1
    assert len(snapshot["run"]) == 2

    class_entry = snapshot["class"][0]
    assert class_entry["workflow_name"] == "task-new"
    assert class_entry["mode"] == "temporary"

    run_names = {e["workflow_name"] for e in snapshot["run"]}
    assert run_names == {"task-new", "morning-routine"}

    run_ids = {e["run_id"] for e in snapshot["run"]}
    assert run_ids == {"wf_abc", "wf_xyz"}


def test_list_active_workflow_grants_skips_legacy_blanket(cache):
    """The legacy ``__workflow_consent__`` is NOT a workflow_class:/run: key
    and should not appear in the new diagnostic."""
    from work_buddy.consent import (
        grant_workflow_consent, list_active_workflow_grants,
    )

    grant_workflow_consent("wf_legacy", ttl_minutes=15)
    snapshot = list_active_workflow_grants()
    # Legacy blanket doesn't show up here — it's a separate key shape.
    assert snapshot["class"] == []
    assert snapshot["run"] == []


# ---------------------------------------------------------------------------
# TTL behavior on class grants
# ---------------------------------------------------------------------------


def test_class_grant_respects_ttl(cache):
    """A class grant whose ``expires_at`` is set in the past is treated as
    expired by the lookup path."""
    from work_buddy.consent import (
        grant_workflow_class, is_workflow_authorized, _cache,
    )
    from datetime import datetime, timedelta, timezone

    grant_workflow_class("task-new", ttl_minutes=1)
    assert is_workflow_authorized("task-new")[0] is True

    # Fast-forward by tampering with the row's expires_at directly so we
    # don't have to sleep in the test.
    import sqlite3
    db = _cache._get_db_path()
    conn = sqlite3.connect(str(db))
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    conn.execute(
        "UPDATE grants SET expires_at = ? "
        "WHERE operation = 'workflow_class:task-new'",
        (past,),
    )
    conn.commit()
    conn.close()

    ok, via = is_workflow_authorized("task-new")
    assert ok is False
    assert via is None


# ---------------------------------------------------------------------------
# Audit-log emission (smoke — full audit-format tests live elsewhere)
# ---------------------------------------------------------------------------


def test_grant_class_writes_audit_line(cache, monkeypatch):
    """Audit log records WORKFLOW_CLASS_GRANTED on class grant."""
    from work_buddy import consent as cmod

    captured: list[tuple[str, str, str]] = []

    def _capture(event, operation, details=""):
        captured.append((event, operation, details))

    monkeypatch.setattr(cmod, "_audit_log", _capture)

    cmod.grant_workflow_class("task-new", ttl_minutes=15)

    events = [e for e, _, _ in captured]
    assert "WORKFLOW_CLASS_GRANTED" in events


def test_grant_run_writes_audit_line(cache, monkeypatch):
    """Audit log records WORKFLOW_RUN_GRANTED on run grant."""
    from work_buddy import consent as cmod

    captured: list[tuple[str, str, str]] = []

    def _capture(event, operation, details=""):
        captured.append((event, operation, details))

    monkeypatch.setattr(cmod, "_audit_log", _capture)

    cmod.grant_workflow_run("task-new", "wf_abc")

    events = [e for e, _, _ in captured]
    assert "WORKFLOW_RUN_GRANTED" in events


def test_revoke_run_writes_audit_line_with_reason(cache, monkeypatch):
    """Audit log records WORKFLOW_RUN_REVOKED with the supplied reason."""
    from work_buddy import consent as cmod

    cmod.grant_workflow_run("task-new", "wf_abc")

    captured: list[tuple[str, str, str]] = []

    def _capture(event, operation, details=""):
        captured.append((event, operation, details))

    monkeypatch.setattr(cmod, "_audit_log", _capture)

    cmod.revoke_workflow_run("task-new", "wf_abc", reason="cascade")

    matching = [
        (e, op, d) for (e, op, d) in captured
        if e == "WORKFLOW_RUN_REVOKED"
    ]
    assert matching, "expected WORKFLOW_RUN_REVOKED audit entry"
    _, _, details = matching[0]
    assert "reason=cascade" in details


# ---------------------------------------------------------------------------
# Decorator check path honors the new keys
# ---------------------------------------------------------------------------


def test_workflow_run_grant_carries_low_weight_op(cache):
    """A workflow_run grant authorizes a low-weight op invocation via
    ``is_granted`` (the path the decorator uses)."""
    from work_buddy.consent import grant_workflow_run, _cache
    grant_workflow_run("task-new", "wf_abc")
    # Low-weight (default) — workflow grant carries it.
    assert _cache.is_granted("some.low_op") is True


def test_workflow_class_grant_carries_low_weight_op(cache):
    """A workflow_class grant authorizes a low-weight op invocation."""
    from work_buddy.consent import grant_workflow_class, _cache
    grant_workflow_class("task-new", ttl_minutes=15)
    assert _cache.is_granted("some.low_op") is True


def test_high_weight_op_is_not_carried_by_workflow_run_grant(cache):
    """A workflow_run grant does NOT authorize a high-weight op."""
    from work_buddy.consent import grant_workflow_run, _cache
    grant_workflow_run("task-new", "wf_abc")
    assert _cache.is_granted("destructive.op", consent_weight="high") is False


def test_high_weight_op_is_not_carried_by_workflow_class_grant(cache):
    """A workflow_class grant does NOT authorize a high-weight op."""
    from work_buddy.consent import grant_workflow_class, _cache
    grant_workflow_class("task-new", ttl_minutes=15)
    assert _cache.is_granted("destructive.op", consent_weight="high") is False


def test_high_weight_op_with_individual_grant_passes(cache):
    """A high-weight op *can* be authorized by an individual grant."""
    from work_buddy.consent import grant_consent, _cache
    grant_consent("destructive.op", mode="always")
    assert _cache.is_granted("destructive.op", consent_weight="high") is True


def test_diagnose_carry_reports_individual(cache):
    """``diagnose_carry`` returns ``individual`` when an op grant matches."""
    from work_buddy.consent import grant_consent, _cache
    grant_consent("some.op", mode="always")
    source, key = _cache.diagnose_carry("some.op")
    assert source == "individual"
    assert key == "some.op"


def test_diagnose_carry_reports_workflow_run(cache):
    """``diagnose_carry`` returns ``workflow_run`` with the matched key."""
    from work_buddy.consent import grant_workflow_run, _cache
    grant_workflow_run("task-new", "wf_abc")
    source, key = _cache.diagnose_carry("some.op")
    assert source == "workflow_run"
    assert key == "workflow_run:task-new:wf_abc"


def test_diagnose_carry_reports_workflow_class_when_no_run(cache):
    """``diagnose_carry`` falls back to workflow_class when no run key."""
    from work_buddy.consent import grant_workflow_class, _cache
    grant_workflow_class("task-new", ttl_minutes=15)
    source, key = _cache.diagnose_carry("some.op")
    assert source == "workflow_class"
    assert key == "workflow_class:task-new"


def test_diagnose_carry_reports_legacy_blanket(cache):
    """``diagnose_carry`` reports ``legacy_blanket`` when only the legacy
    key is present."""
    from work_buddy.consent import grant_workflow_consent, _cache
    grant_workflow_consent("wf_test", ttl_minutes=15)
    source, key = _cache.diagnose_carry("some.op")
    assert source == "legacy_blanket"
    assert key == "__workflow_consent__"


def test_diagnose_carry_reports_none_when_no_grant(cache):
    """``diagnose_carry`` reports ``none`` when nothing matches."""
    from work_buddy.consent import _cache
    source, key = _cache.diagnose_carry("some.op")
    assert source == "none"
    assert key is None


def test_diagnose_carry_prefers_run_over_class(cache):
    """When both run and class grants exist, ``diagnose_carry`` reports
    the more specific run grant."""
    from work_buddy.consent import (
        grant_workflow_run, grant_workflow_class, _cache,
    )
    grant_workflow_class("task-new", ttl_minutes=15)
    grant_workflow_run("task-new", "wf_abc")
    source, _ = _cache.diagnose_carry("some.op")
    assert source == "workflow_run"


def test_legacy_blanket_still_carries_with_deprecation_log(cache, monkeypatch):
    """The legacy ``__workflow_consent__`` key still grants (back-compat),
    but emits exactly one deprecation audit-log line per operation per
    process."""
    from work_buddy.consent import grant_workflow_consent, _cache
    from work_buddy import consent as cmod

    cmod._LEGACY_BLANKET_LOGGED.clear()

    captured: list[tuple[str, str, str]] = []

    def _capture(event, operation, details=""):
        captured.append((event, operation, details))

    monkeypatch.setattr(cmod, "_audit_log", _capture)

    grant_workflow_consent("wf_legacy", ttl_minutes=15)
    # Call 1 — carries, emits deprecation log
    assert _cache.is_granted("legacy.op_1") is True
    # Call 2 same op — carries, NO duplicate deprecation log
    assert _cache.is_granted("legacy.op_1") is True
    # Call 3 different op — carries, emits one deprecation log for that op
    assert _cache.is_granted("legacy.op_2") is True

    legacy_events = [
        (e, op) for (e, op, _) in captured
        if e == "LEGACY_WORKFLOW_BLANKET_USED"
    ]
    assert ("LEGACY_WORKFLOW_BLANKET_USED", "legacy.op_1") in legacy_events
    assert ("LEGACY_WORKFLOW_BLANKET_USED", "legacy.op_2") in legacy_events
    op_1_count = sum(1 for (_, op) in legacy_events if op == "legacy.op_1")
    assert op_1_count == 1


# ---------------------------------------------------------------------------
# Retry-queue isolation (originating-session fallback)
# ---------------------------------------------------------------------------
# Detailed cross-session tests live in
# ``tests/unit/test_consent_originating_session.py`` (it has the
# ``two_sessions`` fixture wiring two SQLite DBs). Here we cover the
# in-session behavior: ``from_originating=True`` suppresses workflow
# grants even when they exist in the same session DB.


def test_from_originating_suppresses_workflow_run_grant(cache):
    """When ``from_originating=True``, workflow_run grants do not carry."""
    from work_buddy.consent import grant_workflow_run, _cache
    grant_workflow_run("task-new", "wf_abc")
    # Direct in-session check carries...
    assert _cache._is_granted_in_session("some.op", session_id=None) is True
    # ...but the replay-path lookup does not.
    assert _cache._is_granted_in_session(
        "some.op", session_id=None, from_originating=True,
    ) is False


def test_from_originating_still_carries_individual_grant(cache):
    """The replay-path isolation only suppresses workflow grants;
    individual op grants continue to authorize replay-path lookups."""
    from work_buddy.consent import grant_consent, _cache
    grant_consent("some.op", mode="always")
    assert _cache._is_granted_in_session(
        "some.op", session_id=None, from_originating=True,
    ) is True


def test_from_originating_suppresses_legacy_blanket(cache):
    """``from_originating=True`` also suppresses the legacy
    ``__workflow_consent__`` blanket — replays should not ride any
    workflow-shaped grant, new or legacy."""
    from work_buddy.consent import grant_workflow_consent, _cache
    grant_workflow_consent("wf_test", ttl_minutes=15)
    assert _cache._is_granted_in_session(
        "some.op", session_id=None, from_originating=True,
    ) is False


# ---------------------------------------------------------------------------
# @requires_consent decorator integration
# ---------------------------------------------------------------------------


def test_decorator_accepts_consent_weight_parameter(cache):
    """``@requires_consent(..., consent_weight="high")`` registers the
    weight in the metadata registry."""
    from work_buddy.consent import (
        requires_consent, get_consent_metadata,
    )

    @requires_consent(
        "test.high_weight_op",
        reason="testing",
        risk="moderate",
        consent_weight="high",
    )
    def _high_weight_fn():
        return "ok"

    meta = get_consent_metadata("test.high_weight_op")
    assert meta is not None
    assert meta["consent_weight"] == "high"
    assert meta["risk"] == "moderate"


def test_decorator_defaults_consent_weight_to_risk(cache):
    """When ``consent_weight`` is omitted, it mirrors the declared ``risk``."""
    from work_buddy.consent import (
        requires_consent, get_consent_metadata,
    )

    @requires_consent(
        "test.moderate_default_op",
        reason="testing",
        risk="moderate",
    )
    def _moderate_fn():
        return "ok"

    meta = get_consent_metadata("test.moderate_default_op")
    assert meta is not None
    assert meta["consent_weight"] == "moderate"


def test_decorator_rejects_invalid_consent_weight(cache):
    """Invalid consent_weight raises at decoration time."""
    from work_buddy.consent import requires_consent

    import pytest as _pytest
    with _pytest.raises(ValueError, match="Invalid consent_weight"):
        @requires_consent(
            "test.bad_weight",
            reason="testing",
            risk="moderate",
            consent_weight="ultraviolet",
        )
        def _bad():
            return "x"


# ---------------------------------------------------------------------------
# Out-of-band approval — resolve_consent_request fills in the workflow_class
# grant AND dismisses sibling surfaces (the gateway's in-window poll does
# both synchronously; this path closes the gap for Telegram / Obsidian
# approvals landing after the poll has exited).
# ---------------------------------------------------------------------------


def _seed_workflow_consent_notification(workflow_name: str = "task-new"):
    """Build a workflow-consent notification record in the same shape
    the gateway's ``_auto_workflow_consent_request`` creates."""
    from work_buddy.consent import create_consent_request
    record = create_consent_request(
        operation=f"workflow:{workflow_name}",
        reason="(test) authorize this workflow?",
        risk="moderate",
        default_ttl=15,
        requester=f"gateway:workflow:{workflow_name}",
        context={
            "kind": "workflow_consent",
            "workflow_name": workflow_name,
            "operation_id": "op_test",
            "consent_ops": ["tasks.create_task"],
        },
    )
    return record["notification_id"]


def test_resolve_workflow_consent_temporary_mints_class_grant(cache):
    """Out-of-band approval with mode='temporary' mints the
    ``workflow_class:<name>`` grant with the 15-min TTL — the same
    grant the in-window poll path would have written."""
    from work_buddy.consent import (
        resolve_consent_request, is_workflow_authorized,
        WORKFLOW_CLASS_TEMPORARY_TTL_MIN,
    )
    nid = _seed_workflow_consent_notification("task-new")

    # Sanity: no class grant before resolve.
    assert is_workflow_authorized("task-new")[0] is False

    resolve_consent_request(
        nid, approved=True, mode="temporary",
        ttl_minutes=WORKFLOW_CLASS_TEMPORARY_TTL_MIN,
    )

    ok, via = is_workflow_authorized("task-new")
    assert ok is True
    assert via == "class"


def test_resolve_workflow_consent_always_mints_24h_class_grant(cache):
    """Out-of-band approval with mode='always' uses the 24h TTL constant."""
    from work_buddy.consent import (
        resolve_consent_request, is_workflow_authorized,
    )
    nid = _seed_workflow_consent_notification("task-new")

    resolve_consent_request(nid, approved=True, mode="always")

    ok, via = is_workflow_authorized("task-new")
    assert ok is True
    assert via == "class"


def test_resolve_workflow_consent_once_does_not_mint_class_grant(cache):
    """Out-of-band approval with mode='once' authorizes only this
    invocation (via the run grant minted later by start_workflow);
    no class grant is written."""
    from work_buddy.consent import (
        resolve_consent_request, is_workflow_authorized,
    )
    nid = _seed_workflow_consent_notification("task-new")

    resolve_consent_request(nid, approved=True, mode="once")

    # No class grant minted on "once" — the run grant covers this run.
    ok, _ = is_workflow_authorized("task-new")
    assert ok is False


def test_resolve_workflow_consent_denied_writes_no_grants(cache):
    """A denied workflow-consent prompt writes no class grant."""
    from work_buddy.consent import (
        resolve_consent_request, is_workflow_authorized,
    )
    nid = _seed_workflow_consent_notification("task-new")

    resolve_consent_request(nid, approved=False)

    ok, _ = is_workflow_authorized("task-new")
    assert ok is False


def test_resolve_capability_consent_unchanged(cache):
    """A non-workflow-consent notification (e.g. capability bundle)
    does NOT mint a workflow_class grant. The new code path is gated
    on ``context.kind == "workflow_consent"``."""
    from work_buddy.consent import (
        create_consent_request, resolve_consent_request,
        is_workflow_authorized, _cache,
    )
    # Capability-style bundle (no ``kind`` field).
    record = create_consent_request(
        operation="bundle:task_create",
        reason="(test)",
        risk="moderate",
        default_ttl=15,
        requester="gateway:task_create",
        context={"capability": "task_create", "operations": ["tasks.create_task"]},
    )
    nid = record["notification_id"]

    resolve_consent_request(
        nid, approved=True, mode="temporary", ttl_minutes=15,
    )

    # Individual op grant was written...
    assert _cache.is_granted("tasks.create_task") is True
    # ...but no workflow_class grant for "task_create" exists.
    assert is_workflow_authorized("task_create")[0] is False


def test_resolve_dismisses_other_surfaces(cache, monkeypatch):
    """``resolve_consent_request`` calls ``dispatcher.dismiss_others`` so
    a notification approved on one surface disappears on the others.
    This is the bug surfaced in the live test: Telegram approvals
    landing after the gateway's poll exit used to leave the Obsidian
    modal up indefinitely.
    """
    from work_buddy.consent import resolve_consent_request
    from work_buddy.notifications import store, dispatcher as dispatcher_mod

    nid = _seed_workflow_consent_notification("task-new")
    # Pretend the notification was delivered to two surfaces.
    store.mark_delivered(nid, "telegram")
    store.mark_delivered(nid, "obsidian")

    calls = []

    class _FakeDispatcher:
        @classmethod
        def from_config(cls):
            return cls()

        def dismiss_others(self, request_id, *, responding_surface,
                           delivered_surfaces):
            calls.append({
                "request_id": request_id,
                "responding_surface": responding_surface,
                "delivered_surfaces": list(delivered_surfaces),
            })
            return {"dismissed": [s for s in delivered_surfaces if s != responding_surface]}

    monkeypatch.setattr(dispatcher_mod, "SurfaceDispatcher", _FakeDispatcher)

    resolve_consent_request(
        nid, approved=True, mode="temporary", ttl_minutes=15,
    )

    assert len(calls) == 1
    assert calls[0]["request_id"] == nid
    # ``response.surface`` was "direct" (programmatic resolve); the
    # helper passes that through.
    assert calls[0]["responding_surface"] == "direct"
    assert set(calls[0]["delivered_surfaces"]) == {"telegram", "obsidian"}


# ---------------------------------------------------------------------------
# consent_list session routing — without ``agent_session_id``, the cache's
# default-path fallback can read from a different session than the
# caller's. The gateway injects the agent session id; the helper here
# exercises the routing directly.
# ---------------------------------------------------------------------------


def test_list_consents_routes_to_named_session(cache, tmp_path, monkeypatch):
    """``list_consents(agent_session_id=...)`` reads from the named
    session's DB, not from whatever the cache's instance path is
    cached against.
    """
    import sqlite3
    from datetime import datetime, timezone
    from work_buddy.consent import list_consents

    # Hand-write a grant directly into a sibling session's DB so we
    # can verify the routing without needing a full second-session
    # setup. ``ConsentCache._connect(session_id=...)`` resolves the
    # path via ``agent_session.get_session_dir(session_id)``.
    import work_buddy.agent_session as asmod
    other_dir = tmp_path / "agents" / "99999999_other"
    other_dir.mkdir(parents=True, exist_ok=True)
    other_db = other_dir / "consent.db"

    original_get_dir = asmod.get_session_dir

    def _patched_get_session_dir(session_id=None):
        if session_id == "other-session-id":
            return other_dir
        return original_get_dir(session_id)

    monkeypatch.setattr(asmod, "get_session_dir", _patched_get_session_dir)

    conn = sqlite3.connect(str(other_db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS grants (
              operation TEXT PRIMARY KEY,
              mode TEXT NOT NULL,
              granted_at TEXT NOT NULL,
              expires_at TEXT
           )"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO grants VALUES (?, ?, ?, ?)",
        ("other.op", "always",
         datetime.now(timezone.utc).isoformat(), None),
    )
    conn.commit()
    conn.close()

    # Default-path lookup does NOT see the other session's grant.
    assert "other.op" not in list_consents()

    # Session-routed lookup DOES see it.
    other = list_consents(agent_session_id="other-session-id")
    assert "other.op" in other
    assert other["other.op"]["mode"] == "always"


# ---------------------------------------------------------------------------
# finalize_consent_response — the helper that surfaces with their own
# response-recording paths (Telegram inline buttons, /reply command) call
# after ``respond_to_notification``. Until this helper existed, Telegram
# out-of-band approvals recorded the response but never wrote any
# consent grant — the "Allow for 15 min" window was a dead promise.
# ---------------------------------------------------------------------------


def _seed_responded_workflow_consent(workflow_name: str, choice: str = "temporary"):
    """Helper: create a workflow-consent notification AND record a
    Telegram-shaped response on it. Mimics the state Telegram's
    ``on_button`` leaves the notification in after ``respond_to_notification``.
    """
    from work_buddy.consent import create_consent_request
    from work_buddy.notifications.store import respond_to_notification
    from work_buddy.notifications.models import StandardResponse, ResponseType
    record = create_consent_request(
        operation=f"workflow:{workflow_name}",
        reason="(test) authorize this workflow?",
        risk="moderate",
        default_ttl=15,
        requester=f"gateway:workflow:{workflow_name}",
        context={
            "kind": "workflow_consent",
            "workflow_name": workflow_name,
            "operation_id": "op_test",
            "consent_ops": ["tasks.create_task"],
        },
    )
    nid = record["notification_id"]
    respond_to_notification(
        nid,
        StandardResponse(
            response_type=ResponseType.CHOICE.value,
            value=choice,
            raw={"callback_data": f"{nid[:8]}:{choice}", "telegram_message_id": 1},
            surface="telegram",
        ),
    )
    return nid


def test_finalize_workflow_consent_mints_class_grant_from_recorded_response(cache):
    """``finalize_consent_response`` reads the already-recorded response
    and mints the workflow_class grant — the path Telegram's
    ``on_button`` exercises."""
    from work_buddy.consent import (
        finalize_consent_response, is_workflow_authorized,
    )
    nid = _seed_responded_workflow_consent("task-new", choice="temporary")

    # Sanity: no class grant before finalize.
    assert is_workflow_authorized("task-new")[0] is False

    out = finalize_consent_response(nid)
    assert out["status"] == "approved"
    assert out["mode"] == "temporary"
    assert out["operation"] == "workflow:task-new"

    ok, via = is_workflow_authorized("task-new")
    assert ok is True
    assert via == "class"


def test_finalize_workflow_consent_always_mode_uses_24h_ttl(cache):
    from work_buddy.consent import (
        finalize_consent_response, is_workflow_authorized,
    )
    nid = _seed_responded_workflow_consent("task-new", choice="always")

    finalize_consent_response(nid)

    ok, via = is_workflow_authorized("task-new")
    assert ok is True
    assert via == "class"


def test_finalize_workflow_consent_once_writes_no_class_grant(cache):
    from work_buddy.consent import (
        finalize_consent_response, is_workflow_authorized,
    )
    nid = _seed_responded_workflow_consent("task-new", choice="once")

    finalize_consent_response(nid)

    # "once" doesn't mint a class grant — only the run grant covers the run.
    ok, _ = is_workflow_authorized("task-new")
    assert ok is False


def test_finalize_returns_no_response_when_unanswered(cache):
    from work_buddy.consent import (
        create_consent_request, finalize_consent_response,
    )
    record = create_consent_request(
        operation="workflow:task-new",
        reason="(test)", risk="moderate", default_ttl=15,
        requester="gateway:workflow:task-new",
        context={"kind": "workflow_consent", "workflow_name": "task-new"},
    )
    out = finalize_consent_response(record["notification_id"])
    assert out["status"] == "no_response"


def test_finalize_returns_not_found_for_unknown_id(cache):
    from work_buddy.consent import finalize_consent_response
    out = finalize_consent_response("req_doesnotexist")
    assert out["status"] == "not_found"


def test_finalize_capability_bundle_writes_individual_op_grants(cache):
    """For a capability-bundle consent (the ``bundle:<cap>`` shape), the
    helper grants each underlying op so the ``@requires_consent``
    decorators (which check individual op names) pass."""
    from work_buddy.consent import (
        create_consent_request, finalize_consent_response, _cache,
    )
    from work_buddy.notifications.store import respond_to_notification
    from work_buddy.notifications.models import StandardResponse, ResponseType
    record = create_consent_request(
        operation="bundle:task_create",
        reason="(test)", risk="moderate", default_ttl=15,
        requester="gateway:task_create",
        context={"capability": "task_create", "operations": ["tasks.create_task"]},
    )
    nid = record["notification_id"]
    respond_to_notification(
        nid,
        StandardResponse(
            response_type=ResponseType.CHOICE.value, value="temporary",
            raw={}, surface="telegram",
        ),
    )

    finalize_consent_response(nid)

    assert _cache.is_granted("tasks.create_task") is True


def test_finalize_idempotent_on_repeated_calls(cache):
    """Calling ``finalize_consent_response`` twice for the same
    notification is a no-op the second time — the grant write uses
    INSERT OR REPLACE, so no spurious state changes."""
    from work_buddy.consent import (
        finalize_consent_response, is_workflow_authorized,
    )
    nid = _seed_responded_workflow_consent("task-new", choice="temporary")

    out_1 = finalize_consent_response(nid)
    out_2 = finalize_consent_response(nid)

    assert out_1["status"] == "approved"
    assert out_2["status"] == "approved"  # still approved on re-read
    assert is_workflow_authorized("task-new")[0] is True


def test_finalize_denied_writes_no_grants(cache):
    from work_buddy.consent import (
        finalize_consent_response, is_workflow_authorized,
    )
    nid = _seed_responded_workflow_consent("task-new", choice="deny")

    out = finalize_consent_response(nid)
    assert out["status"] == "denied"

    ok, _ = is_workflow_authorized("task-new")
    assert ok is False


# ---------------------------------------------------------------------------
# _auto_consent_request session routing — the bug an earlier session
# exposed: capability-bundle consent prompts had their
# ``callback_session_id`` set from ``os.environ.get("WORK_BUDDY_SESSION_ID")``
# (the MCP server's bootstrap session), not from the agent's session.
# Out-of-band approvals (Telegram callback, Obsidian-modal click after
# the gateway's poll has exited) then mis-routed grants into the
# bootstrap DB while the agent's DB stayed empty. The fix threads an
# explicit ``session_id`` argument from the caller (the agent's session
# id, resolved via ``_resolve_session(ctx)``) through to
# ``create_consent_request``.
# ---------------------------------------------------------------------------


def test_auto_consent_request_routes_callback_to_explicit_session(
    cache, monkeypatch,
):
    """``_auto_consent_request(session_id=<sid>)`` sets the resulting
    notification's ``callback_session_id`` to ``<sid>``, not to the
    process's env-var session."""
    from work_buddy.mcp_server.tools import gateway

    # Capture the args ``create_consent_request`` receives.
    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return {
            "notification_id": "req_fake",
            "request_id": "req_fake",
            "notification_id_short": "fake1234",
        }

    monkeypatch.setattr(gateway, "create_consent_request", _fake_create, raising=False)
    # The function imports ``create_consent_request`` from
    # ``work_buddy.consent`` inside its body; patch that source too.
    from work_buddy import consent as cmod
    monkeypatch.setattr(cmod, "create_consent_request", _fake_create)

    # Make the dispatcher path a no-op so the function exits fast (no
    # 90s wait). Returning a dispatcher whose poll yields None makes
    # _auto_consent_request return the "timeout" payload — but we
    # don't care about its return value here, only the captured args.
    class _NoopDispatcher:
        @classmethod
        def from_config(cls):
            return cls()
        def deliver(self, *args, **kwargs):
            pass
        def poll_response(self, *args, **kwargs):
            return None

    from work_buddy.notifications import dispatcher as disp_mod
    monkeypatch.setattr(disp_mod, "SurfaceDispatcher", _NoopDispatcher)
    # Also patch get_notification so the "delivered_surfaces" branch
    # gracefully returns None.
    monkeypatch.setattr(
        "work_buddy.notifications.store.get_notification",
        lambda nid: None,
    )

    # Force the env to a known bogus value so we can prove the
    # function did NOT fall back to env when session_id was given.
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "bootstrap-bogus-sid")

    gateway._auto_consent_request(
        operations=["test.op"],
        capability_name="test_cap",
        op_id="op_test",
        session_id="explicit-agent-sid",
    )

    assert captured.get("callback_session_id") == "explicit-agent-sid", (
        f"Expected callback_session_id='explicit-agent-sid', got "
        f"{captured.get('callback_session_id')!r}. The function must "
        f"route to the explicit session id, not the env-var fallback."
    )


def test_auto_consent_request_falls_back_to_env_when_session_id_none(
    cache, monkeypatch,
):
    """Back-compat: when no ``session_id`` is passed,
    ``_auto_consent_request`` still reads the env var. Keeps any
    legacy / direct caller that hasn't been updated working."""
    from work_buddy.mcp_server.tools import gateway

    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        return {"notification_id": "req_fake", "request_id": "req_fake"}

    from work_buddy import consent as cmod
    monkeypatch.setattr(cmod, "create_consent_request", _fake_create)

    class _NoopDispatcher:
        @classmethod
        def from_config(cls):
            return cls()
        def deliver(self, *args, **kwargs):
            pass
        def poll_response(self, *args, **kwargs):
            return None

    from work_buddy.notifications import dispatcher as disp_mod
    monkeypatch.setattr(disp_mod, "SurfaceDispatcher", _NoopDispatcher)
    monkeypatch.setattr(
        "work_buddy.notifications.store.get_notification",
        lambda nid: None,
    )
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "env-fallback-sid")

    gateway._auto_consent_request(
        operations=["test.op"],
        capability_name="test_cap",
        op_id="op_test",
        # No session_id arg.
    )

    assert captured.get("callback_session_id") == "env-fallback-sid"
