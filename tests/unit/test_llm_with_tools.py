"""Tests for llm_with_tools and the tool-preset security layer.

Covers:
- Tool presets are structurally valid (readonly has no mutations; wb_init excluded)
- Every tool name in every preset is a real registered capability
- resolve_preset rejects unknown names with a helpful error
- llm_with_tools rejects missing profile / tool_preset
- llm_with_tools sends the expected payload to /api/v1/chat
- Cost log records execution_mode=local, $0.00 for tool-enabled calls
- wb_init is NOT in any preset (ACL-escape prevention)
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Tool presets — structural + registry-alignment
# ---------------------------------------------------------------------------

def test_no_preset_includes_wb_init():
    """Security: wb_init must never be in a preset.

    Allowing wb_init inside an ACL-scoped session is an ACL-escape vector
    — the model can call wb_run(capability='wb_init', session_id='...')
    to swap its MCP connection's bound session id and drop the ACL.
    Header-based auto-init covers session registration for local models
    without exposing the primitive.
    """
    from work_buddy.llm.tool_presets import PRESETS
    for name, tools in PRESETS.items():
        assert "wb_init" not in tools, (
            f"Preset {name!r} contains wb_init — this is an ACL-escape "
            f"vector. Remove it; header auto-init handles registration."
        )


def test_readonly_presets_have_no_mutating_capabilities():
    from work_buddy.llm.tool_presets import validate_presets
    problems = validate_presets()
    assert problems == [], f"Preset validation problems: {problems}"


def test_all_preset_names_exist_in_capability_registry():
    """Guards against typos and drift when capabilities are renamed.

    Counts both enabled and *disabled* capabilities as known — a
    capability temporarily unavailable (e.g., Obsidian plugin not
    reachable during a test run) is still a real capability; the
    preset reference isn't stale drift.
    """
    from work_buddy.llm.tool_presets import validate_presets
    from work_buddy.mcp_server.registry import get_registry
    from work_buddy.tools import DISABLED_CAPABILITIES

    reg = get_registry()
    assert isinstance(reg, dict), (
        f"Registry shape changed unexpectedly — got {type(reg)}"
    )
    registry_names = set(reg.keys()) | set(DISABLED_CAPABILITIES.keys())

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
    # readonly_safe should contain the core read capabilities but
    # NOT wb_init (see security note in tool_presets.py).
    assert "task_briefing" in tools
    assert "wb_init" not in tools


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
        # Shape matches LM Studio's actual /api/v1/chat response
        return httpx.Response(200, json={
            "model_instance_id": "qwen/qwen3-4b",
            "response_id": "resp_abc",
            "output": [
                {"type": "message", "content": "final answer"},
            ],
            "stats": {
                "input_tokens": 10,
                "total_output_tokens": 20,
                "reasoning_output_tokens": 0,
            },
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
    # LM Studio's /api/v1/chat has no separate system field; we
    # prepend system text into ``input`` along with the user turn.
    assert "what's happening today" in body["input"]
    assert "You are a helper." in body["input"]    # caller's system text
    # ``instructions`` is not a supported top-level field on this
    # endpoint — verify we're not sending it
    assert "instructions" not in body
    # Output-token budget uses the LM Studio field name, not OpenAI's
    assert "max_output_tokens" in body
    assert "max_tokens" not in body

    # Integrations: one ephemeral_mcp pointing at the gateway.
    # allowed_tools on LM Studio's side is JUST the top-level MCP
    # tools it needs to dispatch (wb_run, wb_search). Domain
    # capabilities are gated server-side via session_acl.
    assert len(body["integrations"]) == 1
    integ = body["integrations"][0]
    assert integ["type"] == "ephemeral_mcp"
    assert integ["server_label"] == "work-buddy"
    assert integ["server_url"] == "http://localhost:5126/mcp"
    assert integ["allowed_tools"] == ["wb_run", "wb_search"]
    # Header carries the synthesized session id for gateway auto-init
    assert integ["headers"]["X-Work-Buddy-Session"].startswith("lms-")
    assert integ["headers"]["X-Work-Buddy-Session"] == result["session_id"]


def test_llm_with_tools_registers_session_acl_with_preset_capabilities(
    profile_cfg, monkeypatch, tmp_path,
):
    """The ACL set on the session should contain the preset's allowed
    capabilities (not the top-level MCP tools LM Studio sees)."""
    captured_acl: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # Capture the ACL at the moment the backend is hit — this is
        # when the model would be making tool calls to the gateway.
        from work_buddy.mcp_server.session_acl import get_session_acl
        # The session_id is embedded in the header the backend sends
        session_id = request.headers.get("X-Work-Buddy-Session")
        # But for this test we observe via the caller's return — for
        # now just verify the backend got called
        captured_acl["called"] = True
        return httpx.Response(200, json={
            "model_instance_id": "qwen/qwen3-4b",
            "response_id": "r",
            "output": [{"type": "message", "content": "ok"}],
            "stats": {"input_tokens": 1, "total_output_tokens": 1},
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

    # Patch set_session_acl to capture what gets registered. The import
    # in with_tools.py is function-local, so we patch it at the source
    # module; the function-local import picks up the patched version.
    from work_buddy.mcp_server import session_acl as acl_mod
    original_set = acl_mod.set_session_acl

    captured_acl_calls: list = []

    def spy_set(session_id, allowed):
        captured_acl_calls.append({"session_id": session_id, "allowed": set(allowed)})
        return original_set(session_id, allowed)

    monkeypatch.setattr(acl_mod, "set_session_acl", spy_set)

    from work_buddy.llm.with_tools import llm_with_tools
    result = llm_with_tools(
        system="s", user="u",
        profile="local_general", tool_preset="readonly_safe",
    )

    assert result["error"] is None
    # ACL was registered exactly once, for this session, with the preset
    assert len(captured_acl_calls) == 1
    call = captured_acl_calls[0]
    assert call["session_id"] == result["session_id"]
    # The ACL should include readonly_safe's task-read capabilities
    assert "task_briefing" in call["allowed"]
    assert "sidecar_status" in call["allowed"]
    # wb_init is deliberately EXCLUDED (ACL-escape prevention)
    assert "wb_init" not in call["allowed"]
    # And should be cleared after the call — no lingering ACL
    assert acl_mod.get_session_acl(result["session_id"]) is None


def test_llm_with_tools_clears_acl_even_on_failure(
    profile_cfg, monkeypatch, tmp_path,
):
    """If the backend raises, the ACL must still get cleared in finally."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "kaboom"})

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

    from work_buddy.mcp_server import session_acl as acl_mod
    from work_buddy.llm.with_tools import llm_with_tools

    result = llm_with_tools(
        system="s", user="u",
        profile="local_general", tool_preset="readonly_safe",
    )

    # The call failed, but the ACL shouldn't be sticky
    assert result["error"] is not None
    assert acl_mod.get_session_acl(result["session_id"]) is None


