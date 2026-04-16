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

    payload: dict[str, Any] = {
        "model": model,
        "input": user,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "store": store,
    }
    if system:
        payload["instructions"] = system
    if integrations:
        payload["integrations"] = integrations
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id

    url = base_url.rstrip("/") + "/api/v1/chat"

    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

    # The native /api/v1/chat response shape isn't fully pinned in
    # public docs — extract defensively and fall back to plausible
    # alternatives if top-level keys aren't there.
    content = _extract_content(body)
    tool_calls = _extract_tool_calls(body)
    usage = body.get("usage") or {}

    return {
        "content": content,
        "tool_calls": tool_calls,
        "response_id": body.get("response_id") or body.get("id"),
        "model": body.get("model") or model,
        "input_tokens": int(usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0),
        "output_tokens": int(
            usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0,
        ),
        "raw": body,
    }


def _extract_content(body: dict[str, Any]) -> str:
    """Extract the model's final text content from a native-chat response.

    Handles a few plausible response shapes since the public docs are
    sparse. Preference order: top-level ``output``, OpenAI-style
    ``choices[0].message.content``, first text block in an ``output``
    list of content-items.
    """
    if isinstance(body.get("output"), str):
        return body["output"]

    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            c = msg.get("content")
            if isinstance(c, str):
                return c

    # /v1/responses-style: output is an array of content items
    out = body.get("output")
    if isinstance(out, list):
        for item in out:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                content = item.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") in (
                            "output_text", "text",
                        ):
                            return block.get("text", "") or ""
                elif isinstance(content, str):
                    return content

    return ""


def _extract_tool_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract any MCP tool calls the model invoked.

    Each returned dict preserves ``tool``, ``arguments``, ``output``,
    and any ``provider_info`` the server included. Returns an empty
    list when no tools were called.
    """
    calls: list[dict[str, Any]] = []

    # Pattern 1: top-level "tool_calls" array
    tc = body.get("tool_calls")
    if isinstance(tc, list):
        calls.extend(x for x in tc if isinstance(x, dict))

    # Pattern 2: output items list with type="tool_call"
    out = body.get("output")
    if isinstance(out, list):
        for item in out:
            if isinstance(item, dict) and item.get("type") in (
                "tool_call", "mcp_call",
            ):
                calls.append(item)

    return calls
