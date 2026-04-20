"""Tests for the inline dispatcher."""

from __future__ import annotations

import pytest

from work_buddy.inline import consume, dispatcher, registry, store
from work_buddy.inline.models import InlineContext


FIXTURE = (
    "line 0\n"
    "line 1 #wb/cmd/test/sync trailing\n"
    "line 2\n"
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Isolate registry + SQLite + bridge for each test."""
    snapshot = dict(registry._COMMANDS)  # noqa: SLF001
    registry._COMMANDS.clear()  # noqa: SLF001

    # Redirect SQLite to a temp file and rebuild schema there.
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "inline.db")
    conn = store.get_connection()
    store._ensure_schema(conn)  # noqa: SLF001
    conn.close()

    # Stub the bridge so consume doesn't try to hit Obsidian.
    state = {"written": None, "content": FIXTURE}
    monkeypatch.setattr(consume, "read_file", lambda p: state["content"])

    def _write(p, c):
        state["written"] = (p, c)
        state["content"] = c
        return True

    monkeypatch.setattr(consume, "write_file", _write)

    try:
        yield state
    finally:
        registry._COMMANDS.clear()  # noqa: SLF001
        registry._COMMANDS.update(snapshot)  # noqa: SLF001


def test_sync_handler_dispatch(_isolate):
    calls: list[InlineContext] = []

    @registry.inline_command(
        name="test/sync",
        surfaces=["menu", "tag"],
        consume_mode="leave",
        context_scope="line",
    )
    def h(ctx):
        calls.append(ctx)
        return {"ok": True}

    out = dispatcher.dispatch_sync(
        "menu",
        {
            "command": "test/sync",
            "file_path": "Notes/A.md",
            "selection": "hello",
            "cursor_line": 0,
            "full_text": FIXTURE,
        },
    )
    assert out["result"] == {"ok": True}
    assert out["consume"]["mutated"] is False  # leave
    assert len(calls) == 1
    assert calls[0].selection == "hello"
    # Invocation was logged and marked completed.
    invs = store.list_invocations()
    assert len(invs) == 1
    assert invs[0].status == "completed"


def test_async_handler_awaited(_isolate):
    @registry.inline_command(name="test/async", surfaces=["menu"])
    async def h(ctx):
        return {"async": True}

    out = dispatcher.dispatch_sync(
        "menu", {"command": "test/async", "file_path": "A.md", "cursor_line": 0, "full_text": "x"}
    )
    assert out["result"] == {"async": True}


def test_unknown_command_error(_isolate):
    out = dispatcher.dispatch_sync("menu", {"command": "nope", "file_path": "A.md"})
    assert out == {"error": "unknown_command", "command": "nope"}


def test_persistent_tag_registers_watcher(_isolate):
    @registry.inline_command(
        name="test/pers",
        surfaces=["tag"],
        persistent=True,
    )
    def h(ctx):  # pragma: no cover — should not be invoked
        raise AssertionError("persistent handler should not execute on register")

    out = dispatcher.dispatch_sync(
        "tag",
        {
            "tag": "wb/cmd/test/pers",
            "tag_line": 2,
            "file_path": "Notes/B.md",
            "full_text": "a\nb\n#wb/cmd/test/pers\n",
        },
    )
    assert "registered" in out
    assert out["persistent"] is True
    watchers = store.list_watchers(command_name="test/pers")
    assert len(watchers) == 1
    assert watchers[0].file_path == "Notes/B.md"


def test_handler_exception_marks_failed(_isolate):
    @registry.inline_command(name="test/boom", surfaces=["menu"])
    def h(ctx):
        raise RuntimeError("boom")

    out = dispatcher.dispatch_sync(
        "menu", {"command": "test/boom", "file_path": "A.md", "cursor_line": 0, "full_text": "x"}
    )
    assert out["error"] == "boom"
    invs = store.list_invocations()
    assert invs[0].status == "failed"


def test_surface_unsupported(_isolate):
    @registry.inline_command(name="test/menu-only", surfaces=["menu"])
    def h(ctx):
        return {}

    out = dispatcher.dispatch_sync(
        "tag",
        {"tag": "wb/cmd/test/menu-only", "tag_line": 0, "file_path": "A.md", "full_text": "x"},
    )
    assert out["error"] == "surface_unsupported"


def test_tag_surface_parses_command_from_tag(_isolate):
    @registry.inline_command(name="test/fromtag", surfaces=["tag"])
    def h(ctx):
        return {"hit": True}

    out = dispatcher.dispatch_sync(
        "tag",
        {
            "tag": "#wb/cmd/test/fromtag",
            "tag_line": 0,
            "file_path": "A.md",
            "full_text": "line\n",
        },
    )
    assert out["result"] == {"hit": True}
