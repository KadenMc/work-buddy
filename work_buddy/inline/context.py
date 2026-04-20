"""Build :class:`InlineContext` from surface payloads.

Menu payloads carry an explicit cursor / selection from the Obsidian editor.
Tag payloads carry the tag position from ``metadataCache``; the context
scope in the handler decides how much surrounding text we pre-compute.
"""

from __future__ import annotations

import logging

from work_buddy.inline.models import InlineContext

logger = logging.getLogger(__name__)


def extract_paragraph(full_text: str, cursor_line: int | None) -> str:
    """Return the blank-line-bounded block containing ``cursor_line``."""
    if not full_text or cursor_line is None:
        return ""
    lines = full_text.splitlines()
    if cursor_line < 0 or cursor_line >= len(lines):
        return ""
    # Walk up to the last blank line
    start = cursor_line
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = cursor_line
    while end < len(lines) - 1 and lines[end + 1].strip():
        end += 1
    return "\n".join(lines[start : end + 1])


def extract_section(full_text: str, cursor_line: int | None) -> str:
    """Return the heading-bounded block containing ``cursor_line``.

    Walks backward to the nearest Markdown heading and forward to the next
    heading at the same or higher level.
    """
    if not full_text or cursor_line is None:
        return ""
    lines = full_text.splitlines()
    if cursor_line < 0 or cursor_line >= len(lines):
        return ""

    def heading_level(s: str) -> int:
        stripped = s.lstrip()
        if not stripped.startswith("#"):
            return 0
        level = 0
        for ch in stripped:
            if ch == "#":
                level += 1
            else:
                break
        return level if level <= 6 else 0

    # Walk back for the owning heading
    start = cursor_line
    owning_level = 0
    while start >= 0:
        lvl = heading_level(lines[start])
        if lvl > 0:
            owning_level = lvl
            break
        start -= 1
    if start < 0:
        start = 0
        owning_level = 0

    end = cursor_line
    while end < len(lines) - 1:
        nxt = heading_level(lines[end + 1])
        if nxt > 0 and (owning_level == 0 or nxt <= owning_level):
            break
        end += 1
    return "\n".join(lines[start : end + 1])


def build_context(surface: str, payload: dict, scope: str) -> InlineContext:
    """Assemble an :class:`InlineContext` for the given surface/scope."""
    if surface == "menu":
        ctx = InlineContext(
            surface="menu",
            file_path=payload.get("file_path"),
            selection=payload.get("selection", "") or "",
            cursor_line=payload.get("cursor_line"),
            cursor_ch=payload.get("cursor_ch"),
            full_text=payload.get("full_text", "") or "",
            hint=payload.get("hint", "") or "",
        )
    elif surface == "tag":
        tag_name = payload.get("tag", "")
        tag_line = payload.get("tag_line")
        ctx = InlineContext(
            surface="tag",
            file_path=payload.get("file_path"),
            cursor_line=tag_line,
            full_text=payload.get("full_text", "") or "",
            tag={"name": tag_name, "line": tag_line} if tag_name else None,
            hint=payload.get("hint", "") or "",
        )
    else:
        raise ValueError(f"Unknown inline surface: {surface!r}")

    # Derive line / paragraph / section from full_text + cursor_line
    if ctx.full_text and ctx.cursor_line is not None:
        lines = ctx.full_text.splitlines()
        if 0 <= ctx.cursor_line < len(lines):
            ctx.line_text = lines[ctx.cursor_line]
        if scope in ("paragraph", "section", "file"):
            ctx.paragraph = extract_paragraph(ctx.full_text, ctx.cursor_line)
        if scope in ("section", "file"):
            ctx.section = extract_section(ctx.full_text, ctx.cursor_line)
        # scope == "file" keeps full_text populated; other scopes keep it
        # too (it's already loaded) — the handler can ignore fields it
        # doesn't need.
    return ctx
