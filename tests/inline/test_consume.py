"""Tests for the consume-mode post-execution mutations."""

from __future__ import annotations

import pytest

from work_buddy.inline import consume
from work_buddy.inline.models import InlineContext


FIXTURE = (
    "# Heading\n"
    "\n"
    "Some prose here.\n"
    "A trigger line #wb/cmd/task/new extra text\n"
    "Trailing content.\n"
)


@pytest.fixture
def captured(monkeypatch):
    """Capture bridge read/write interactions."""
    state: dict = {"written": None, "file_contents": FIXTURE}

    def fake_read(path: str) -> str:
        return state["file_contents"]

    def fake_write(path: str, content: str) -> bool:
        state["written"] = (path, content)
        state["file_contents"] = content
        return True

    monkeypatch.setattr(consume, "read_file", fake_read)
    monkeypatch.setattr(consume, "write_file", fake_write)
    return state


def _ctx(tag_line: int = 3) -> InlineContext:
    return InlineContext(
        surface="tag",
        file_path="Notes/Example.md",
        cursor_line=tag_line,
        full_text=FIXTURE,
        line_text=FIXTURE.splitlines()[tag_line],
        tag={"name": "wb/cmd/task/new", "line": tag_line},
    )


def test_leave_is_noop(captured):
    out = consume.apply("leave", _ctx(), {"status": "ok"})
    assert out["mutated"] is False
    assert captured["written"] is None


def test_no_file_short_circuits(captured):
    ctx = _ctx()
    ctx.file_path = None
    out = consume.apply("strip", ctx, {"status": "ok"})
    assert out == {"mutated": False, "note": "no_file", "mode": "strip"}
    assert captured["written"] is None


def test_strip_removes_tag_preserving_rest(captured):
    out = consume.apply("strip", _ctx(), {"status": "ok"})
    assert out["mutated"] is True
    _, content = captured["written"]
    line3 = content.splitlines()[3]
    assert "#wb/cmd/task/new" not in line3
    assert "A trigger line" in line3
    assert "extra text" in line3


def test_replace_renames_tag_to_done(captured):
    out = consume.apply("replace", _ctx(), {"status": "ok"})
    assert out["mutated"] is True
    _, content = captured["written"]
    line3 = content.splitlines()[3]
    assert "#wb/cmd/task/new/done" in line3


def test_annotate_inserts_callout_after_tag_line(captured):
    out = consume.apply("annotate", _ctx(), {"thread_id": "abc", "status": "awaiting"})
    assert out["mutated"] is True
    _, content = captured["written"]
    lines = content.splitlines()
    assert lines[3].startswith("A trigger line")
    assert lines[4].startswith("> [!work-buddy]")
    assert "abc" in lines[5]
