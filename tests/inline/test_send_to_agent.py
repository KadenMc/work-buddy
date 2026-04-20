"""Tests for the ``send-to-agent`` inline command handler."""

from __future__ import annotations

import asyncio

import pytest

from work_buddy.inline.handlers import send_to_agent as handler_mod
from work_buddy.inline.models import InlineContext
from work_buddy.inline import registry


def test_handler_is_registered() -> None:
    cmd = registry.get("send-to-agent")
    assert cmd is not None
    assert "menu" in cmd.surfaces
    assert "tag" in cmd.surfaces
    assert cmd.consume_mode == "leave"
    assert cmd.persistent is False
    assert cmd.interactive is False
    assert cmd.context_scope == "paragraph"


def test_empty_selection_and_paragraph_short_circuits(monkeypatch) -> None:
    spawned: list = []
    monkeypatch.setattr(
        handler_mod.threading,
        "Thread",
        lambda **kw: spawned.append(kw) or _FakeThread(),
    )

    ctx = InlineContext(surface="menu", file_path="Notes/x.md")
    result = asyncio.run(handler_mod.send_to_agent(ctx))
    assert result["status"] == "empty"
    assert spawned == []


def test_handler_schedules_background_thread(monkeypatch) -> None:
    captured: dict = {}

    class _FT:
        def __init__(self, **kw):
            captured.update(kw)

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(handler_mod.threading, "Thread", _FT)

    ctx = InlineContext(
        surface="menu",
        file_path="Notes/Example.md",
        selection="Make a task out of this",
        paragraph="Some paragraph context",
        cursor_line=12,
        hint="this is a followup",
    )

    result = asyncio.run(handler_mod.send_to_agent(ctx))

    assert result == {
        "status": "queued",
        "surface": "menu",
        "file_path": "Notes/Example.md",
    }
    assert captured["started"] is True
    assert captured["daemon"] is True
    assert captured["target"] is handler_mod._run_producer
    kwargs = captured["kwargs"]
    assert kwargs["file_path"] == "Notes/Example.md"
    assert kwargs["selection"] == "Make a task out of this"
    assert kwargs["paragraph"] == "Some paragraph context"
    assert kwargs["cursor_line"] == 12
    assert kwargs["hint"] == "this is a followup"


def test_run_producer_catches_exceptions(monkeypatch) -> None:
    def _boom(**kw):
        raise RuntimeError("LLM down")

    import importlib
    mod = importlib.import_module(
        "work_buddy.triage.capabilities.inline_triage_scan"
    )
    monkeypatch.setattr(mod, "inline_triage_scan", _boom)
    # Should not raise even though the inner call blew up
    handler_mod._run_producer(
        file_path="x", selection="y", paragraph="", cursor_line=0, hint="",
    )


def test_run_producer_invokes_scan(monkeypatch) -> None:
    seen: dict = {}

    def _fake_scan(**kw):
        seen.update(kw)
        return {"status": "ok", "run_id": "r1", "submitted": 1}

    import importlib
    mod = importlib.import_module(
        "work_buddy.triage.capabilities.inline_triage_scan"
    )
    monkeypatch.setattr(mod, "inline_triage_scan", _fake_scan)

    handler_mod._run_producer(
        file_path="Notes/a.md",
        selection="sel",
        paragraph="para",
        cursor_line=4,
        hint="hint",
    )

    assert seen["file_path"] == "Notes/a.md"
    assert seen["selection"] == "sel"
    assert seen["paragraph"] == "para"
    assert seen["cursor_line"] == 4
    assert seen["hint"] == "hint"
    assert seen["force"] is True


class _FakeThread:
    def start(self) -> None:
        return None
