"""Local LLM call with MCP tool access, gated by a named preset.

Routes a query to a local LM Studio model via ``/api/v1/chat``, which
supports MCP tool-call loops server-side. The model can invoke
work-buddy capabilities whitelisted by the caller-specified
``tool_preset`` — no ad-hoc tool list accepted at call time, so agents
cannot bypass the whitelist.

This is the tool-enabled companion to ``llm_call`` (bounded synchronous
text-only) and ``llm_submit`` (asynchronous background). All three
serve different jobs; this one addresses the "local model needs to
look something up" use case without treating local models as fully
agentic Claude replacements.

## Why a per-call synthesized session id

The work-buddy MCP gateway requires ``wb_init`` before any other tool
call, tied to the MCP connection. LM Studio opens its own MCP
connection to our gateway on behalf of the model, distinct from the
agent's Claude Code connection. We synthesize a fresh session id per
``llm_with_tools`` call and instruct the model (via system prompt) to
call ``wb_init`` with it first. Imperfect — it relies on the model
following the instruction — but isolates failure modes and avoids
gateway surgery. Follow-up: have the gateway auto-register from a
session header so the first-tool-call requirement goes away.
"""

from __future__ import annotations

import uuid
from typing import Any


# Default URL for the work-buddy MCP gateway HTTP transport; matches
# the sidecar config (``sidecar.services.mcp_gateway.port = 5126``).
_DEFAULT_MCP_ENDPOINT = "http://localhost:5126/mcp"


def llm_with_tools(
    *,
    system: str,
    user: str,
    profile: str,
    tool_preset: str,
    previous_response_id: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    store: bool = False,
    mcp_endpoint: str = _DEFAULT_MCP_ENDPOINT,
) -> dict[str, Any]:
    """Invoke a local model with restricted work-buddy MCP tool access.

    Args:
        system: System prompt — this becomes the ``instructions`` field
            on the native chat request. An instruction to call
            ``wb_init`` first is prepended automatically.
        user: User query — sent as ``input``.
        profile: Named local profile (e.g. ``"local_general"``). Must
            resolve to an LM Studio-backed server; the ``/v1`` suffix
            is stripped to build the ``/api/v1/chat`` URL.
        tool_preset: Name of a whitelist defined in
            ``work_buddy/llm/tool_presets.py`` (e.g. ``"readonly_safe"``,
            ``"readonly_context"``). Required; no arbitrary tool list
            accepted at call time.
        previous_response_id: Continue a prior stateful-chat turn.
        max_tokens: Output budget. Default 4096 — tool-using models
            burn budget on reasoning and tool args.
        temperature: Sampling temperature.
        store: Whether LM Studio should retain this turn server-side.
        mcp_endpoint: URL of the work-buddy MCP gateway. Override only
            when testing against a non-standard endpoint.

    Returns:
        ``{content, tool_calls, response_id, model, input_tokens,
        output_tokens, tool_preset, allowed_tools, session_id, error}``.
    """
    if not profile:
        return _error("'profile' is required")
    if not tool_preset:
        return _error(
            "'tool_preset' is required. Presets are defined in "
            "work_buddy/llm/tool_presets.py — currently: "
            "'readonly_safe', 'readonly_context'."
        )

    from work_buddy.llm.profiles import resolve_profile
    from work_buddy.llm.tool_presets import resolve_preset

    try:
        allowed_tools = resolve_preset(tool_preset)
    except KeyError as exc:
        return _error(str(exc))

    try:
        profile_info = resolve_profile(profile)
    except KeyError as exc:
        return _error(str(exc))

    # Strip the ``/v1`` suffix from the openai_compat base_url to build
    # the native-endpoint base. LM Studio serves both from the same host.
    native_base = profile_info["base_url"].rstrip("/")
    if native_base.endswith("/v1"):
        native_base = native_base[:-3]
    native_base = native_base.rstrip("/")

    # Synthesize a one-shot session id for this call's MCP connection.
    session_id = f"lms-{uuid.uuid4().hex[:8]}"

    # Prepend an instruction telling the model to call wb_init first.
    # This is a band-aid for the v1 session-registration constraint
    # (see module docstring); the model must follow it for tool calls
    # to work.
    init_preamble = (
        f"IMPORTANT: Before using any work-buddy tool, first call "
        f"`wb_init` with `session_id=\"{session_id}\"`. This registers "
        f"your MCP session with the gateway. Only after wb_init succeeds "
        f"should you invoke other tools.\n\n"
    )
    effective_system = init_preamble + (system or "")

    integrations = [
        {
            "type": "ephemeral_mcp",
            "server_label": "work-buddy",
            "server_url": mcp_endpoint,
            "allowed_tools": allowed_tools,
            # Pass the synthesized session id as a header too — when
            # the gateway eventually supports header-based auto-init
            # (see module docstring), this enables it transparently.
            "headers": {"X-Work-Buddy-Session": session_id},
        }
    ]

    from work_buddy.llm.backends.lmstudio_native import call_lmstudio_native

    try:
        result = call_lmstudio_native(
            base_url=native_base,
            model=profile_info["model"],
            system=effective_system,
            user=user,
            integrations=integrations,
            previous_response_id=previous_response_id,
            store=store,
            max_tokens=max_tokens,
            temperature=temperature,
            api_key_env=profile_info["api_key_env"],
        )
    except Exception as exc:
        return _error(
            f"{type(exc).__name__}: {exc}",
            model=profile_info["model"],
            tool_preset=tool_preset,
            allowed_tools=allowed_tools,
            session_id=session_id,
        )

    # Log cost (local mode → $0.00, per existing v1 convention)
    from work_buddy.llm.cost import log_call
    log_call(
        model=result.get("model", profile_info["model"]),
        input_tokens=result.get("input_tokens", 0),
        output_tokens=result.get("output_tokens", 0),
        task_id=f"llm_with_tools:{tool_preset}",
        execution_mode="local",
        backend=profile_info["backend_id"],
    )

    return {
        "content": result.get("content", ""),
        "tool_calls": result.get("tool_calls", []),
        "response_id": result.get("response_id"),
        "model": result.get("model", profile_info["model"]),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "tool_preset": tool_preset,
        "allowed_tools": allowed_tools,
        "session_id": session_id,
        "error": None,
    }


def _error(message: str, **extra: Any) -> dict[str, Any]:
    """Build an error response with consistent shape."""
    return {
        "content": "",
        "tool_calls": [],
        "response_id": None,
        "model": extra.get("model", ""),
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_preset": extra.get("tool_preset"),
        "allowed_tools": extra.get("allowed_tools", []),
        "session_id": extra.get("session_id"),
        "error": message,
    }
