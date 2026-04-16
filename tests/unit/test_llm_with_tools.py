"""Tests for llm_with_tools and the tool-preset security layer.

Covers:
- Tool presets are structurally valid (wb_init included, readonly has no mutations)
- Every tool name in every preset is a real registered capability
- resolve_preset rejects unknown names with a helpful error
- llm_with_tools rejects missing profile / tool_preset
- llm_with_tools sends the expected payload to /api/v1/chat
- Cost log records execution_mode=local, $0.00 for tool-enabled calls
- wb_init is present in allowed_tools (required for session registration)
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Tool presets — structural + registry-alignment
# ---------------------------------------------------------------------------

def test_every_preset_includes_wb_init():
    from work_buddy.llm.tool_presets import PRESETS
    for name, tools in PRESETS.items():
        assert "wb_init" in tools, (
            f"Preset {name!r} is missing wb_init — models need it to "
            f"register their MCP session."
        )


def test_readonly_presets_have_no_mutating_capabilities():
    from work_buddy.llm.tool_presets import validate_presets
    problems = validate_presets()
    assert problems == [], f"Preset validation problems: {problems}"


def test_all_preset_names_exist_in_capability_registry():
    """Guards against typos and drift when capabilities are renamed."""
    from work_buddy.llm.tool_presets import validate_presets
    from work_buddy.mcp_server.registry import get_registry

    reg = get_registry()
    assert isinstance(reg, dict), (
        f"Registry shape changed unexpectedly — got {type(reg)}"
    )
    registry_names = set(reg.keys())

    problems = validate_presets(registry_names)
    assert problems == [], (
        "Preset drift from registry — capabilities renamed or removed:\n"
        + "\n".join(problems)
    )


def test_resolve_preset_unknown_name_raises():
    from work_buddy.llm.tool_presets import resolve_preset
    with pytest.raises(KeyError) as excinfo:
        resolve_preset("does_not_exist")
    msg = str(excinfo.value)
    assert "readonly_safe" in msg
    assert "readonly_context" in msg


def test_resolve_preset_known_returns_sorted_list():
    from work_buddy.llm.tool_presets import resolve_preset
    tools = resolve_preset("readonly_safe")
    assert isinstance(tools, list)
    assert tools == sorted(tools)
    assert "wb_init" in tools


def test_readonly_context_is_superset_of_readonly_safe():
    from work_buddy.llm.tool_presets import PRESETS
    safe = PRESETS["readonly_safe"]
    ctx = PRESETS["readonly_context"]
    assert safe <= ctx, (
        "readonly_context should be a superset of readonly_safe — "
        "context adds collectors on top of the safe baseline."
    )


# ---------------------------------------------------------------------------
# llm_with_tools — input validation
# ---------------------------------------------------------------------------

def test_llm_with_tools_rejects_missing_profile():
    from work_buddy.llm.with_tools import llm_with_tools
    result = llm_with_tools(
        system="s", user="u", profile="", tool_preset="readonly_safe",
    )
    assert result["error"]
    assert "profile" in result["error"].lower()


def test_llm_with_tools_rejects_missing_tool_preset():
    from work_buddy.llm.with_tools import llm_with_tools
    result = llm_with_tools(
        system="s", user="u", profile="local_general", tool_preset="",
    )
    assert result["error"]
    assert "preset" in result["error"].lower()


def test_llm_with_tools_rejects_unknown_preset(monkeypatch):
    from work_buddy.llm.with_tools import llm_with_tools
    monkeypatch.setattr(
        "work_buddy.llm.profiles.load_config",
        lambda: {"llm": {"backends": {}, "profiles": {}}},
    )
    result = llm_with_tools(
        system="s", user="u", profile="local_general", tool_preset="hackme",
    )
    assert result["error"]
    assert "hackme" in result["error"]


# ---------------------------------------------------------------------------
# llm_with_tools — request construction
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
                "context_length": 8192,
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


def test_llm_with_tools_posts_to_native_endpoint_with_integration(
    profile_cfg, monkeypatch, tmp_path,
):
    """The request should POST to /api/v1/chat (not /v1/chat/completions)
    with an integrations array containing the whitelisted tools."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "output": "final answer",
            "response_id": "resp_abc",
            "model": "qwen/qwen3-4b",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "tool_calls": [],
        })

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": transport}),
    )

    # Route cost log to a tmp dir to avoid polluting real session state
    monkeypatch.setattr(
        "work_buddy.llm.cost._cost_log_path",
        lambda: tmp_path / "llm_costs.jsonl",
    )

    from work_buddy.llm.with_tools import llm_with_tools

    result = llm_with_tools(
        system="You are a helper.",
        user="what's happening today",
        profile="local_general",
        tool_preset="readonly_safe",
        max_tokens=500,
    )

    assert result["error"] is None
    assert result["content"] == "final answer"
    assert result["response_id"] == "resp_abc"
    assert result["tool_preset"] == "readonly_safe"

    # URL should strip /v1 and append /api/v1/chat
    assert captured["url"] == "http://localhost:1234/api/v1/chat"

    body = captured["body"]
    assert body["model"] == "qwen/qwen3-4b"
    assert body["input"] == "what's happening today"
    # System prompt gets an init preamble prepended but must still
    # contain the caller's original text
    assert "wb_init" in body["instructions"]
    assert "You are a helper." in body["instructions"]

    # Integrations: one ephemeral_mcp pointing at the gateway, with
    # the resolved whitelist
    assert len(body["integrations"]) == 1
    integ = body["integrations"][0]
    assert integ["type"] == "ephemeral_mcp"
    assert integ["server_label"] == "work-buddy"
    assert integ["server_url"] == "http://localhost:5126/mcp"
    assert "wb_init" in integ["allowed_tools"]
    # Readonly-safe should NOT leak context_bundle (that's readonly_context)
    assert "context_bundle" not in integ["allowed_tools"]
    # Header should contain the synthesized session id
    assert integ["headers"]["X-Work-Buddy-Session"].startswith("lms-")
    assert integ["headers"]["X-Work-Buddy-Session"] == result["session_id"]


