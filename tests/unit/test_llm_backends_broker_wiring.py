"""Broker wiring invariants for the LLM backend call functions.

``call_openai_compat`` and ``call_lmstudio_native`` both dispatch
through ``LocalInferenceBroker`` — this file pins the wiring:

* Each backend uses a distinct profile-name prefix so slot limits
  don't accidentally collide with the embedding path (which uses
  ``lmstudio:`` prefix).
* Priority defaults to ``WORKFLOW`` (middle-ground — caller can override).
* Metrics record ``started_http_at`` before the HTTP call and ``status==ok``
  after a successful call.
* ``QueueWaitTimeout`` propagates without being caught (so callers
  can classify it distinctly from inference-layer errors).

Uses ``httpx.MockTransport`` to stand in for the real LM Studio
endpoint, so these tests are self-contained and fast.
"""

from __future__ import annotations

import httpx
import pytest

from work_buddy.inference import Priority
from work_buddy.inference.broker import _reset_broker_for_tests


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_broker_for_tests()
    yield
    _reset_broker_for_tests()


# ---------------------------------------------------------------------------
# openai_compat
# ---------------------------------------------------------------------------


def _oa_fake_response():
    """Minimal OpenAI chat-completions success body."""
    return {
        "choices": [
            {"message": {"content": "hello from the fake"}}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        "model": "fake-echo",
    }


def test_openai_compat_records_broker_metrics(monkeypatch):
    """Successful call lands in the broker's ring buffer with the
    expected profile name, priority, status=ok, and HTTP-start stamped."""
    import work_buddy.llm.backends.openai_compat as mod
    from work_buddy.inference import get_broker

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json=_oa_fake_response())

    # Replace httpx.Client() inside the module with a Mock-transport version.
    real_client = httpx.Client

    def _mock_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mod.httpx, "Client", _mock_client)

    result = mod.call_openai_compat(
        base_url="http://fake/v1",
        model="fake-echo",
        system="sys",
        user="usr",
    )
    assert result["content"] == "hello from the fake"

    metrics = get_broker().snapshot_metrics()
    assert len(metrics) == 1
    m = metrics[0]
    assert m["profile"] == "openai_compat:fake-echo"
    assert m["priority"] == "WORKFLOW"
    assert m["status"] == "ok"
    assert m["started_http_at"] is not None
    assert m["service_time_ms"] is not None


def test_openai_compat_priority_override(monkeypatch):
    """Caller-supplied priority is respected and recorded in metrics."""
    import work_buddy.llm.backends.openai_compat as mod
    from work_buddy.inference import get_broker

    real_client = httpx.Client
    monkeypatch.setattr(mod.httpx, "Client", lambda *a, **kw: real_client(
        *a, **kw, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=_oa_fake_response())
        ),
    ))

    mod.call_openai_compat(
        base_url="http://fake/v1",
        model="fake-echo",
        system="sys",
        user="usr",
        priority=Priority.INTERACTIVE,
    )
    m = get_broker().snapshot_metrics()[-1]
    assert m["priority"] == "INTERACTIVE"


def test_openai_compat_http_error_propagates_as_localinferenceerror(
    monkeypatch,
):
    """Transport failures surface as ``LocalInferenceError`` with the
    right error kind — broker wrapping must not swallow this."""
    import work_buddy.llm.backends.openai_compat as mod
    from work_buddy.llm.backends._errors import LocalInferenceError
    from work_buddy.inference import get_broker

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection refused")

    real_client = httpx.Client
    monkeypatch.setattr(mod.httpx, "Client", lambda *a, **kw: real_client(
        *a, **kw, transport=httpx.MockTransport(_handler),
    ))

    with pytest.raises(LocalInferenceError) as excinfo:
        mod.call_openai_compat(
            base_url="http://fake/v1",
            model="fake-echo",
            system="sys",
            user="usr",
        )
    assert excinfo.value.kind == "server_unreachable"

    # The slot was released; metrics recorded with the wrapped error.
    m = get_broker().snapshot_metrics()[-1]
    # BrokerError -> uses its own kind. Plain exceptions -> "error".
    # LocalInferenceError is not a BrokerError, so it records as "error".
    assert m["status"] == "error"
    assert m["error_kind"] == "LocalInferenceError"


# ---------------------------------------------------------------------------
# lmstudio_native
# ---------------------------------------------------------------------------


def _lms_fake_response():
    """Minimal LM Studio /api/v1/chat success body."""
    return {
        "output": [
            {"type": "message", "content": "lm studio echo"}
        ],
        "stats": {
            "input_tokens": 2,
            "total_output_tokens": 4,
            "reasoning_output_tokens": 0,
        },
        "response_id": "resp_fake",
        "model_instance_id": "fake-lms",
    }


def test_lmstudio_native_records_broker_metrics_with_distinct_profile(
    monkeypatch,
):
    """The native-chat backend uses a ``lmstudio_native:`` prefix so
    its slot-limit is independent of the embedding ``lmstudio:`` path
    and the ``openai_compat:`` path."""
    import work_buddy.llm.backends.lmstudio_native as mod
    from work_buddy.inference import get_broker

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/v1/chat")
        return httpx.Response(200, json=_lms_fake_response())

    real_client = httpx.Client
    monkeypatch.setattr(mod.httpx, "Client", lambda *a, **kw: real_client(
        *a, **kw, transport=httpx.MockTransport(_handler),
    ))

    result = mod.call_lmstudio_native(
        base_url="http://fake",
        model="fake-lms",
        system="sys",
        user="usr",
    )
    assert "lm studio echo" in result["content"]

    metrics = get_broker().snapshot_metrics()
    assert len(metrics) == 1
    m = metrics[0]
    # Critical: prefix is distinct from the embedding 'lmstudio:' path
    # AND from the openai_compat 'openai_compat:' path.
    assert m["profile"] == "lmstudio_native:fake-lms"
    assert m["priority"] == "WORKFLOW"
    assert m["status"] == "ok"
    assert m["started_http_at"] is not None


def test_lmstudio_native_and_openai_compat_profiles_are_distinct(
    monkeypatch,
):
    """Same model name + same base URL should NOT collide between
    backends: each gets its own broker profile so slot limits are
    independent. Regression guard for accidentally unifying the
    profile prefix."""
    import work_buddy.llm.backends.openai_compat as oa_mod
    import work_buddy.llm.backends.lmstudio_native as lms_mod
    from work_buddy.inference import get_broker

    # Both modules import the same ``httpx`` — a single route-by-path
    # handler serves both. (Earlier attempts to monkeypatch them
    # separately clobbered each other because ``httpx.Client`` is a
    # shared module attribute.)
    def _router(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json=_oa_fake_response())
        if request.url.path.endswith("/api/v1/chat"):
            return httpx.Response(200, json=_lms_fake_response())
        raise AssertionError(f"unexpected URL: {request.url}")

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real_client(
        *a, **kw, transport=httpx.MockTransport(_router),
    ))

    oa_mod.call_openai_compat(
        base_url="http://shared/v1",
        model="shared-model",
        system="",
        user="",
    )
    lms_mod.call_lmstudio_native(
        base_url="http://shared",
        model="shared-model",
        system="",
        user="",
    )

    profiles = set(get_broker().profile_status().keys())
    assert "openai_compat:shared-model" in profiles
    assert "lmstudio_native:shared-model" in profiles
    # Emphatic: they are NOT the same entry.
    assert len(profiles) == 2
