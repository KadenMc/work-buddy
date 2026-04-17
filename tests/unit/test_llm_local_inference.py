"""Unit tests for the local-inference foundation (v1).

Covers:
- Profile resolution (known, unknown, missing backend)
- OpenAI-compatible backend HTTP call shape (schema envelope, errors)
- llm_call profile ↔ tier mutual exclusion
- Cache key scoping by backend/model
- Cost log execution_mode + backend fields
"""

from __future__ import annotations

import json

import httpx
import pytest

from work_buddy.llm import cost
from work_buddy.llm import profiles as profiles_mod
from work_buddy.llm.backends.openai_compat import call_openai_compat
from work_buddy.llm.call import llm_call


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

_SAMPLE_LLM_CONFIG = {
    "backends": {
        "lmstudio_local": {
            "provider": "openai_compat",
            "base_url": "http://localhost:1234/v1",
            "api_key_env": "",
        },
    },
    "profiles": {
        "local_general": {
            "backend": "lmstudio_local",
            "model": "qwen/qwen3-4b",
            "max_output_tokens": 1024,
            "context_length": 8192,
            "execution_mode": "local",
        },
    },
}


@pytest.fixture
def sample_llm_config(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.llm.profiles.load_config",
        lambda: {"llm": _SAMPLE_LLM_CONFIG},
    )


def test_resolve_profile_known(sample_llm_config):
    resolved = profiles_mod.resolve_profile("local_general")
    assert resolved["backend_id"] == "lmstudio_local"
    assert resolved["provider"] == "openai_compat"
    assert resolved["base_url"] == "http://localhost:1234/v1"
    assert resolved["model"] == "qwen/qwen3-4b"
    assert resolved["execution_mode"] == "local"
    assert resolved["max_output_tokens"] == 1024


def test_resolve_profile_unknown_lists_available(sample_llm_config):
    with pytest.raises(KeyError) as excinfo:
        profiles_mod.resolve_profile("does_not_exist")
    assert "local_general" in str(excinfo.value)


def test_resolve_profile_missing_backend(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.llm.profiles.load_config",
        lambda: {
            "llm": {
                "backends": {},
                "profiles": {
                    "broken": {
                        "backend": "no_such_backend",
                        "model": "foo",
                        "execution_mode": "local",
                    },
                },
            }
        },
    )
    with pytest.raises(KeyError) as excinfo:
        profiles_mod.resolve_profile("broken")
    assert "no_such_backend" in str(excinfo.value)


def test_list_profiles_sorted(sample_llm_config):
    assert profiles_mod.list_profiles() == ["local_general"]


# ---------------------------------------------------------------------------
# openai_compat backend
# ---------------------------------------------------------------------------

def _mock_response(payload: dict) -> httpx.MockTransport:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    return transport, captured


def test_openai_compat_structured_output_envelope(monkeypatch):
    transport, captured = _mock_response({
        "choices": [{"message": {"content": '{"ok": true}'}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        "model": "qwen/qwen3-4b",
    })

    # Patch httpx.Client to use the mock transport
    orig_client = httpx.Client

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _patched_client)

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    result = call_openai_compat(
        base_url="http://localhost:1234/v1",
        model="qwen/qwen3-4b",
        system="sys",
        user="usr",
        output_schema=schema,
    )

    assert result["content"] == '{"ok": true}'
    assert result["input_tokens"] == 12
    assert result["output_tokens"] == 3
    assert result["model"] == "qwen/qwen3-4b"

    body = captured["body"]
    assert body["model"] == "qwen/qwen3-4b"
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == schema


def test_openai_compat_no_schema_omits_response_format(monkeypatch):
    transport, captured = _mock_response({
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "model": "qwen/qwen3-4b",
    })
    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": transport}),
    )

    call_openai_compat(
        base_url="http://localhost:1234/v1",
        model="qwen/qwen3-4b",
        system="s",
        user="u",
    )
    assert "response_format" not in captured["body"]


def test_openai_compat_http_error(monkeypatch):
    from work_buddy.llm.backends._errors import LocalInferenceError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": transport}),
    )

    # openai_compat now wraps httpx errors in LocalInferenceError so
    # callers get a structured kind + hint instead of a raw stack trace.
    with pytest.raises(LocalInferenceError) as excinfo:
        call_openai_compat(
            base_url="http://localhost:1234/v1",
            model="qwen/qwen3-4b",
            system="s",
            user="u",
        )
    assert excinfo.value.kind == "server_error"


def test_openai_compat_malformed_response(monkeypatch):
    from work_buddy.llm.backends._errors import LocalInferenceError
    transport, _ = _mock_response({"unexpected": "shape"})
    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": transport}),
    )

    # Malformed 2xx bodies are now wrapped in LocalInferenceError
    # (kind="malformed_response") so callers get a uniform error shape
    # rather than a bare ValueError.
    with pytest.raises(LocalInferenceError) as excinfo:
        call_openai_compat(
            base_url="http://localhost:1234/v1",
            model="qwen/qwen3-4b",
            system="s",
            user="u",
        )
    assert excinfo.value.kind == "malformed_response"


