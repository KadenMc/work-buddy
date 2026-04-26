"""Apply the post-execution mutation declared by an inline command.

Modes:

- ``leave`` — no-op (default for persistent watchers while active).
- ``strip`` — remove the tag from the note.
- ``annotate`` — leave tag; append a ``> [!work-buddy]`` callout below.
- ``replace`` — rewrite ``#wb/cmd/foo`` → ``#wb/cmd/foo/done`` on the tag line.

All mutations route through :mod:`work_buddy.obsidian.bridge` so Obsidian's
dirty-buffer handling is preserved.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from work_buddy.inline.models import InlineContext
from work_buddy.obsidian.bridge import read_file, write_file
from work_buddy.obsidian.errors import ObsidianError

logger = logging.getLogger(__name__)


_VALID_MODES = {"leave", "strip", "annotate", "replace"}


def _resolve_mode(handler_default: str) -> str:
    """Resolve the effective consume mode, honouring user overrides.

    TODO: the expected override key is ``features.inline.consume_mode_override``
    once ``work_buddy/features/`` lands. For now we fall back to the
    handler's declared mode.
    """
    try:  # pragma: no cover — placeholder until features package exists
        from work_buddy import features  # type: ignore

        override = features.get("inline.consume_mode_override")  # type: ignore[attr-defined]
        if override in _VALID_MODES:
            return override
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("features lookup failed: %s", exc)
    return handler_default if handler_default in _VALID_MODES else "leave"


def _summarize_result(result: dict | None) -> str:
    if not isinstance(result, dict):
        return str(result)[:120] if result is not None else ""
    if result.get("thread_id"):
        return f"thread:{result['thread_id']} ({result.get('status', 'ok')})"
    if result.get("status"):
        return str(result["status"])
    return str(result)[:120]


def _tag_token(tag_name: str) -> str:
    """Return the in-note token (ensures leading ``#``)."""
    if not tag_name:
        return ""
    return tag_name if tag_name.startswith("#") else "#" + tag_name


def apply(mode: str, ctx: InlineContext, result: dict | None) -> dict:
    """Apply the resolved consume mode and return a summary dict.

    Short-circuits with ``no_file`` when the context has no file (e.g.
    menu invocations without an active editor file).
    """
    effective = _resolve_mode(mode)
    if effective == "leave":
        return {"mutated": False, "note": "no-op", "mode": effective}

    if not ctx.file_path:
        return {"mutated": False, "note": "no_file", "mode": effective}

    content = read_file(ctx.file_path)
    if content is None:
        return {"mutated": False, "note": "read_failed", "mode": effective}

    lines = content.splitlines()
    tag_line = ctx.cursor_line if ctx.tag else ctx.cursor_line
    if tag_line is None or tag_line < 0 or tag_line >= len(lines):
        # Nothing to mutate deterministically
        return {"mutated": False, "note": "no_tag_line", "mode": effective}

    tag_token = _tag_token((ctx.tag or {}).get("name", "")) if ctx.tag else ""

    # Helper: post-CP6 write_file raises typed ObsidianError on failure.
    # consume.apply is a fire-and-forget post-execution mutator — it
    # should NOT propagate bridge failures into the calling agent's
    # workflow (the user's command already succeeded; the consume step
    # is cosmetic). Catch typed exceptions and report mutated=False.
    def _safe_write(file_path: str, new_content: str) -> tuple[bool, str | None]:
        try:
            write_file(file_path, new_content)
            return True, None
        except ObsidianError as exc:
            logger.warning(
                "inline.consume: write failed for %s (%s)",
                file_path, exc.error_kind,
            )
            return False, exc.error_kind

    if effective == "strip":
        if not tag_token:
            return {"mutated": False, "note": "no_tag", "mode": effective}
        # Remove first occurrence of the exact token on that line
        new_line = re.sub(
            r"\s*" + re.escape(tag_token) + r"(?![\w/-])",
            "",
            lines[tag_line],
            count=1,
        ).rstrip()
        lines[tag_line] = new_line
        new_content = "\n".join(lines)
        if content.endswith("\n"):
            new_content += "\n"
        ok, err_kind = _safe_write(ctx.file_path, new_content)
        return {
            "mutated": ok,
            "note": "stripped" if ok else f"write_failed:{err_kind}",
            "mode": effective,
            "tag": tag_token,
        }

    if effective == "replace":
        if not tag_token:
            return {"mutated": False, "note": "no_tag", "mode": effective}
        new_line = re.sub(
            re.escape(tag_token) + r"(?![\w/-])",
            tag_token + "/done",
            lines[tag_line],
            count=1,
        )
        lines[tag_line] = new_line
        new_content = "\n".join(lines)
        if content.endswith("\n"):
            new_content += "\n"
        ok, err_kind = _safe_write(ctx.file_path, new_content)
        return {
            "mutated": ok,
            "note": "replaced" if ok else f"write_failed:{err_kind}",
            "mode": effective,
            "tag": tag_token,
        }

    if effective == "annotate":
        summary = _summarize_result(result)
        ts = datetime.now(timezone.utc).isoformat()
        callout = [
            f"> [!work-buddy] Processed at {ts}",
            f"> Result: {summary}",
        ]
        insert_at = tag_line + 1
        new_lines = lines[:insert_at] + callout + lines[insert_at:]
        new_content = "\n".join(new_lines)
        if content.endswith("\n"):
            new_content += "\n"
        ok, err_kind = _safe_write(ctx.file_path, new_content)
        return {
            "mutated": ok,
            "note": "annotated" if ok else f"write_failed:{err_kind}",
            "mode": effective,
        }

    return {"mutated": False, "note": f"unknown_mode:{effective}", "mode": effective}
