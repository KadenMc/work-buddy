"""Response hygiene + retry policy for llm_with_tools.

Pins three behaviors that were fixed after a live test run revealed
bleeding edges:

1. Local-LLM capabilities (``llm_with_tools``, ``llm_submit``) must
   NOT be auto-enqueued on transient failure. A failing model run
   wastes tokens on each replay and spams consent prompts; the
   caller should see the failure and decide what to do.

2. Reasoning tokens (the local model's chain-of-thought) must be
   stripped from the default response. They're often hundreds of
   tokens the calling agent never needs. When persistence is active
   (persist_tool_results=True or any tool errored), reasoning is
   saved as an artifact instead.

3. Persisted tool-call outputs must be UNWRAPPED from LM Studio's
   MCP envelope (``[{type:"text",text:"..."}]``) so artifacts contain
   readable JSON rather than double-encoded escape hell.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# auto_retry field + gateway policy
# ---------------------------------------------------------------------------

def test_capability_has_auto_retry_field_default_true():
    """New field exists and defaults to True so existing caps keep behaving."""
    from work_buddy.mcp_server.registry import Capability
    cap = Capability(
        name="test", description="", category="test", parameters={}, callable=lambda: None,
    )
    assert cap.auto_retry is True


def test_llm_with_tools_and_llm_submit_opt_out_of_auto_retry():
    """These two capabilities must not be in the auto-retry set — retrying
    them wastes tokens and spams consent prompts."""
    from work_buddy.mcp_server.registry import get_registry
    reg = get_registry()

    for name in ("llm_with_tools", "llm_submit"):
        entry = reg.get(name)
        assert entry is not None, f"{name} should be registered"
        assert entry.auto_retry is False, (
            f"{name} must have auto_retry=False — retrying a failed local-LLM "
            f"call is wasteful and dangerous."
        )


def test_llm_call_keeps_auto_retry_default():
    """The cloud tier path of llm_call does benefit from retrying
    transient errors (API blips, 429s), so it stays opt-in."""
    from work_buddy.mcp_server.registry import get_registry
    entry = get_registry().get("llm_call")
    assert entry is not None
    assert entry.auto_retry is True


# ---------------------------------------------------------------------------
# Reasoning demotion
# ---------------------------------------------------------------------------

_PROFILE_CONFIG = {
    "llm": {
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
                "max_output_tokens": 2048,
                "execution_mode": "local",
            },
        },
    },
}


@pytest.fixture
def profile_cfg(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.llm.profiles.load_config", lambda: _PROFILE_CONFIG,
    )


def _install_mock_backend(monkeypatch, body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)
    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": transport}),
    )


def test_default_response_omits_reasoning_field(profile_cfg, monkeypatch, tmp_path):
    """When no persistence is active, reasoning must not appear in the
    returned dict at all — zero tokens wasted on calling-agent side."""
    _install_mock_backend(monkeypatch, {
        "model_instance_id": "qwen/qwen3-4b",
        "response_id": "r",
        "output": [
            {"type": "reasoning", "content": "a very long chain of thought " * 100},
            {"type": "message", "content": "final answer"},
        ],
        "stats": {"input_tokens": 5, "total_output_tokens": 600,
                  "reasoning_output_tokens": 590},
    })
    monkeypatch.setattr(
        "work_buddy.llm.cost._cost_log_path",
        lambda: tmp_path / "llm_costs.jsonl",
    )

    from work_buddy.llm.with_tools import llm_with_tools
    result = llm_with_tools(
        system="s", user="u",
        profile="local_general", tool_preset="readonly_safe",
    )

    assert result["content"] == "final answer"
    assert "reasoning" not in result, (
        "reasoning must be stripped from default responses"
    )
    # But the count metadata stays so the caller can see if it's wasteful
    assert result["reasoning_tokens"] == 590
    # No artifact saved either (no persist, no errors)
    assert result["reasoning_artifact_id"] is None
    assert result["tool_calls_persisted"] is False


def test_reasoning_saved_as_artifact_when_persist_true(
    profile_cfg, monkeypatch, tmp_path,
):
    _install_mock_backend(monkeypatch, {
        "model_instance_id": "qwen/qwen3-4b",
        "response_id": "r",
        "output": [
            {"type": "reasoning", "content": "my deep reasoning text"},
            {"type": "message", "content": "hi"},
        ],
        "stats": {"input_tokens": 3, "total_output_tokens": 10,
                  "reasoning_output_tokens": 7},
    })
    monkeypatch.setattr(
        "work_buddy.llm.cost._cost_log_path",
        lambda: tmp_path / "llm_costs.jsonl",
    )

    saved: list[dict] = []

    class _Rec:
        def __init__(self, id_):
            self.id = id_

    def spy_save(content, type, slug, ext="json", *, tags=None, description="",
                 session_id=None, **kw):
        saved.append({
            "content": content, "type": type, "slug": slug, "ext": ext,
            "tags": tags or [], "session_id": session_id,
        })
        return _Rec(f"artifact_{slug}_{len(saved)}")

    monkeypatch.setattr("work_buddy.artifacts.save", spy_save)

    from work_buddy.llm.with_tools import llm_with_tools
    result = llm_with_tools(
        system="s", user="u",
        profile="local_general", tool_preset="readonly_safe",
        persist_tool_results=True,
    )

    # Reasoning artifact saved
    reasoning_saves = [s for s in saved if s["slug"] == "llm_reasoning"]
    assert len(reasoning_saves) == 1
    rs = reasoning_saves[0]
    assert rs["content"] == "my deep reasoning text"
    assert rs["type"] == "scratch"
    assert rs["ext"] == "md"
    assert "reasoning" in rs["tags"]
    assert rs["session_id"] == result["session_id"]

    # Artifact id surfaced in the response
    assert result["reasoning_artifact_id"] is not None
    assert result["reasoning_artifact_id"].startswith("artifact_llm_reasoning")


def test_reasoning_saved_as_artifact_on_auto_escalation(
    profile_cfg, monkeypatch, tmp_path,
):
    """Any tool error in the batch → reasoning persisted too, even
    without explicit persist_tool_results=True."""
    # Build a tool_call with an error
    error_payload = json.dumps([
        {"type": "text", "text": json.dumps({"error": "denied"})},
    ])
    _install_mock_backend(monkeypatch, {
        "model_instance_id": "qwen/qwen3-4b",
        "response_id": "r",
        "output": [
            {"type": "reasoning", "content": "why did that fail hmm"},
            {"type": "tool_call", "tool": "wb_run",
             "arguments": {"capability": "task_toggle"},
             "output": error_payload,
             "provider_info": {"server_label": "work-buddy", "type": "ephemeral_mcp"}},
            {"type": "message", "content": "it failed"},
        ],
        "stats": {"input_tokens": 5, "total_output_tokens": 12},
    })
    monkeypatch.setattr(
        "work_buddy.llm.cost._cost_log_path",
        lambda: tmp_path / "llm_costs.jsonl",
    )

    saved: list[dict] = []

    class _Rec:
        def __init__(self, id_):
            self.id = id_

    def spy_save(content, type, slug, ext="json", *, tags=None, description="",
                 session_id=None, **kw):
        saved.append({"slug": slug, "content": content})
        return _Rec(f"artifact_{slug}_{len(saved)}")

    monkeypatch.setattr("work_buddy.artifacts.save", spy_save)

    from work_buddy.llm.with_tools import llm_with_tools
    result = llm_with_tools(
        system="s", user="u",
        profile="local_general", tool_preset="readonly_safe",
        persist_tool_results=False,  # explicit False — but error escalates
    )

    assert result["any_tool_errored"] is True
    assert result["tool_calls_persisted"] is True
    # Reasoning got persisted because the batch escalated
    reasoning_saves = [s for s in saved if s["slug"] == "llm_reasoning"]
    assert len(reasoning_saves) == 1
    assert reasoning_saves[0]["content"] == "why did that fail hmm"
    assert result["reasoning_artifact_id"] is not None


# ---------------------------------------------------------------------------
# Artifact content is clean unwrapped JSON, not escaped strings
# ---------------------------------------------------------------------------

def test_persisted_artifact_unwraps_mcp_envelope(monkeypatch):
    """Artifact content should be readable JSON of the inner MCP
    result dict, not a triple-escaped string-of-string-of-string."""
    from work_buddy.llm._tool_call_trim import trim_tool_calls

    saved: list[str] = []

    class _Rec:
        def __init__(self, id_):
            self.id = id_

    def spy_save(content, *a, **k):
        saved.append(content)
        return _Rec(f"a{len(saved)}")

    monkeypatch.setattr("work_buddy.artifacts.save", spy_save)

    inner_payload = {"result": {"status": "ok", "services": ["a", "b"]}}
    wrapped = json.dumps([
        {"type": "text", "text": json.dumps(inner_payload)},
    ])
    tool_calls = [{
        "type": "tool_call",
        "tool": "wb_run",
        "arguments": {"capability": "sidecar_status"},
        "output": wrapped,
        "provider_info": {"server_label": "work-buddy"},
    }]

    trim_tool_calls(
        tool_calls, persist_tool_results=True,
        session_id="lms-x", tool_preset="readonly_safe",
    )

    # The saved artifact should be the INNER parsed dict pretty-printed,
    # not the raw wrapped string.
    assert len(saved) == 1
    content = saved[0]
    # Must NOT contain the literal escaped string artifacts of MCP
    # envelope — those are the smell of double-encoding
    assert "\\\"type\\\":\\\"text\\\"" not in content
    assert '"\\{' not in content  # no escaped brace-in-string
    # Must BE parseable as JSON and equal the unwrapped inner dict
    reparsed = json.loads(content)
    assert reparsed == inner_payload
