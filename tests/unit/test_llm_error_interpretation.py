"""Tests for structured interpretation of local-inference failures.

Ensures that the raw httpx exceptions bubbling out of LM Studio get
mapped to actionable LocalInferenceError messages with the right
``kind`` and ``hint`` so agent-facing responses explain *why* a call
failed, not just that it did.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest

from work_buddy.llm.backends._errors import (
    LocalInferenceError,
    interpret_httpx_exception,
)


def _status_error(status: int, body: dict | str | None) -> httpx.HTTPStatusError:
    """Build a realistic HTTPStatusError for a given status + body."""
    request = httpx.Request("POST", "http://localhost:1234/api/v1/chat")
    if isinstance(body, dict):
        content = json.dumps(body).encode("utf-8")
        headers = {"content-type": "application/json"}
    elif isinstance(body, str):
        content = body.encode("utf-8")
        headers = {"content-type": "text/plain"}
    else:
        content = b""
        headers = {}
    response = httpx.Response(status, headers=headers, content=content, request=request)
    return httpx.HTTPStatusError("http error", request=request, response=response)


# ---------------------------------------------------------------------------
# Connection-level failures
# ---------------------------------------------------------------------------

def test_connect_error_maps_to_server_unreachable():
    exc = httpx.ConnectError("Connection refused")
    err = interpret_httpx_exception(exc, model="m", endpoint="/api/v1/chat")
    assert err.kind == "server_unreachable"
    assert "LM Studio" in str(err) or "not reachable" in str(err)
    assert "start" in err.hint.lower() or "server" in err.hint.lower()


def test_read_timeout_maps_to_timeout():
    exc = httpx.ReadTimeout("timed out")
    err = interpret_httpx_exception(exc, model="m", endpoint="/api/v1/chat")
    assert err.kind == "timeout"
    assert "timed out" in str(err).lower()


# ---------------------------------------------------------------------------
# Model-not-available family (LM Studio's actual error shapes)
# ---------------------------------------------------------------------------
# Renamed from "model_not_loaded": the error doesn't actually mean "the
# model is downloaded but not in memory" — that case JIT auto-loads
# successfully. This kind covers the broader "we can't reach this model
# right now" state: not downloaded, no linked device surfaces it, or
# LM Link dropped.

def test_invalid_model_identifier_maps_to_model_not_available():
    # Actual observed shape from /api/v1/chat when LM Link is down
    body = {
        "error": {
            "message": "Invalid model identifier \"qwen/qwen3.5-9b\". There are no downloaded llm models. Please download a model to get started.",
            "type": "invalid_request",
            "param": "model",
            "code": "model_not_found",
        }
    }
    exc = _status_error(404, body)
    err = interpret_httpx_exception(exc, model="qwen/qwen3.5-9b", endpoint="/api/v1/chat")
    assert err.kind == "model_not_available"
    assert "qwen/qwen3.5-9b" in str(err)
    assert "LM Link" in err.hint


def test_no_models_loaded_maps_to_model_not_available():
    # Actual observed shape from /v1/chat/completions when nothing's loaded
    body = {
        "error": {
            "message": "No models loaded. Please load a model in the developer page or use the 'lms load' command.",
            "type": "invalid_request_error",
            "param": "model",
            "code": None,
        }
    }
    exc = _status_error(400, body)
    err = interpret_httpx_exception(exc, model="qwen/qwen3.5-9b", endpoint="/v1/chat/completions")
    assert err.kind == "model_not_available"
    assert "LM Link" in err.hint


def test_model_not_found_code_alone_triggers_classification():
    # Sometimes the message is generic but the code is definitive
    body = {"error": {"message": "whatever", "code": "model_not_found"}}
    exc = _status_error(404, body)
    err = interpret_httpx_exception(exc, model="foo", endpoint="/api/v1/chat")
    assert err.kind == "model_not_available"


# ---------------------------------------------------------------------------
# Generic classification
# ---------------------------------------------------------------------------

def test_400_without_recognized_shape_is_bad_request():
    body = {"error": {"message": "something malformed"}}
    exc = _status_error(400, body)
    err = interpret_httpx_exception(exc, model="m", endpoint="/api/v1/chat")
    assert err.kind == "bad_request"
    assert "400" in str(err)
    assert "something malformed" in str(err)


def test_500_is_server_error():
    body = {"error": {"message": "internal explosion"}}
    exc = _status_error(500, body)
    err = interpret_httpx_exception(exc, model="m", endpoint="/v1/chat/completions")
    assert err.kind == "server_error"


def test_non_json_body_still_produces_reasonable_error():
    exc = _status_error(502, "<html>Bad Gateway</html>")
    err = interpret_httpx_exception(exc, model="m", endpoint="/api/v1/chat")
    assert err.kind == "server_error"
    assert "502" in str(err)


# ---------------------------------------------------------------------------
# Context-window overflow (multiple LM Studio phrasings)
# ---------------------------------------------------------------------------

def test_context_size_exceeded_classic_phrasing():
    body = {"error": "Context size has been exceeded."}
    exc = _status_error(500, body)
    err = interpret_httpx_exception(exc, model="m", endpoint="/v1/chat/completions")
    assert err.kind == "context_exceeded"


def test_n_keep_n_ctx_phrasing_2026_lm_studio():
    """LM Studio's newer phrasing — used to fall through to bad_request /
    schema_violation downstream because no 'exceed' / 'too long' word
    appears, even though the structured tokens are unambiguous."""
    body = {
        "error": (
            "The number of tokens to keep from the initial prompt is "
            "greater than the context length (n_keep: 4103 >= n_ctx: 4096). "
            "Try to load the model with a larger context length, or "
            "provide a shorter input."
        ),
    }
    exc = _status_error(400, body)
    err = interpret_httpx_exception(exc, model="m", endpoint="/v1/chat/completions")
    assert err.kind == "context_exceeded"


def test_context_exceeded_ignores_schema_hint_in_message():
    """An LM Studio overflow error sometimes ships with a fallback hint
    mentioning the API schema. The detector must not get distracted by
    the word 'schema' and reclassify."""
    body = {
        "error": (
            "n_keep: 4103 >= n_ctx: 4096. Hint: shape mismatch — the "
            "native-endpoint schema may have changed."
        ),
    }
    exc = _status_error(400, body)
    err = interpret_httpx_exception(exc, model="m", endpoint="/v1/chat/completions")
    assert err.kind == "context_exceeded"


def test_prompt_too_long_phrasing():
    body = {"error": "Prompt is too long for this model's context"}
    exc = _status_error(500, body)
    err = interpret_httpx_exception(exc, model="m", endpoint="/v1/chat/completions")
    assert err.kind == "context_exceeded"


def test_context_exceeded_carries_actionable_hint():
    """The hint should mention the three-lever fix ladder."""
    body = {"error": "Context size has been exceeded."}
    exc = _status_error(500, body)
    err = interpret_httpx_exception(exc, model="m", endpoint="/v1/chat/completions")
    assert err.kind == "context_exceeded"
    hint = err.hint.lower()
    assert "context length" in hint or "context window" in hint
    # At least one of the three levers should be mentioned.
    assert (
        "increase" in hint
        or "narrow" in hint
        or "shorten" in hint
    )


# ---------------------------------------------------------------------------
# Unsupported model family
# ---------------------------------------------------------------------------

def test_unsupported_model_for_endpoint():
    body = {
        "error": {
            "message": "Model 'text-embedding-nomic-embed-text-v1.5' is not supported for chat completions."
        }
    }
    exc = _status_error(400, body)
    err = interpret_httpx_exception(exc, model="text-embedding-nomic-embed-text-v1.5", endpoint="/api/v1/chat")
    assert err.kind == "model_unsupported"


# ---------------------------------------------------------------------------
# Serialization to dict
# ---------------------------------------------------------------------------

def test_to_dict_shape():
    err = LocalInferenceError(
        "oops", kind="model_not_available", hint="do the thing",
    )
    d = err.to_dict(model="m")
    assert d == {
        "error": "oops",
        "error_kind": "model_not_available",
        "hint": "do the thing",
        "model": "m",
    }


# ---------------------------------------------------------------------------
# Integration: llm_with_tools surfaces hint + error_kind
# ---------------------------------------------------------------------------

def test_with_tools_surfaces_model_not_available_hint(monkeypatch, tmp_path):
    """End-to-end check that a model-not-available failure propagates
    structured error info through llm_with_tools's response."""
    monkeypatch.setattr(
        "work_buddy.llm.profiles.load_config",
        lambda: {
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
                        "model": "qwen/qwen3.5-9b",
                        "max_output_tokens": 2048,
                        "execution_mode": "local",
                    },
                },
            }
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={
            "error": {
                "message": "Invalid model identifier \"qwen/qwen3.5-9b\". There are no downloaded llm models.",
                "code": "model_not_found",
            }
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
        system="", user="hi",
        profile="local_general", tool_preset="readonly_safe",
    )

    assert result["error_kind"] == "model_not_available"
    assert "LM Link" in result["hint"]
    assert result["content"] == ""
    # Agent should still see which model + preset it was trying to use
    assert result["model"] == "qwen/qwen3.5-9b"
    assert result["tool_preset"] == "readonly_safe"


