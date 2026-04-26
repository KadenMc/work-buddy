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
# backend_kind routing — tier-binding wins over profile-config provider
# ---------------------------------------------------------------------------


def _stub_cache(monkeypatch):
    """Stub the cache module to always miss + record nothing, so tests don't
    need real cache infrastructure and don't pollute each other.

    Accepts the current cache API shape (scoped_task_id positional, rest
    keyword-only). Signature mirrors :func:`work_buddy.llm.cache.get` and
    :func:`work_buddy.llm.cache.put`."""
    monkeypatch.setattr(
        "work_buddy.llm.cache.get",
        lambda scoped_task_id, **kw: None,
    )
    monkeypatch.setattr(
        "work_buddy.llm.cache.put",
        lambda scoped_task_id, **kw: None,
    )


def test_run_task_with_backend_kind_native_dispatches_to_native(
    monkeypatch, sample_llm_config,
) -> None:
    """``backend_kind='lmstudio_native'`` routes through call_lmstudio_native,
    even though the fixture's config says provider=openai_compat."""
    from work_buddy.llm import runner

    _stub_cache(monkeypatch)
    monkeypatch.setattr(runner, "_get_llm_config", lambda: {})

    native_calls: list[dict] = []

    def fake_native(**kwargs):
        native_calls.append(kwargs)
        return {
            "content": "{}", "input_tokens": 1, "output_tokens": 1,
            "model": kwargs["model"], "reasoning": "", "tool_calls": [],
            "response_id": "r", "reasoning_tokens": 0,
        }

    def fail_compat(**kwargs):
        raise AssertionError("openai_compat must NOT be called here")

    monkeypatch.setattr("work_buddy.llm.backends.call_lmstudio_native", fake_native)
    monkeypatch.setattr("work_buddy.llm.backends.call_openai_compat", fail_compat)

    runner.run_task(
        task_id="t", system="s", user="u",
        profile="local_general",
        backend_kind="lmstudio_native",
    )
    assert len(native_calls) == 1


def test_run_task_without_backend_kind_defaults_to_openai_compat(
    monkeypatch, sample_llm_config,
) -> None:
    """Legacy callers (no ``backend_kind``) get openai-compat so JIT auto-load
    works against LM Studio."""
    from work_buddy.llm import runner

    _stub_cache(monkeypatch)
    monkeypatch.setattr(runner, "_get_llm_config", lambda: {})

    compat_calls: list[dict] = []

    def fake_compat(**kwargs):
        compat_calls.append(kwargs)
        return {
            "content": "{}", "input_tokens": 1, "output_tokens": 1,
            "model": kwargs["model"],
        }

    def fail_native(**kwargs):
        raise AssertionError("lmstudio_native must NOT be called here")

    monkeypatch.setattr("work_buddy.llm.backends.call_openai_compat", fake_compat)
    monkeypatch.setattr("work_buddy.llm.backends.call_lmstudio_native", fail_native)

    runner.run_task(
        task_id="t", system="s", user="u",
        profile="local_general",  # no backend_kind
    )
    assert len(compat_calls) == 1


