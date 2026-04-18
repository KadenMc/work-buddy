"""Call-stack-aware consent risk reduction (t-3629e1b1).

A capability decorated with ``@reduces_risk_for("some.op", "low")`` declares
itself a safe invoker of ``some.op``. While it is on the call stack, inner
``@requires_consent("some.op", ...)`` calls auto-pass without prompting.
Direct calls to the primitive from code NOT wrapped by that decorator must
still go through the normal consent gate (and raise ``ConsentRequired``
when no grant exists).

These tests exercise the mechanism without touching Obsidian or the bridge.
"""
from __future__ import annotations

import pytest


def _fresh_cache(monkeypatch, tmp_path):
    """Swap the module-level ConsentCache for a tmp-path-backed one so the
    tests can't leak grants into the real session DB.
    """
    from work_buddy import consent as c

    # Fresh in-memory cache: reuse the ConsentCache class but point it at a
    # tmp DB. The class's __init__ accepts no args, so we patch the path
    # resolver.
    from work_buddy import agent_session

    monkeypatch.setattr(
        agent_session, "get_session_consent_db_path",
        lambda: tmp_path / "consent.db",
    )
    monkeypatch.setattr(
        agent_session, "get_session_audit_path",
        lambda: tmp_path / "audit.log",
    )

    c._cache = c.ConsentCache()
    return c


def test_direct_call_still_high_risk(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.requires_consent("eval.test", "desc", risk="high", default_ttl=10)
    def primitive():
        return "ran"

    # No grant, no safe caller → must raise at the declared (high) risk.
    with pytest.raises(c.ConsentRequired) as exc_info:
        primitive()
    assert exc_info.value.risk == "high"


def test_safe_caller_auto_passes(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.requires_consent("eval.test", "desc", risk="high", default_ttl=10)
    def primitive():
        return "ran"

    @c.reduces_risk_for("eval.test", "low")
    def safe_caller():
        return primitive()

    # No grant required — safe_caller declares itself low-risk for eval.test.
    assert safe_caller() == "ran"


def test_unlisted_caller_still_high_risk(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.requires_consent("eval.test", "desc", risk="high", default_ttl=10)
    def primitive():
        return "ran"

    def unlisted_caller():
        return primitive()

    with pytest.raises(c.ConsentRequired) as exc_info:
        unlisted_caller()
    assert exc_info.value.risk == "high"


def test_moderate_reduction_prompts_at_moderate(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.requires_consent("eval.test", "desc", risk="high", default_ttl=10)
    def primitive():
        return "ran"

    @c.reduces_risk_for("eval.test", "moderate")
    def moderate_caller():
        return primitive()

    with pytest.raises(c.ConsentRequired) as exc_info:
        moderate_caller()
    # Risk should be surfaced as moderate (reduced), not high.
    assert exc_info.value.risk == "moderate"


def test_reducer_only_scopes_to_declared_operation(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.requires_consent("eval.test", "desc", risk="high", default_ttl=10)
    def primitive_a():
        return "a"

    @c.requires_consent("other.op", "desc2", risk="high", default_ttl=10)
    def primitive_b():
        return "b"

    @c.reduces_risk_for("eval.test", "low")
    def mixed_caller():
        # a is covered by the declaration; b is not.
        return primitive_a() + "+" + primitive_b()

    with pytest.raises(c.ConsentRequired) as exc_info:
        mixed_caller()
    # The failing op must be other.op at its original risk.
    assert exc_info.value.operation == "other.op"
    assert exc_info.value.risk == "high"


def test_stack_is_balanced_on_exception(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.reduces_risk_for("eval.test", "low")
    def safe_caller():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        safe_caller()
    # Stack must be empty so subsequent direct calls don't leak the
    # safe-caller scope.
    assert c._safe_caller_ctx.stack == []


def test_nested_safe_callers_innermost_wins(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.requires_consent("eval.test", "desc", risk="high", default_ttl=10)
    def primitive():
        return "ran"

    @c.reduces_risk_for("eval.test", "moderate")
    def outer():
        return inner()

    @c.reduces_risk_for("eval.test", "low")
    def inner():
        return primitive()

    # Inner declares "low" — should auto-pass even though outer only
    # reduces to moderate.
    assert outer() == "ran"


def test_inner_consent_passes_through_under_safe_caller(monkeypatch, tmp_path):
    """When a safe caller auto-passes, further @requires_consent calls
    nested inside the primitive should pass through via the normal consent
    context mechanism — NOT re-check each operation independently.
    """
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.requires_consent("inner.op", "desc", risk="high", default_ttl=10)
    def inner_op():
        return "inner"

    @c.requires_consent("eval.test", "desc", risk="high", default_ttl=10)
    def primitive():
        # This inner call would normally raise ConsentRequired. But because
        # primitive is running inside a safe-caller-established consent
        # context, it should pass through.
        return inner_op()

    @c.reduces_risk_for("eval.test", "low")
    def safe_caller():
        return primitive()

    assert safe_caller() == "inner"


def test_list_risk_reducers_reports_declarations(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)

    @c.reduces_risk_for("audited.op", "low")
    def audited():
        return "ok"

    declared = c.list_risk_reducers()
    assert "audited.op" in declared
    assert any(
        risk == "low" and "audited" in qualname
        for qualname, risk in declared["audited.op"].items()
    )
