"""Tests for response-payload hygiene on ``llm_with_tools``.

Raw MCP tool outputs can be huge (observed: 324KB from a single
``sidecar_status`` call). The calling agent delegated to the local
model precisely so it wouldn't have to read raw outputs; embedding
them in the response defeats the feature. These tests pin the
policy:

- Default: tool outputs stripped, only metadata returned.
- persist_tool_results=True: outputs saved as scratch artifacts,
  artifact id embedded.
- Any tool error in the batch: auto-escalate to persist everything
  so the calling agent can audit the failing run.
- Errors always surface an error_preview regardless of persistence.
"""

from __future__ import annotations

import json

import pytest

from work_buddy.llm._tool_call_trim import (
    _detect_error,
    _unwrap_mcp_output,
    trim_tool_calls,
)


# ---------------------------------------------------------------------------
# MCP output unwrapping + error detection
# ---------------------------------------------------------------------------

def test_unwrap_mcp_output_text_list_shape():
    """Actual shape LM Studio emits: JSON string of [{type, text}]."""
    payload = {"result": "all good"}
    wrapped = json.dumps([
        {"type": "text", "text": json.dumps(payload)},
    ])
    assert _unwrap_mcp_output(wrapped) == payload


def test_unwrap_mcp_output_handles_dict_passthrough():
    assert _unwrap_mcp_output({"already": "unwrapped"}) == {"already": "unwrapped"}


def test_unwrap_mcp_output_returns_none_on_garbage():
    assert _unwrap_mcp_output("not json at all") is None
    assert _unwrap_mcp_output(None) is None
    assert _unwrap_mcp_output(42) is None


def test_detect_error_on_error_field():
    wrapped = json.dumps([
        {"type": "text", "text": json.dumps({"error": "access denied"})},
    ])
    assert _detect_error(wrapped) == "access denied"


def test_detect_error_on_success_false():
    wrapped = json.dumps([
        {"type": "text", "text": json.dumps({"success": False, "message": "try again"})},
    ])
    assert _detect_error(wrapped) == "try again"


def test_detect_error_none_for_successful_calls():
    wrapped = json.dumps([
        {"type": "text", "text": json.dumps({"result": {"ok": True}})},
    ])
    assert _detect_error(wrapped) is None


def test_detect_error_none_for_opaque_output():
    assert _detect_error("some raw string response") is None


# ---------------------------------------------------------------------------
# trim_tool_calls — default path
# ---------------------------------------------------------------------------

def _make_success_call(tool="wb_run", capability="sidecar_status", payload=None) -> dict:
    """Build a realistic tool_call entry as LM Studio emits it."""
    payload = payload if payload is not None else {"result": "ok", "big": "x" * 5000}
    wrapped = json.dumps([
        {"type": "text", "text": json.dumps(payload)},
    ])
    return {
        "type": "tool_call",
        "tool": tool,
        "arguments": {"capability": capability, "params": {}},
        "output": wrapped,
        "provider_info": {"server_label": "work-buddy", "type": "ephemeral_mcp"},
    }


def _make_error_call(tool="wb_run", capability="task_toggle", error_msg="denied") -> dict:
    payload = {"error": error_msg}
    wrapped = json.dumps([
        {"type": "text", "text": json.dumps(payload)},
    ])
    return {
        "type": "tool_call",
        "tool": tool,
        "arguments": {"capability": capability, "params": {}},
        "output": wrapped,
        "provider_info": {"server_label": "work-buddy", "type": "ephemeral_mcp"},
    }


def test_default_strips_outputs(monkeypatch):
    """No errors, persist_tool_results=False → outputs stripped."""
    # Prevent accidental artifact writes
    monkeypatch.setattr(
        "work_buddy.artifacts.save",
        _must_not_be_called, raising=False,
    )
    calls = [_make_success_call(), _make_success_call(capability="feature_status")]
    out = trim_tool_calls(
        calls,
        persist_tool_results=False,
        session_id="lms-test",
        tool_preset="readonly_safe",
    )
    assert len(out) == 2
    for entry in out:
        assert entry["status"] == "ok"
        assert entry.get("output_omitted") is True
        assert "output_artifact_id" not in entry
        # Metadata preserved
        assert entry["tool"] == "wb_run"
        assert "arguments" in entry
        assert entry["output_size_chars"] > 0
        # No leaked raw output
        assert "output" not in entry


def test_default_preserves_provider_info_and_type():
    calls = [_make_success_call()]
    out = trim_tool_calls(
        calls, persist_tool_results=False,
        session_id="lms-x", tool_preset="readonly_safe",
    )
    assert out[0]["provider_info"]["server_label"] == "work-buddy"
    assert out[0]["type"] == "tool_call"


# ---------------------------------------------------------------------------
# trim_tool_calls — explicit persist
# ---------------------------------------------------------------------------

