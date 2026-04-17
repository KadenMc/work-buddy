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
) -> dict[str, Any]:
    """POST a chat completion to an OpenAI-compatible endpoint.

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

    Returns:
        ``{content, input_tokens, output_tokens, model}``. ``model``
        echoes the server-reported model id (may differ from input
        when the server normalizes names).

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

    try:
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
