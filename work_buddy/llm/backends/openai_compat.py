"""OpenAI-compatible chat-completions backend.

Targets any server exposing the OpenAI ``/v1/chat/completions`` protocol:
LM Studio (including via LM Link for remote model execution), vLLM,
Ollama's OpenAI shim, llama.cpp's server, etc.

Uses ``httpx`` directly rather than the ``openai`` SDK — smaller
dependency surface, no second SDK to pin, and ``httpx`` is pure-Python
(built on h11/anyio, no C extensions) so it's safe in the MCP gateway's
``asyncio.to_thread`` dispatch path. This matches the pure-Python
discipline noted in registry.py for the anthropic backend.
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


def _profile_name(model: str) -> str:
    """Broker profile name for an OpenAI-compat chat call.

    Prefix ``openai_compat:`` distinguishes this from the LM-Studio
    native path and from the embedding path, so per-profile slot limits
    are set independently. Users pointing ``openai_compat`` at LM Studio
    AND using ``lmstudio_native`` get two logical profiles against the
    same physical server — that's fine, the broker is a client-side
    admission control, not a server capacity model.
    """
    return f"openai_compat:{model}"


def call_openai_compat(
    *,
    base_url: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    output_schema: dict | None = None,
    api_key_env: str | None = None,
    timeout: float = 180.0,
    priority: "Priority | None" = None,
    queue_wait_s: float = 30.0,
) -> dict[str, Any]:
    """POST a chat completion to an OpenAI-compatible endpoint.

    Routed through ``LocalInferenceBroker`` so concurrent callers
    respect per-profile slot limits and priority ordering. Without the
    broker, a background classifier call could starve an interactive
    agent response; with it, INTERACTIVE admits ahead of queued
    BACKGROUND work on the same model.

    Args:
        base_url: Server base URL ending in ``/v1`` (e.g.,
            ``http://localhost:1234/v1``).
        model: Model identifier as the server reports it in
            ``GET /v1/models`` (e.g., ``qwen/qwen3-4b``).
        system: System prompt.
        user: User message.
        max_tokens: Max response tokens.
        temperature: Sampling temperature.
        output_schema: Optional JSON Schema dict. When set, wraps the
            request in the ``response_format: json_schema`` envelope
            with ``strict: true`` for reliable conformance.
        api_key_env: Environment variable name to read the bearer token
            from. LM Studio defaults to unauth — pass None or "" when
            no auth is required.
        timeout: HTTP timeout in seconds. Default 180s accounts for
            slower local inference on CPU or partial-offload setups.
        priority: Broker priority class. Defaults to ``WORKFLOW``; bump
            to ``INTERACTIVE`` for user-facing agent loops, drop to
            ``BACKGROUND`` for batch classifier / summarizer work.
        queue_wait_s: Max time to wait for a broker slot before giving
            up (``QueueWaitTimeout``). 30s is a conservative default
            that stays well under the typical end-to-end caller budget.

    Returns:
        ``{content, input_tokens, output_tokens, model}``. ``model``
        echoes the server-reported model id (may differ from input
        when the server normalizes names).

    Raises:
        LocalInferenceError on HTTP failure or malformed response.
        QueueFull / QueueWaitTimeout when the broker can't admit.
    """
    # Deferred import so this module stays cheap to import and the
    # broker's config-load path runs lazily at first use.
    from work_buddy.inference import get_broker, Priority as _Priority

    prio = priority if priority is not None else _Priority.WORKFLOW
    broker = get_broker()

    headers = {"Content-Type": "application/json"}
    if api_key_env:
        token = os.environ.get(api_key_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    if output_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "strict": True,
                "schema": output_schema,
            },
        }

    url = base_url.rstrip("/") + "/chat/completions"

    with broker.slot(
        profile=_profile_name(model),
        priority=prio,
        queue_wait_s=queue_wait_s,
        inference_s=timeout,
    ) as ticket:
        try:
            ticket.mark_started_http()
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise interpret_httpx_exception(
                exc, model=model, endpoint="/v1/chat/completions",
            ) from exc

    try:
        choice = body["choices"][0]
        content = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LocalInferenceError(
            f"Unexpected response shape from {url}: {body!r}",
            kind="malformed_response",
            hint=(
                "LM Studio returned HTTP 200 but the response body did not "
                "match the expected OpenAI chat-completions shape."
            ),
            raw=body,
        ) from exc

    usage = body.get("usage") or {}
    return {
        "content": content,
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "model": body.get("model") or model,
    }