def test_persist_true_saves_each_call(monkeypatch):
    saved: list[dict] = []

    class _StubRec:
        def __init__(self, id_):
            self.id = id_

    def spy_save(content, type, slug, ext="json", *, tags=None, description="", session_id=None, **kw):
        saved.append({
            "content_len": len(content), "type": type, "slug": slug,
            "tags": tags or [], "session_id": session_id,
        })
        return _StubRec(f"artifact_{len(saved)}")

    monkeypatch.setattr("work_buddy.artifacts.save", spy_save)

    calls = [_make_success_call(), _make_success_call(capability="feature_status")]
    out = trim_tool_calls(
        calls, persist_tool_results=True,
        session_id="lms-persist", tool_preset="readonly_safe",
    )

    assert len(saved) == 2
    # scratch type (3-day TTL)
    assert all(s["type"] == "scratch" for s in saved)
    # Session threaded through for attribution
    assert all(s["session_id"] == "lms-persist" for s in saved)
    # Tagged for later discovery
    for s in saved:
        assert "llm_with_tools" in s["tags"]
        assert "preset:readonly_safe" in s["tags"]
    # Artifact ids surfaced in response
    assert out[0]["output_artifact_id"] == "artifact_1"
    assert out[1]["output_artifact_id"] == "artifact_2"


def test_persist_gracefully_handles_save_failure(monkeypatch):
    """Artifact-store hiccups shouldn't take down the whole tool run."""
    def failing_save(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr("work_buddy.artifacts.save", failing_save)

    out = trim_tool_calls(
        [_make_success_call()],
        persist_tool_results=True,
        session_id="lms-x", tool_preset="readonly_safe",
    )
    assert out[0]["persist_failed"] is True
    assert out[0]["output_omitted"] is True


# ---------------------------------------------------------------------------
# Error auto-escalation
# ---------------------------------------------------------------------------

def test_error_in_batch_auto_persists_all(monkeypatch):
    """Even with persist=False, ANY error → persist all outputs."""
    saved: list[dict] = []

    class _StubRec:
        def __init__(self, id_):
            self.id = id_

    def spy_save(content, *a, **k):
        saved.append(content)
        return _StubRec(f"artifact_{len(saved)}")

    monkeypatch.setattr("work_buddy.artifacts.save", spy_save)

    calls = [
        _make_success_call(capability="sidecar_status"),
        _make_error_call(capability="task_toggle", error_msg="ACL denied"),
        _make_success_call(capability="feature_status"),
    ]
    out = trim_tool_calls(
        calls,
        persist_tool_results=False,  # explicit False — but error forces escalation
        session_id="lms-err",
        tool_preset="readonly_safe",
    )

    # All three persisted despite the default-False flag
    assert len(saved) == 3
    # Status correctly reflects per-call error state
    assert out[0]["status"] == "ok"
    assert out[1]["status"] == "error"
    assert out[2]["status"] == "ok"
    # Every entry got an artifact id since we auto-escalated
    assert all("output_artifact_id" in e for e in out)


def test_error_preview_included_and_capped(monkeypatch):
    """error_preview surfaces even when outputs are stripped, capped at 500 chars."""
    monkeypatch.setattr(
        "work_buddy.artifacts.save",
        lambda *a, **k: _Stub("artifact_1"),
    )

    long_error = "x" * 2000
    calls = [_make_error_call(error_msg=long_error)]
    out = trim_tool_calls(
        calls, persist_tool_results=False,
        session_id="lms-x", tool_preset="readonly_safe",
    )
    entry = out[0]
    assert entry["status"] == "error"
    assert "error_preview" in entry
    # Capped at ~500 chars + ellipsis marker
    assert len(entry["error_preview"]) <= 501
    assert entry["error_preview"].endswith("…")


def test_successful_calls_have_no_error_preview(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.artifacts.save",
        _must_not_be_called, raising=False,
    )
    calls = [_make_success_call()]
    out = trim_tool_calls(
        calls, persist_tool_results=False,
        session_id="lms-x", tool_preset="readonly_safe",
    )
    assert "error_preview" not in out[0]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_tool_calls_returns_empty():
    assert trim_tool_calls([], persist_tool_results=False,
                           session_id="x", tool_preset="y") == []


def test_non_dict_entries_are_skipped(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.artifacts.save",
        _must_not_be_called, raising=False,
    )
    out = trim_tool_calls(
        [_make_success_call(), "not a dict", None, 42],
        persist_tool_results=False,
        session_id="x", tool_preset="y",
    )
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Stub:
    def __init__(self, id_):
        self.id = id_


def _must_not_be_called(*a, **k):
    raise AssertionError(
        "artifact save should not have been called in a non-persist, "
        "no-error path"
    )
