"""LM Studio native /api/v1/chat backend.

Targets LM Studio's feature-richer native endpoint, which supports MCP
tool-call loops server-side. Given an ``integrations`` array the model
can invoke work-buddy MCP tools (restricted by a per-request
``allowed_tools`` whitelist) and LM Studio threads the tool results
back to the model automatically — no client-side round-trips.

This is separate from ``openai_compat.py`` because the request shape
differs: ``/api/v1/chat`` takes a single ``input`` string plus
optional ``instructions`` (system prompt) rather than OpenAI's
``messages`` array, uses ``previous_response_id`` for multi-turn
continuity, and returns ``tool_call`` objects inline with the output.

Uses ``httpx`` directly (pure Python; safe in the MCP gateway's
``asyncio.to_thread`` dispatch path).
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from work_buddy.llm.backends._errors import (
    LocalInferenceError,
    interpret_httpx_exception,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def call_lmstudio_native(
    *,
    base_url: str,
    model: str,
    system: str,
    user: str,
    integrations: list[dict[str, Any]] | None = None,
    previous_response_id: str | None = None,
    store: bool = False,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    api_key_env: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """POST to LM Studio's native ``/api/v1/chat`` endpoint.

    Args:
        base_url: LM Studio base URL WITHOUT ``/v1`` suffix
            (e.g. ``http://localhost:1234``). The native API path
            ``/api/v1/chat`` is appended.
        model: Model identifier (``id`` from GET /v1/models).
        system: System prompt. Sent as ``instructions`` (LM Studio
            follows the OpenAI ``/v1/responses`` convention).
        user: User message — sent as ``input``.
        integrations: Optional list of MCP integration configs; each
            entry looks like::

                {
                  "type": "ephemeral_mcp",
                  "server_label": "work-buddy",
                  "server_url": "http://localhost:5126/mcp",
                  "allowed_tools": ["list", "of", "whitelisted", "names"],
                  "headers": {"Authorization": "Bearer ..."},
                }

            Pre-configured servers can be referenced by id string
            directly, e.g. ``"mcp/playwright"``.
        previous_response_id: When continuing a stateful chat, pass
            the ``response_id`` from the prior response.
        store: Whether LM Studio should retain this turn server-side
            for future ``previous_response_id`` references. Default
            False — stateless one-shot call.
        max_tokens: Max output tokens. Default 4096 (tool-calling
            models eat token budget on reasoning + tool args).
        temperature: Sampling temperature.
        api_key_env: Env var holding a bearer token if LM Studio auth
            is enabled. LM Studio default is unauth — pass None.
        timeout: HTTP timeout. Default 300s accommodates tool-call
            loops with multiple MCP round-trips.

    Returns:
        ``{content, tool_calls, response_id, model, input_tokens,
        output_tokens}``. ``tool_calls`` is the list the model made
        (for audit / observability); ``content`` is the final text
        answer after tool results fed back.

    Raises:
        httpx.HTTPError on connection failure or non-2xx.
        ValueError on malformed response.
    """
    headers = {"Content-Type": "application/json"}
    if api_key_env:
        token = os.environ.get(api_key_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    # LM Studio's /api/v1/chat accepts ``input`` as the user turn but
    # does NOT expose a separate system-prompt field. Prepend the
    # system text to the input so the model's chat template picks it
    # up as the leading context. Observed-valid fields (from probing
    # the live endpoint): model, input, integrations, previous_response_id,
    # store, temperature, max_output_tokens, context_length. The OpenAI
    # field name ``max_tokens`` is rejected.
    combined_input = user
    if system:
        combined_input = f"{system}\n\n---\n\n{user}"

    payload: dict[str, Any] = {
        "model": model,
        "input": combined_input,
        "max_output_tokens": max_tokens,
        "temperature": temperature,
        "store": store,
    }
    if integrations:
        payload["integrations"] = integrations
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id

    url = base_url.rstrip("/") + "/api/v1/chat"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise interpret_httpx_exception(
            exc, model=model, endpoint="/api/v1/chat",
        ) from exc

    # Observed response shape from LM Studio /api/v1/chat:
    #   {
    #     "model_instance_id": "...",
    #     "output": [
    #       {"type": "reasoning", "content": "..."},   # thinking models
    #       {"type": "message", "content": "final answer"},
    #       {"type": "mcp_call", ...},                  # tool invocations
    #     ],
    #     "stats": {"input_tokens": N, "total_output_tokens": N,
    #               "reasoning_output_tokens": N, ...},
    #     "response_id": "resp_..."
    #   }
    content = _extract_content(body)
    tool_calls = _extract_tool_calls(body)
    reasoning = _extract_reasoning(body)
    stats = body.get("stats") or {}

    return {
        "content": content,
        "reasoning": reasoning,
        "tool_calls": tool_calls,
        "response_id": body.get("response_id") or body.get("id"),
        "model": body.get("model_instance_id") or body.get("model") or model,
        "input_tokens": int(stats.get("input_tokens", 0) or 0),
        "output_tokens": int(stats.get("total_output_tokens", 0) or 0),
        "reasoning_tokens": int(stats.get("reasoning_output_tokens", 0) or 0),
        "raw": body,
    }


def _extract_content(body: dict[str, Any]) -> str:
    """Extract the model's final text answer from a native-chat response.

    LM Studio's /api/v1/chat returns ``output`` as an ordered array of
    typed blocks — ``reasoning`` (thinking), ``message`` (user-facing
    answer), ``mcp_call`` (tool invocations). We want the LAST
    ``message`` block's content; earlier message blocks may be
    intermediate reasoning-visible text.
    """
    out = body.get("output")
    if isinstance(out, str):  # legacy/simple shape fallback
        return out

    if isinstance(out, list):
        # Walk backwards to find the last message block — this is the
        # post-tool-call final answer when tools were invoked.
        for item in reversed(out):
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Some shapes nest content items inside a message block
                for block in content:
                    if isinstance(block, dict) and block.get("type") in (
                        "output_text", "text",
                    ):
                        return block.get("text", "") or ""
        # No message block at all — fall through

    # OpenAI-compat fallback (shouldn't happen for /api/v1/chat but
    # costs nothing to tolerate)
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            c = msg.get("content")
            if isinstance(c, str):
                return c

    return ""


def _extract_reasoning(body: dict[str, Any]) -> str:
    """Extract the model's reasoning/thinking text when present.

    Thinking-enabled models (Qwen3.5, etc.) emit their internal chain
    of thought as a separate ``reasoning`` block before the final
    message. Surfacing it (separately from ``content``) lets agents
    decide whether to use it for audit or discard it.
    """
    out = body.get("output")
    if isinstance(out, list):
        chunks: list[str] = []
        for item in out:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                c = item.get("content")
                if isinstance(c, str):
                    chunks.append(c)
        return "\n\n".join(chunks)
    return ""


def _extract_tool_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract any MCP tool calls the model invoked.

    LM Studio surfaces tool invocations as ``output`` items with
    ``type: "mcp_call"`` (or ``"tool_call"`` in older versions).
    Each entry preserves the call's tool name, arguments, output,
    and provider_info. Returns an empty list when no tools fired.
    """
    calls: list[dict[str, Any]] = []

    out = body.get("output")
    if isinstance(out, list):
        for item in out:
            if isinstance(item, dict) and item.get("type") in (
                "tool_call", "mcp_call",
            ):
                calls.append(item)

    # Belt-and-suspenders: some response variants bubble tool_calls
    # as a sibling key — include them too
    tc = body.get("tool_calls")
    if isinstance(tc, list):
        calls.extend(x for x in tc if isinstance(x, dict))

    return calls
