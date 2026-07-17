from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def cache(tmp_agents_dir, monkeypatch):
    import work_buddy.agent_session as agent_session
    from work_buddy import consent

    def _get_agents_dir():
        return agent_session.data_dir("agents")

    monkeypatch.setattr(agent_session, "get_agents_dir", _get_agents_dir)
    monkeypatch.setattr(agent_session, "_cached_session_dir", None)
    consent._cache._db_path = None
    consent._cache._initialized = False
    return consent._cache


def _prompt(value: str):
    from work_buddy.consent import ConsentPrompt

    return ConsentPrompt(
        body=f"Approve the exact value {value!r}",
        fingerprint=f"fingerprint:{value}",
        context={"value": value},
    )


def test_per_invocation_ignores_cache_and_user_initiated_context(cache):
    from work_buddy.consent import (
        ConsentRequired,
        grant_consent,
        requires_consent,
        user_initiated,
    )

    @requires_consent(
        "test.per_invocation.ignore_carry",
        reason="test reason",
        grant_policy="per_invocation",
        request_factory=_prompt,
    )
    def guarded(value: str):
        return value

    grant_consent("test.per_invocation.ignore_carry", mode="always")

    with user_initiated("test.click"):
        with pytest.raises(ConsentRequired) as caught:
            guarded("alpha")

    assert caught.value.body == "Approve the exact value 'alpha'"
    assert caught.value.fingerprint == "fingerprint:alpha"
    assert caught.value.context == {"value": "alpha"}
    assert caught.value.grant_policy == "per_invocation"


def test_matching_authorization_is_visible_and_consumed_once(cache):
    from work_buddy.consent import (
        ConsentRequired,
        current_per_invocation_authorization,
        per_invocation_authorization,
        requires_consent,
    )

    @requires_consent(
        "test.per_invocation.consume",
        reason="test reason",
        grant_policy="per_invocation",
        request_factory=_prompt,
    )
    def guarded(value: str):
        authorization = current_per_invocation_authorization()
        assert authorization is not None
        return {
            "value": value,
            "request_id": authorization.request_id,
            "response_surface": authorization.response_surface,
            "consumed": authorization.consumed,
        }

    with per_invocation_authorization(
        "test.per_invocation.consume",
        "fingerprint:alpha",
        request_id="request-1",
        response_surface="dashboard",
        context={"gesture": "click"},
    ) as authorization:
        assert guarded("alpha") == {
            "value": "alpha",
            "request_id": "request-1",
            "response_surface": "dashboard",
            "consumed": True,
        }
        assert authorization.consumed is True
        with pytest.raises(ConsentRequired):
            guarded("alpha")

    assert current_per_invocation_authorization() is None


def test_authorization_is_consumed_before_function_error(cache):
    from work_buddy.consent import (
        ConsentRequired,
        current_per_invocation_authorization,
        per_invocation_authorization,
        requires_consent,
    )

    @requires_consent(
        "test.per_invocation.error_consumes",
        reason="test reason",
        grant_policy="per_invocation",
        request_factory=_prompt,
    )
    def guarded(value: str):
        authorization = current_per_invocation_authorization()
        assert authorization is not None
        assert authorization.consumed is True
        raise RuntimeError("function failed")

    with per_invocation_authorization(
        "test.per_invocation.error_consumes",
        "fingerprint:alpha",
        request_id="request-2",
        response_surface="telegram",
    ):
        with pytest.raises(RuntimeError, match="function failed"):
            guarded("alpha")
        with pytest.raises(ConsentRequired):
            guarded("alpha")


def test_stale_fingerprint_raises_fresh_prompt(cache):
    from work_buddy.consent import (
        ConsentRequired,
        per_invocation_authorization,
        requires_consent,
    )

    calls: list[str] = []

    @requires_consent(
        "test.per_invocation.stale",
        reason="test reason",
        grant_policy="per_invocation",
        request_factory=_prompt,
    )
    def guarded(value: str):
        calls.append(value)

    with per_invocation_authorization(
        "test.per_invocation.stale",
        "fingerprint:old",
        request_id="request-old",
        response_surface="dashboard",
    ) as authorization:
        with pytest.raises(ConsentRequired) as caught:
            guarded("new")

    assert caught.value.fingerprint == "fingerprint:new"
    assert caught.value.body == "Approve the exact value 'new'"
    assert authorization.consumed is False
    assert calls == []