# ---------------------------------------------------------------------------
# MCP-integrations failures — 500 from /api/v1/chat with telling body text
# ---------------------------------------------------------------------------
# These are the failures a local model produces when it uses LM Studio's
# `integrations` tool-loop and that loop talks to the work-buddy gateway.
# The raw error bodies are inscrutable ("-32001", "fetch failed",
# "peer_keepalive_timeout") unless you know what they mean — the
# interpreter should surface the real cause + first diagnostic step.


def test_mcp_gateway_timeout_dash_32001_is_classified() -> None:
    """JSON-RPC -32001 from the MCP round-trip means the gateway was
    too slow. Must be distinct from a generic 500."""
    body = {"error": {"message": "MCP error -32001: Request timed out"}}
    err = interpret_httpx_exception(
        _status_error(500, body),
        model="qwen/qwen2.5-coder-14b",
        endpoint="/api/v1/chat",
    )
    assert err.kind == "mcp_gateway_timeout"
    assert "-32001" in str(err)
    # Hint must point at a concrete first diagnostic
    assert "localhost:5126" in err.hint
    assert "health" in err.hint.lower()


def test_mcp_fetch_failed_is_distinct_from_timeout() -> None:
    """`fetch failed` is a transport-layer failure, not a JSON-RPC
    timeout — the interpreter should classify it separately so the
    caller can try the right remedy (retry vs debug gateway)."""
    body = {"error": {"message": "fetch failed"}}
    err = interpret_httpx_exception(
        _status_error(500, body),
        model="qwen/qwen2.5-coder-14b",
        endpoint="/api/v1/chat",
    )
    assert err.kind == "mcp_fetch_failed"
    assert "transport" in str(err).lower() or "tcp" in str(err).lower()
    assert "retry" in err.hint.lower()


def test_lm_link_peer_keepalive_timeout_is_classified() -> None:
    """LM Link dropping mid-call is a distinct failure mode from a
    gateway timeout — the remedy is restart LM Link, not debug the
    gateway."""
    body = {
        "error": {
            "message": (
                "LM Link connection entered error state "
                "peer_keepalive_timeout"
            ),
        },
    }
    err = interpret_httpx_exception(
        _status_error(500, body),
        model="qwen/qwen2.5-coder-14b",
        endpoint="/api/v1/chat",
    )
    assert err.kind == "lm_link_dropped"
    assert "LM Link" in str(err)
    # Hint should point at the compute device, not the gateway
    assert "compute device" in err.hint.lower() or "link" in err.hint.lower()


def test_generic_500_still_falls_through_to_server_error() -> None:
    """If the body doesn't match any of the three integration-specific
    patterns, it stays classified as a plain server_error."""
    body = {"error": {"message": "Something entirely unrelated exploded"}}
    err = interpret_httpx_exception(
        _status_error(500, body),
        model="any-model",
        endpoint="/api/v1/chat",
    )
    assert err.kind == "server_error"