def test_run_task_warns_when_config_provider_mismatches_binding(
    monkeypatch, caplog,
) -> None:
    """Config says provider=lmstudio_native but caller requests openai_compat;
    dispatch honors the caller and emits a warning about the stale config."""
    import logging
    from work_buddy.llm import runner

    _stub_cache(monkeypatch)
    monkeypatch.setattr(runner, "_get_llm_config", lambda: {})
    # Config with stale provider — simulates a user's legacy config.local.yaml.
    monkeypatch.setattr(
        "work_buddy.llm.profiles.load_config",
        lambda: {
            "llm": {
                "backends": {
                    "lmstudio_local": {
                        "provider": "lmstudio_native",  # stale / wrong for LOCAL_FAST
                        "base_url": "http://localhost:1234/v1",
                        "api_key_env": "",
                    },
                },
                "profiles": {
                    "local_general": {
                        "backend": "lmstudio_local",
                        "model": "qwen/qwen3-4b",
                        "max_output_tokens": 1024,
                        "execution_mode": "local",
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        "work_buddy.llm.backends.call_openai_compat",
        lambda **kw: {
            "content": "{}", "input_tokens": 1, "output_tokens": 1,
            "model": kw["model"],
        },
    )

    with caplog.at_level(logging.WARNING, logger="work_buddy.llm.runner"):
        runner.run_task(
            task_id="t", system="s", user="u",
            profile="local_general",
            backend_kind="openai_compat",
        )

    warns = [r for r in caplog.records if "provider" in r.getMessage()]
    assert warns, "expected a warning about config/binding provider mismatch"
    msg = warns[0].getMessage()
    assert "lmstudio_native" in msg and "openai_compat" in msg


def test_llm_runner_threads_binding_backend_to_run_task(monkeypatch) -> None:
    """``LLMRunner.call(tier=LOCAL_FAST)`` forwards ``binding.backend`` as
    ``backend_kind``, making the tier the source of truth for dispatch."""
    from work_buddy.llm import ModelTier
    from work_buddy.llm.runner_v2 import LLMRunner

    captured: dict = {}

    def fake_run_task(**kwargs):
        captured.update(kwargs)
        from work_buddy.llm.runner import TaskResult
        return TaskResult(content="{}", model="qwen/qwen3-4b")

    monkeypatch.setattr("work_buddy.llm.runner.run_task", fake_run_task)

    LLMRunner().call(
        tier=ModelTier.LOCAL_FAST,
        system="s", user="u",
    )
    # LOCAL_FAST's tier binding says backend=openai_compat; that must reach run_task.
    assert captured.get("backend_kind") == "openai_compat"
    assert captured.get("profile") == "local_general"


def test_llm_runner_local_tool_calling_routes_to_native(monkeypatch) -> None:
    """LOCAL_TOOL_CALLING has backend=lmstudio_native in defaults — that
    reaches run_task via ``backend_kind`` so MCP tool-calling stays on the
    native endpoint."""
    from work_buddy.llm import ModelTier
    from work_buddy.llm.runner_v2 import LLMRunner

    captured: dict = {}

    def fake_run_task(**kwargs):
        captured.update(kwargs)
        from work_buddy.llm.runner import TaskResult
        return TaskResult(content="{}", model="local_agent_model")

    monkeypatch.setattr("work_buddy.llm.runner.run_task", fake_run_task)

    LLMRunner().call(
        tier=ModelTier.LOCAL_TOOL_CALLING,
        system="s", user="u",
    )
    assert captured.get("backend_kind") == "lmstudio_native"
    assert captured.get("profile") == "local_agent"


# ---------------------------------------------------------------------------
# Cache scoping (smoke — ensures key includes backend+model)
# ---------------------------------------------------------------------------

def test_cache_scoped_by_backend_and_model(monkeypatch, tmp_path, sample_llm_config):
    """Same (system, user, schema) under two different profiles must not collide.

    We intercept the cache module and record the task_id used at put/get time.
    """
    from work_buddy.llm import runner

    puts: list[str] = []

    def fake_put(scoped_task_id, *, result, input_hash, input_text,
                 system_hash, system_preview, ttl_minutes, model="", tokens=None):
        puts.append(scoped_task_id)

    def fake_get(scoped_task_id, *, input_hash, input_text=None,
                 hamming_threshold=None):
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
# Content-aware cache — input hash + system hash + SimHash fuzzy match
# ---------------------------------------------------------------------------


@pytest.fixture
def _cache_recorder(monkeypatch):
    """Stub cache.get/put with an in-memory dict recorder.

    Records puts as a list of (scoped_task_id, entry_dict); lookups use
    the real cache.get semantics mirrored against the in-memory store so
    we can exercise hit/miss/fuzzy paths without touching disk.
    """
    from work_buddy.llm import cache as cache_mod
    from work_buddy.llm import runner

    store: dict[str, dict] = {}
    puts: list[tuple[str, dict]] = []

    def fake_put(scoped_task_id, *, result, input_hash, input_text,
                 system_hash, system_preview, ttl_minutes, model="", tokens=None):
        import datetime as _dt
        now = _dt.datetime.now()
        entry = {
            "result": result,
            "input_hash": input_hash,
            "input_simhash": cache_mod._compute_simhash(input_text),
            "system_hash": system_hash,
            "system_preview": system_preview,
            "model": model,
            "tokens": tokens or {},
            "created_at": now.isoformat(),
            "expires_at": (now + _dt.timedelta(minutes=ttl_minutes)).isoformat(),
        }
        store[scoped_task_id] = entry
        puts.append((scoped_task_id, entry))

    def fake_get(scoped_task_id, *, input_hash, input_text=None,
                 hamming_threshold=None):
        entry = store.get(scoped_task_id)
        if entry is None:
            return None
        if entry["input_hash"] == input_hash:
            return entry
        if input_text is not None:
            incoming = cache_mod._compute_simhash(input_text)
            t = hamming_threshold if hamming_threshold is not None else 3
            if cache_mod._hamming_distance(entry["input_simhash"], incoming) <= t:
                return entry
        return None

    monkeypatch.setattr("work_buddy.llm.cache.put", fake_put)
    monkeypatch.setattr("work_buddy.llm.cache.get", fake_get)

    def fake_backend(**kwargs):
        return {
            "content": "{}", "input_tokens": 1, "output_tokens": 1,
            "model": kwargs["model"],
        }

    monkeypatch.setattr(
        "work_buddy.llm.backends.call_openai_compat", fake_backend,
    )
    monkeypatch.setattr(runner, "_get_llm_config", lambda: {})

    return store, puts


def test_cache_hits_same_system_same_user(
    monkeypatch, sample_llm_config, _cache_recorder,
) -> None:
    """Identical (system, user) inputs resolve to a single cache entry
    and the second call serves a hit."""
    from work_buddy.llm import runner

    store, puts = _cache_recorder

    r1 = runner.run_task(
        task_id="t", system="sys-A", user="u-A", profile="local_general",
    )
    r2 = runner.run_task(
        task_id="t", system="sys-A", user="u-A", profile="local_general",
    )
    assert not r1.cached
    assert r2.cached
    assert len(puts) == 1


def test_cache_misses_on_different_user(
    monkeypatch, sample_llm_config, _cache_recorder,
) -> None:
    """Same system, different user text → cache miss → LLM re-queried."""
    from work_buddy.llm import runner

    store, puts = _cache_recorder

    runner.run_task(
        task_id="t", system="sys-A", user="user prompt ONE", profile="local_general",
    )
    runner.run_task(
        task_id="t", system="sys-A", user="completely different content",
        profile="local_general",
    )
    assert len(puts) == 2
    # Same scoped key (system + tier + task_id unchanged); two put calls
    # mean both runs actually invoked the backend.
    assert puts[0][0] == puts[1][0]


def test_cache_scope_partitioned_by_system_hash(
    monkeypatch, sample_llm_config, _cache_recorder,
) -> None:
    """Changing the system prompt produces a different scoped key — old
    entries can't collide with new ones even at the same tier + task_id."""
    from work_buddy.llm import runner

    store, puts = _cache_recorder

    runner.run_task(
        task_id="t", system="sys-A", user="u", profile="local_general",
    )
    runner.run_task(
        task_id="t", system="sys-DIFFERENT", user="u", profile="local_general",
    )
    assert len(puts) == 2
    assert puts[0][0] != puts[1][0], (
        "system prompt change must partition the cache scope"
    )


def test_cache_fuzzy_hit_on_trivial_user_change(
    monkeypatch, sample_llm_config, _cache_recorder,
) -> None:
    """A tiny user-prompt change (e.g., whitespace / number rotation)
    stays within the SimHash Hamming threshold → fuzzy hit, no re-query."""
    from work_buddy.llm import runner

    store, puts = _cache_recorder

    long_user = " ".join(["topic one blah bar qux"] * 50)
    runner.run_task(
        task_id="t", system="sys-A", user=long_user, profile="local_general",
    )
    # Change one trailing punctuation — SimHash should stay within threshold.
    r2 = runner.run_task(
        task_id="t", system="sys-A", user=long_user + " .",
        profile="local_general",
    )
    assert len(puts) == 1
    assert r2.cached


def test_cache_legacy_entries_treated_as_miss(monkeypatch, tmp_path) -> None:
    """Legacy on-disk entries (pre-refactor, no ``input_hash`` field)
    must never satisfy a lookup — they naturally age out via TTL."""
    from work_buddy.llm import cache as cache_mod

    fake_cache_file = tmp_path / "llm_cache.json"
    import datetime as _dt
    future = (_dt.datetime.now() + _dt.timedelta(minutes=30)).isoformat()
    legacy = {
        "some_scoped_key": {
            "result": {"content": "stale", "parsed": None},
            # No input_hash. No input_simhash. Legacy.
            "content_hash": "old-style",
            "content_sample": "old-sample",
            "simhash": 123456789,
            "expires_at": future,
            "model": "anything",
            "tokens": {},
            "created_at": _dt.datetime.now().isoformat(),
        },
    }
    fake_cache_file.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", fake_cache_file)

    result = cache_mod.get(
        "some_scoped_key",
        input_hash="fresh-hash",
        input_text="fresh text",
    )
    assert result is None, "legacy-schema entries must not satisfy a get()"


def test_cache_put_rejects_missing_input_hash(monkeypatch, tmp_path) -> None:
    """The required-kwarg contract: calling put without ``input_hash``
    raises at the call site — no silent wrong-cache-hits downstream."""
    from work_buddy.llm import cache as cache_mod

    fake_cache_file = tmp_path / "llm_cache.json"
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", fake_cache_file)

    with pytest.raises(TypeError):
        # Intentionally missing input_hash to verify the guard.
        cache_mod.put(  # type: ignore[call-arg]
            "key",
            result={"content": "x"},
            input_text="user text",
            system_hash="abc",
            system_preview="sys",
            ttl_minutes=10,
        )


def test_cache_entry_stores_system_provenance(monkeypatch, tmp_path) -> None:
    """Put stores system_hash + system_preview; operators tracing a
    stale result can identify which prompt revision produced it."""
    from work_buddy.llm import cache as cache_mod

    fake_cache_file = tmp_path / "llm_cache.json"
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", fake_cache_file)

    cache_mod.put(
        "key",
        result={"content": "x"},
        input_hash="h",
        input_text="user text",
        system_hash="sys-fingerprint",
        system_preview="You are an assistant. Please...",
        ttl_minutes=10,
    )
    stored = json.loads(fake_cache_file.read_text())["key"]
    assert stored["system_hash"] == "sys-fingerprint"
    assert stored["system_preview"].startswith("You are an assistant")
    assert "input_simhash" in stored and stored["input_simhash"] is not None
    # The raw input text must NOT be persisted.
    assert "input_text" not in stored
    assert "content_sample" not in stored


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
    # 1M input tokens at the Haiku 4.5 rate ($1.00/M, per the canonical
    # table at work_buddy.llm.claude_code_usage.pricing). The earlier
    # 0.80 expectation predated the pricing consolidation that retired
    # the Haiku 3.5 rate.
    assert entry["estimated_cost_usd"] == pytest.approx(1.00, rel=1e-3)