def test_preflight_skips_per_invocation_operation(cache):
    from work_buddy.consent import requires_consent
    from work_buddy.mcp_server.tools.gateway import _check_missing_consent

    @requires_consent(
        "test.per_invocation.preflight",
        reason="test reason",
        grant_policy="per_invocation",
        request_factory=_prompt,
    )
    def guarded(value: str):
        return value

    assert _check_missing_consent([
        "test.per_invocation.preflight"
    ]) == []


def test_notification_offers_once_or_deny_and_writes_no_grant(cache):
    from work_buddy.consent import (
        create_consent_request,
        finalize_consent_response,
    )
    from work_buddy.notifications.models import ResponseType, StandardResponse
    from work_buddy.notifications.store import respond_to_notification

    operation = "test.per_invocation.notification"
    record = create_consent_request(
        operation=operation,
        reason="Approve this exact action",
        grant_policy="per_invocation",
        fingerprint="fingerprint:notification",
        context={"claim": "claim-1"},
    )
    assert [choice["key"] for choice in record["choices"]] == [
        "once",
        "deny",
    ]

    respond_to_notification(
        record["request_id"],
        StandardResponse(
            response_type=ResponseType.CHOICE.value,
            value="once",
            surface="dashboard",
        ),
    )
    result = finalize_consent_response(record["request_id"])

    assert result == {
        "status": "approved",
        "request_id": record["request_id"],
        "mode": "once",
        "operation": operation,
        "fingerprint": "fingerprint:notification",
        "response_surface": "dashboard",
        "grant_written": False,
    }
    assert cache.is_granted(operation) is False


class _ApprovingDispatcher:
    def __init__(self, response):
        self.response = response

    def deliver(self, *args, **kwargs):
        return None

    def poll_response(self, *args, **kwargs):
        return self.response

    def dismiss_others(self, *args, **kwargs):
        return None


class _RacingDispatcher:
    """Persist one winning response, then return a different poll result."""

    def __init__(self, winner, polled):
        self.winner = winner
        self.polled = polled
        self.notification_id = None

    def deliver(self, notification, *args, **kwargs):
        from work_buddy.notifications.store import respond_to_notification

        self.notification_id = notification.notification_id
        respond_to_notification(notification.notification_id, self.winner)

    def poll_response(self, *args, **kwargs):
        return self.polled

    def dismiss_others(self, *args, **kwargs):
        return None


def _choice(value: str, surface: str):
    from work_buddy.notifications.models import ResponseType, StandardResponse

    return StandardResponse(
        response_type=ResponseType.CHOICE.value,
        value=value,
        surface=surface,
    )


def _use_dispatcher(monkeypatch, dispatcher):
    from work_buddy.notifications import dispatcher as dispatcher_module

    monkeypatch.setattr(
        dispatcher_module.SurfaceDispatcher,
        "from_config",
        classmethod(lambda cls: dispatcher),
    )


def test_gateway_returns_ephemeral_authorization_without_grant(
    cache, monkeypatch
):
    from work_buddy.consent import ConsentRequired
    from work_buddy.mcp_server.tools import gateway
    from work_buddy.notifications import dispatcher as dispatcher_module
    from work_buddy.notifications.models import ResponseType, StandardResponse

    response = StandardResponse(
        response_type=ResponseType.CHOICE.value,
        value="once",
        surface="telegram",
    )
    monkeypatch.setattr(
        dispatcher_module.SurfaceDispatcher,
        "from_config",
        classmethod(lambda cls: _ApprovingDispatcher(response)),
    )
    error = ConsentRequired(
        "test.per_invocation.gateway",
        "test reason",
        "moderate",
        5,
        body="Server composed body",
        fingerprint="fingerprint:gateway",
        context={"claim": "claim-2"},
        grant_policy="per_invocation",
    )

    result = gateway._auto_consent_request(
        [error.operation],
        "truth_confirm",
        "operation-1",
        timeout=0,
        session_id="agent-session-1",
        consent_error=error,
    )

    assert result["status"] == "granted"
    assert result["authorization"] == {
        "operation": error.operation,
        "fingerprint": "fingerprint:gateway",
        "request_id": result["authorization"]["request_id"],
        "response_surface": "telegram",
        "context": {"claim": "claim-2"},
    }
    assert error.operation not in cache.list_all(
        session_id="agent-session-1"
    )