def test_llm_with_tools_readonly_context_exposes_broader_menu(
    profile_cfg, monkeypatch, tmp_path,
):
    """readonly_context should expose context collectors + smart search."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "model_instance_id": "qwen/qwen3-4b",
            "response_id": "r",
            "output": [{"type": "message", "content": "ok"}],
            "stats": {"input_tokens": 1, "total_output_tokens": 1},
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
    result = llm_with_tools(
        system="",
        user="q",
        profile="local_general",
        tool_preset="readonly_context",
    )

    # The integrations.allowed_tools field advertises only the top-level
    # MCP tools — context capabilities are NOT there directly.
    advertised = captured["body"]["integrations"][0]["allowed_tools"]
    assert advertised == ["wb_run", "wb_search"]
    # The actual capability whitelist is returned in the result and
    # reflects the broader readonly_context preset.
    assert "context_git" in result["allowed_tools"]
    assert "context_smart" in result["allowed_tools"]
    assert "datacore_run_plan" in result["allowed_tools"]


def test_llm_with_tools_cost_log_local_zero(
    profile_cfg, monkeypatch, tmp_path,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model_instance_id": "qwen/qwen3-4b",
            "response_id": "r",
            "output": [{"type": "message", "content": "hi"}],
            "stats": {"input_tokens": 5, "total_output_tokens": 10},
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
