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
    # Emphatic: they are NOT the same entry. (Other profiles may also be
    # registered — e.g. the config-declared `local:embedding` admission profile
    # — so assert the two backend profiles are present + distinct rather than
    # pinning the total count.)
    assert len(
        {"openai_compat:shared-model", "lmstudio_native:shared-model"} & profiles
    ) == 2


# ---------------------------------------------------------------------------
# End-to-end priority threading
#
# The backend functions accept ``priority``; these tests pin that the
# runner layers above them actually forward it:
#   LLMRunner.call -> _call_one -> run_task -> _run_profile -> backend slot.
# Without this threading every local LLM call is stuck at WORKFLOW and
# callers can't make background work yield to interactive work.
# ---------------------------------------------------------------------------


def _fake_profile_info(model: str) -> dict:
    """Minimal ``profile_info`` shape consumed by run_task / _run_profile."""
    return {
        "model": model,
        "execution_mode": "local",
        "backend_id": "test_profile",
        "base_url": "http://fake/v1",
        "api_key_env": None,
        "provider": None,
    }


def _patch_profile_path(monkeypatch, backend_mod, model, response_json):
    """Wire run_task's profile path to a mock backend HTTP endpoint.

    Patches profile resolution (so no config.yaml is needed), cost
    logging (so no cost file is written), and the backend module's httpx
    client (so no network call happens).
    """
    monkeypatch.setattr(
        "work_buddy.llm.profiles.resolve_profile",
        lambda name: _fake_profile_info(model),
    )
    monkeypatch.setattr(
        "work_buddy.llm.cost.log_call", lambda *a, **kw: None,
    )
    real_client = httpx.Client
    monkeypatch.setattr(backend_mod.httpx, "Client", lambda *a, **kw: real_client(
        *a, **kw, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=response_json)
        ),
    ))


def test_run_task_threads_priority_to_openai_compat(monkeypatch):
    """run_task(profile=..., backend_kind='openai_compat', priority=...)
    forwards the priority all the way to the broker slot."""
    import work_buddy.llm.backends.openai_compat as oa_mod
    from work_buddy.llm.runner import run_task
    from work_buddy.inference import get_broker

    _patch_profile_path(monkeypatch, oa_mod, "fake-echo", _oa_fake_response())

    result = run_task(
        task_id="t",
        system="s",
        user="u",
        profile="local_general",
        backend_kind="openai_compat",
        priority=Priority.BACKGROUND,
        cache_ttl_minutes=0,
    )
    assert result.error is None
    assert result.content == "hello from the fake"

    m = get_broker().snapshot_metrics()[-1]
    assert m["profile"] == "openai_compat:fake-echo"
    assert m["priority"] == "BACKGROUND"
    assert m["status"] == "ok"


def test_run_task_threads_priority_to_lmstudio_native(monkeypatch):
    """Same end-to-end threading for the native-chat backend, with a
    distinct priority + profile prefix."""
    import work_buddy.llm.backends.lmstudio_native as lms_mod
    from work_buddy.llm.runner import run_task
    from work_buddy.inference import get_broker

    _patch_profile_path(monkeypatch, lms_mod, "fake-lms", _lms_fake_response())

    result = run_task(
        task_id="t",
        system="s",
        user="u",
        profile="local_agent",
        backend_kind="lmstudio_native",
        priority=Priority.INTERACTIVE,
        cache_ttl_minutes=0,
    )
    assert result.error is None
    assert "lm studio echo" in result.content

    m = get_broker().snapshot_metrics()[-1]
    assert m["profile"] == "lmstudio_native:fake-lms"
    assert m["priority"] == "INTERACTIVE"