def test_per_invocation_persisted_denial_beats_losing_polled_approval(
    cache, monkeypatch
):
    from work_buddy.consent import ConsentRequired
    from work_buddy.mcp_server.tools import gateway
    from work_buddy.notifications.store import get_notification

    dispatcher = _RacingDispatcher(
        _choice("deny", "dashboard"),
        _choice("once", "telegram"),
    )
    _use_dispatcher(monkeypatch, dispatcher)
    error = ConsentRequired(
        "test.per_invocation.deny_wins",
        "test reason",
        "moderate",
        5,
        body="Server composed denial race",
        fingerprint="fingerprint:deny-wins",
        grant_policy="per_invocation",
    )

    result = gateway._auto_consent_request(
        [error.operation],
        "truth_confirm",
        "operation-deny-wins",
        timeout=0,
        session_id="agent-session-deny-wins",
        consent_error=error,
    )

    assert result["status"] == "denied"
    assert "authorization" not in result
    stored = get_notification(dispatcher.notification_id)
    assert stored is not None
    assert stored.response["value"] == "deny"
    assert stored.response["surface"] == "dashboard"
    assert error.operation not in cache.list_all(
        session_id="agent-session-deny-wins"
    )


def test_per_invocation_persisted_approval_beats_losing_polled_denial(
    cache, monkeypatch
):
    from work_buddy.consent import ConsentRequired
    from work_buddy.mcp_server.tools import gateway

    _use_dispatcher(
        monkeypatch,
        _RacingDispatcher(
            _choice("once", "dashboard"),
            _choice("deny", "telegram"),
        ),
    )
    error = ConsentRequired(
        "test.per_invocation.approve_wins",
        "test reason",
        "moderate",
        5,
        body="Server composed approval race",
        fingerprint="fingerprint:approve-wins",
        grant_policy="per_invocation",
    )

    result = gateway._auto_consent_request(
        [error.operation],
        "truth_confirm",
        "operation-approve-wins",
        timeout=0,
        session_id="agent-session-approve-wins",
        consent_error=error,
    )

    assert result["status"] == "granted"
    assert result["authorization"]["response_surface"] == "dashboard"
    assert error.operation not in cache.list_all(
        session_id="agent-session-approve-wins"
    )


def test_per_invocation_uses_persisted_response_when_poll_returns_none(
    cache, monkeypatch
):
    from work_buddy.consent import ConsentRequired
    from work_buddy.mcp_server.tools import gateway

    _use_dispatcher(
        monkeypatch,
        _RacingDispatcher(_choice("once", "obsidian"), None),
    )
    error = ConsentRequired(
        "test.per_invocation.persisted_after_empty_poll",
        "test reason",
        "moderate",
        5,
        body="Server composed empty poll race",
        fingerprint="fingerprint:empty-poll",
        grant_policy="per_invocation",
    )

    result = gateway._auto_consent_request(
        [error.operation],
        "truth_confirm",
        "operation-empty-poll",
        timeout=0,
        session_id="agent-session-empty-poll",
        consent_error=error,
    )

    assert result["status"] == "granted"
    assert result["authorization"]["response_surface"] == "obsidian"


def test_cacheable_consent_uses_persisted_winner(cache, monkeypatch):
    from work_buddy.mcp_server.tools import gateway

    operation = "test.cacheable.deny_wins"
    _use_dispatcher(
        monkeypatch,
        _RacingDispatcher(
            _choice("deny", "dashboard"),
            _choice("always", "telegram"),
        ),
    )

    result = gateway._auto_consent_request(
        [operation],
        "cacheable_capability",
        "operation-cacheable-race",
        timeout=0,
        session_id="agent-session-cacheable-race",
    )

    assert result["status"] == "denied"
    assert operation not in cache.list_all(
        session_id="agent-session-cacheable-race"
    )


def test_workflow_consent_uses_persisted_winner(cache, monkeypatch):
    from work_buddy.mcp_server.tools import gateway

    _use_dispatcher(
        monkeypatch,
        _RacingDispatcher(
            _choice("deny", "dashboard"),
            _choice("temporary", "telegram"),
        ),
    )
    monkeypatch.setattr(
        gateway,
        "_collect_workflow_consent_ops",
        lambda entry: (["test.workflow.mutation"], "moderate"),
    )
    entry = SimpleNamespace(description="Race test", steps=[object()])

    result = gateway._auto_workflow_consent_request(
        "race-workflow",
        entry,
        "operation-workflow-race",
        session_id="agent-session-workflow-race",
        timeout=0,
    )

    assert result["status"] == "denied"
    assert not any(
        key.startswith("workflow_class:race-workflow")
        for key in cache.list_all(session_id="agent-session-workflow-race")
    )