def test_llm_with_tools_readonly_context_exposes_broader_menu(
    profile_cfg, monkeypatch, tmp_path,
):
    """readonly_context should expose context collectors + smart search."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "output": "ok", "response_id": "r",
            "model": "qwen/qwen3-4b",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": transport}),
    )
    monkeypatch.setattr(
        "work_buddy.llm.cost._cost_log_path",
        lambda: tmp_path / "llm_costs.jsonl",
    )

    from work_buddy.llm.with_tools import llm_with_tools
    llm_with_tools(
        system="",
        user="q",
        profile="local_general",
        tool_preset="readonly_context",
    )

    allowed = captured["body"]["integrations"][0]["allowed_tools"]
    assert "context_git" in allowed
    assert "context_smart" in allowed
    assert "datacore_run_plan" in allowed


def test_llm_with_tools_cost_log_local_zero(
    profile_cfg, monkeypatch, tmp_path,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "output": "hi", "response_id": "r",
            "model": "qwen/qwen3-4b",
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        })

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": transport}),
    )

    log_file = tmp_path / "llm_costs.jsonl"
    monkeypatch.setattr(
        "work_buddy.llm.cost._cost_log_path", lambda: log_file,
    )

    from work_buddy.llm.with_tools import llm_with_tools
    llm_with_tools(
        system="s", user="u",
        profile="local_general", tool_preset="readonly_safe",
    )

    entry = json.loads(log_file.read_text().splitlines()[-1])
    assert entry["execution_mode"] == "local"
    assert entry["estimated_cost_usd"] == 0.0
    assert entry["backend"] == "lmstudio_local"
    assert entry["task_id"] == "llm_with_tools:readonly_safe"


def test_llm_with_tools_returns_error_on_http_failure(
    profile_cfg, monkeypatch, tmp_path,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": transport}),
    )
    monkeypatch.setattr(
        "work_buddy.llm.cost._cost_log_path",
        lambda: tmp_path / "llm_costs.jsonl",
    )

    from work_buddy.llm.with_tools import llm_with_tools
    result = llm_with_tools(
        system="s", user="u",
        profile="local_general", tool_preset="readonly_safe",
    )
    assert result["error"] is not None
    assert "HTTPStatusError" in result["error"] or "500" in result["error"]
    assert result["content"] == ""
