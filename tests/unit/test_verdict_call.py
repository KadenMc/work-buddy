"""Tests for work_buddy.triage.verdict_call — validation-escalation helper."""

from __future__ import annotations

from work_buddy.llm import ErrorKind, LLMResponse, LLMRunner, ModelTier
from work_buddy.triage.verdict_call import call_for_verdict


_SCHEMA = {"type": "object", "properties": {"recommended_action": {"type": "string"}}}


def _resp(
    *, tier: ModelTier, action: str | None = None, err: ErrorKind | None = None,
) -> LLMResponse:
    structured: dict = {}
    if action is not None:
        structured["recommended_action"] = action
    return LLMResponse(
        content="",
        structured_output=structured or None,
        tier_used=tier.value,
        error="x" if err else None,
        error_kind=err,
    )


def test_first_tier_success_returns_immediately(monkeypatch) -> None:
    """When the starting tier returns a valid verdict, no retry call happens."""
    calls: list[ModelTier] = []

    def fake_call(self, *, tier, **kw):
        calls.append(tier)
        return _resp(tier=tier, action="create_task")

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    resp = call_for_verdict(
        runner=LLMRunner(),
        tier=ModelTier.FRONTIER_BALANCED,
        system="s", user="u", output_schema=_SCHEMA,
    )

    assert not resp.is_error()
    assert resp.structured_output == {"recommended_action": "create_task"}
    assert calls == [ModelTier.FRONTIER_BALANCED]


def test_validation_failure_escalates_to_frontier_best(monkeypatch) -> None:
    """Missing recommended_action at FRONTIER_BALANCED → retry at FRONTIER_BEST."""
    responses = iter([
        _resp(tier=ModelTier.FRONTIER_BALANCED, action=None),   # missing field
        _resp(tier=ModelTier.FRONTIER_BEST, action="leave"),     # valid on retry
    ])
    calls: list[ModelTier] = []

    def fake_call(self, *, tier, **kw):
        calls.append(tier)
        return next(responses)

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    resp = call_for_verdict(
        runner=LLMRunner(),
        tier=ModelTier.FRONTIER_BALANCED,
        system="s", user="u", output_schema=_SCHEMA,
    )

    assert not resp.is_error()
    assert resp.structured_output == {"recommended_action": "leave"}
    assert calls == [ModelTier.FRONTIER_BALANCED, ModelTier.FRONTIER_BEST]


def test_validation_failure_at_frontier_best_surfaces_validation_failed(
    monkeypatch,
) -> None:
    """Missing field at starting tier AND after retry → VALIDATION_FAILED."""
    responses = iter([
        _resp(tier=ModelTier.FRONTIER_BALANCED, action=None),
        _resp(tier=ModelTier.FRONTIER_BEST, action=None),  # still missing
    ])

    def fake_call(self, **kw):
        return next(responses)

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    resp = call_for_verdict(
        runner=LLMRunner(),
        tier=ModelTier.FRONTIER_BALANCED,
        system="s", user="u", output_schema=_SCHEMA,
    )

    assert resp.is_error()
    assert resp.error_kind == ErrorKind.VALIDATION_FAILED
    assert "recommended_action" in (resp.error or "")


def test_starting_at_frontier_best_no_validation_retry(monkeypatch) -> None:
    """If caller starts at FRONTIER_BEST and validation fails, no re-call."""
    calls: list[ModelTier] = []

    def fake_call(self, *, tier, **kw):
        calls.append(tier)
        return _resp(tier=ModelTier.FRONTIER_BEST, action=None)

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    resp = call_for_verdict(
        runner=LLMRunner(),
        tier=ModelTier.FRONTIER_BEST,
        system="s", user="u", output_schema=_SCHEMA,
    )

    assert resp.is_error()
    assert resp.error_kind == ErrorKind.VALIDATION_FAILED
    # No validation-retry because we were already at the top tier.
    assert calls == [ModelTier.FRONTIER_BEST]


def test_backend_error_surfaces_without_validation_retry(monkeypatch) -> None:
    """Backend error bypasses validation logic — no post-error re-call."""
    calls: list[ModelTier] = []

    def fake_call(self, *, tier, **kw):
        calls.append(tier)
        return _resp(tier=tier, err=ErrorKind.AUTH)

    monkeypatch.setattr(LLMRunner, "call", fake_call)

    resp = call_for_verdict(
        runner=LLMRunner(),
        tier=ModelTier.FRONTIER_BALANCED,
        system="s", user="u", output_schema=_SCHEMA,
    )

    assert resp.is_error()
    assert resp.error_kind == ErrorKind.AUTH
    # Helper made exactly one call; it does NOT re-call on a backend
    # error that wasn't in its internal escalate_on set.
    assert calls == [ModelTier.FRONTIER_BALANCED]