def test_timed_out_late_approval_never_writes_reusable_grant(
    cache, monkeypatch
):
    from work_buddy.consent import ConsentRequired, finalize_consent_response
    from work_buddy.mcp_server.tools import gateway
    from work_buddy.notifications import dispatcher as dispatcher_module
    from work_buddy.notifications.models import ResponseType, StandardResponse
    from work_buddy.notifications.store import respond_to_notification

    monkeypatch.setattr(
        dispatcher_module.SurfaceDispatcher,
        "from_config",
        classmethod(lambda cls: _ApprovingDispatcher(None)),
    )
    error = ConsentRequired(
        "test.per_invocation.timeout",
        "test reason",
        "moderate",
        5,
        body="Server composed timeout body",
        fingerprint="fingerprint:timeout",
        context={"claim": "claim-3"},
        grant_policy="per_invocation",
    )

    result = gateway._auto_consent_request(
        [error.operation],
        "truth_confirm",
        "operation-2",
        timeout=0,
        session_id="agent-session-2",
        consent_error=error,
    )
    assert result["status"] == "timeout"
    assert "later approval cannot authorize" in result["message"]

    respond_to_notification(
        result["request_id"],
        StandardResponse(
            response_type=ResponseType.CHOICE.value,
            value="once",
            surface="dashboard",
        ),
    )
    finalized = finalize_consent_response(result["request_id"])

    assert finalized["grant_written"] is False
    assert error.operation not in cache.list_all(
        session_id="agent-session-2"
    )


def test_invoke_injects_session_and_binds_authorization():
    from work_buddy.consent import current_per_invocation_authorization
    from work_buddy.mcp_server.tools.gateway import _invoke_with_session

    def operation(value: str, *, agent_session_id: str | None = None):
        authorization = current_per_invocation_authorization()
        assert authorization is not None
        return {
            "value": value,
            "agent_session_id": agent_session_id,
            "request_id": authorization.request_id,
            "surface": authorization.response_surface,
        }

    result = _invoke_with_session(
        operation,
        "agent-session-3",
        value="alpha",
        _wb_per_invocation_authorization={
            "operation": "test.per_invocation.invoke",
            "fingerprint": "fingerprint:invoke",
            "request_id": "request-3",
            "response_surface": "dashboard",
            "context": {},
        },
    )

    assert result == {
        "value": "alpha",
        "agent_session_id": "agent-session-3",
        "request_id": "request-3",
        "surface": "dashboard",
    }


def test_reload_generations_share_one_consumed_authorization():
    import importlib
    import sys

    import work_buddy
    from work_buddy.mcp_server.tools.gateway import _invoke_with_session

    stale = importlib.import_module("work_buddy.consent")
    operation = "test.per_invocation.reload_consumption"
    calls: list[str] = []

    def stale_prompt(value: str):
        return stale.ConsentPrompt(
            body=f"Approve {value}",
            fingerprint=f"fingerprint:{value}",
        )

    @stale.requires_consent(
        operation,
        reason="test reason",
        grant_policy="per_invocation",
        request_factory=stale_prompt,
    )
    def outer(value: str):
        fresh_module = sys.modules["work_buddy.consent"]

        def fresh_prompt(inner_value: str):
            return fresh_module.ConsentPrompt(
                body=f"Approve {inner_value}",
                fingerprint=f"fingerprint:{inner_value}",
            )

        @fresh_module.requires_consent(
            operation,
            reason="test reason",
            grant_policy="per_invocation",
            request_factory=fresh_prompt,
        )
        def inner(inner_value: str):
            calls.append("inner")
            return inner_value

        calls.append("outer")
        return inner(value)

    sys.modules.pop("work_buddy.consent")
    fresh = importlib.import_module("work_buddy.consent")
    assert fresh is not stale
    try:
        with pytest.raises(fresh.ConsentRequired):
            _invoke_with_session(
                outer,
                None,
                value="alpha",
                _wb_per_invocation_authorization={
                    "operation": operation,
                    "fingerprint": "fingerprint:alpha",
                    "request_id": "request-reload",
                    "response_surface": "dashboard",
                    "context": {},
                },
            )
    finally:
        sys.modules["work_buddy.consent"] = stale
        work_buddy.consent = stale

    assert calls == ["outer"]