# ---------------------------------------------------------------------------
# llm_call routing
# ---------------------------------------------------------------------------

def test_llm_call_rejects_tier_and_profile_together():
    result = llm_call(
        system="s", user="u", tier="haiku", profile="local_general",
    )
    assert result["error"] is not None
    assert "mutually exclusive" in result["error"]


def test_llm_call_profile_routes_to_run_task_with_profile(monkeypatch):
    captured = {}

    def fake_run_task(**kwargs):
        captured.update(kwargs)
        from work_buddy.llm.runner import TaskResult
        return TaskResult(content="ok", model="qwen/qwen3-4b")

    monkeypatch.setattr("work_buddy.llm.runner.run_task", fake_run_task)

    result = llm_call(system="s", user="u", profile="local_general")
    assert result["content"] == "ok"
    assert captured.get("profile") == "local_general"
    assert captured.get("tier") is None or "tier" not in captured


def test_llm_call_default_tier_haiku_when_neither_given(monkeypatch):
    captured = {}

    def fake_run_task(**kwargs):
        captured.update(kwargs)
        from work_buddy.llm.runner import TaskResult
        return TaskResult(content="ok", model="claude-haiku-4-5-20251001")

    monkeypatch.setattr("work_buddy.llm.runner.run_task", fake_run_task)

    llm_call(system="s", user="u")
    from work_buddy.llm.runner import ModelTier
    assert captured.get("tier") == ModelTier.HAIKU


# ---------------------------------------------------------------------------
# Cache scoping (smoke — ensures key includes backend+model)
# ---------------------------------------------------------------------------

def test_cache_scoped_by_backend_and_model(monkeypatch, tmp_path, sample_llm_config):
    """Same (system, user, schema) under two different profiles must not collide.

    We intercept the cache module and record the task_id used at put/get time.
    """
    from work_buddy.llm import runner

    puts: list[str] = []

    def fake_put(*, task_id, result, content_hash, content_sample, ttl_minutes, model, tokens):
        puts.append(task_id)

    def fake_get(task_id, content_hash=None, content_sample=None):
        return None  # always miss — we only care about key construction

    monkeypatch.setattr("work_buddy.llm.cache.put", fake_put)
    monkeypatch.setattr("work_buddy.llm.cache.get", fake_get)

    # Stub the backend so no real HTTP is attempted.
    def fake_backend(**kwargs):
        return {
            "content": "{}",
            "input_tokens": 1,
            "output_tokens": 1,
            "model": kwargs["model"],
        }

    monkeypatch.setattr(
        "work_buddy.llm.backends.call_openai_compat", fake_backend,
    )

    # Extend the sample config with a second profile pointing to a
    # different model (same backend is fine).
    cfg = {
        "llm": {
            "backends": _SAMPLE_LLM_CONFIG["backends"],
            "profiles": {
                **_SAMPLE_LLM_CONFIG["profiles"],
                "local_general_alt": {
                    "backend": "lmstudio_local",
                    "model": "qwen/qwen3-14b",
                    "max_output_tokens": 1024,
                    "execution_mode": "local",
                },
            },
        }
    }
    monkeypatch.setattr("work_buddy.llm.profiles.load_config", lambda: cfg)
    monkeypatch.setattr(runner, "_get_llm_config", lambda: cfg["llm"])

    runner.run_task(task_id="shared", system="s", user="u", profile="local_general")
    runner.run_task(task_id="shared", system="s", user="u", profile="local_general_alt")

    assert len(puts) == 2
    assert puts[0] != puts[1], "Cache keys must differ across profiles with different models"
    assert "qwen/qwen3-4b" in puts[0]
    assert "qwen/qwen3-14b" in puts[1]


# ---------------------------------------------------------------------------
# Cost log execution_mode + backend
# ---------------------------------------------------------------------------

def test_cost_log_local_mode_is_zero(monkeypatch, tmp_path):
    log_file = tmp_path / "llm_costs.jsonl"
    monkeypatch.setattr("work_buddy.llm.cost._cost_log_path", lambda: log_file)

    cost.log_call(
        model="qwen/qwen3-4b",
        input_tokens=100,
        output_tokens=50,
        task_id="t",
        execution_mode="local",
        backend="lmstudio_local",
    )

    entries = [json.loads(line) for line in log_file.read_text().splitlines() if line]
    assert len(entries) == 1
    e = entries[0]
    assert e["execution_mode"] == "local"
    assert e["backend"] == "lmstudio_local"
    assert e["estimated_cost_usd"] == 0.0


def test_cost_log_cloud_mode_uses_price_table(monkeypatch, tmp_path):
    log_file = tmp_path / "llm_costs.jsonl"
    monkeypatch.setattr("work_buddy.llm.cost._cost_log_path", lambda: log_file)

    cost.log_call(
        model="claude-haiku-4-5-20251001",
        input_tokens=1_000_000,
        output_tokens=0,
        task_id="t",
    )

    entry = json.loads(log_file.read_text().splitlines()[0])
    assert entry["execution_mode"] == "cloud"
    assert entry["estimated_cost_usd"] == pytest.approx(0.80, rel=1e-3)