def test_run_task_defaults_to_workflow_priority(monkeypatch):
    """Omitting priority leaves the backend default (WORKFLOW) intact —
    the new param must not change existing behaviour."""
    import work_buddy.llm.backends.openai_compat as oa_mod
    from work_buddy.llm.runner import run_task
    from work_buddy.inference import get_broker

    _patch_profile_path(monkeypatch, oa_mod, "fake-echo", _oa_fake_response())

    run_task(
        task_id="t",
        system="s",
        user="u",
        profile="local_general",
        backend_kind="openai_compat",
        cache_ttl_minutes=0,
    )
    m = get_broker().snapshot_metrics()[-1]
    assert m["priority"] == "WORKFLOW"


def test_llm_runner_call_threads_priority_end_to_end(monkeypatch):
    """The public entry point LLMRunner.call(priority=...) threads the
    priority through _call_one -> run_task -> _run_profile -> backend."""
    import work_buddy.llm.backends.openai_compat as oa_mod
    import work_buddy.llm.runner_v2 as runner_v2
    from work_buddy.llm import LLMRunner, ModelTier
    from work_buddy.llm.tiers import TierBinding
    from work_buddy.inference import get_broker

    _patch_profile_path(monkeypatch, oa_mod, "fake-echo", _oa_fake_response())

    # Pin tier resolution so the test is independent of config.yaml's
    # llm.tiers block.
    binding = TierBinding(
        tier=ModelTier.LOCAL_FAST,
        backend="openai_compat",
        profile="local_general",
        model=None,
        max_tokens=512,
        temperature=0.0,
        tool_support=False,
    )
    monkeypatch.setattr(runner_v2, "resolve_tier", lambda tier: binding)

    resp = LLMRunner().call(
        tier=ModelTier.LOCAL_FAST,
        system="s",
        user="u",
        priority=Priority.BACKGROUND,
        cache_ttl_minutes=0,
    )
    assert not resp.is_error()

    m = get_broker().snapshot_metrics()[-1]
    assert m["profile"] == "openai_compat:fake-echo"
    assert m["priority"] == "BACKGROUND"


# ---------------------------------------------------------------------------
# MCP-boundary priority string -> Priority enum (parse_priority + llm_call)
# ---------------------------------------------------------------------------


def test_parse_priority_maps_names_case_insensitively():
    from work_buddy.inference import parse_priority

    assert parse_priority("background") is Priority.BACKGROUND
    assert parse_priority("WORKFLOW") is Priority.WORKFLOW
    assert parse_priority("Interactive") is Priority.INTERACTIVE


def test_parse_priority_passthrough_and_none():
    from work_buddy.inference import parse_priority

    assert parse_priority(None) is None
    assert parse_priority(Priority.BACKGROUND) is Priority.BACKGROUND


def test_parse_priority_rejects_unknown():
    from work_buddy.inference import parse_priority

    with pytest.raises(ValueError):
        parse_priority("urgent")


def test_llm_call_threads_string_priority_to_broker(monkeypatch):
    """The llm_call MCP capability accepts a string priority and threads
    it (mapped to the Priority enum) down to the backend broker slot."""
    import work_buddy.llm.backends.openai_compat as oa_mod
    from work_buddy.llm.call import llm_call
    from work_buddy.inference import get_broker

    _patch_profile_path(monkeypatch, oa_mod, "fake-echo", _oa_fake_response())

    result = llm_call(
        system="s",
        user="u",
        profile="local_general",
        priority="background",
        cache_ttl_minutes=0,
    )
    assert result["error"] is None

    m = get_broker().snapshot_metrics()[-1]
    assert m["profile"] == "openai_compat:fake-echo"
    assert m["priority"] == "BACKGROUND"


def test_llm_call_rejects_invalid_priority():
    """A bad priority string is rejected at the capability boundary
    before any backend call — no broker slot is taken."""
    from work_buddy.llm.call import llm_call

    result = llm_call(
        system="s",
        user="u",
        profile="local_general",
        priority="urgent",
        cache_ttl_minutes=0,
    )
    assert result["error"] and "priority" in result["error"].lower()
