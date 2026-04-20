"""Render a :class:`Context` into a markdown block or JSON.

The curator is the presentation-layer counterpart to
:class:`ContextCollector`. It holds no state and reads no data — it
just asks each source's ``render()`` to produce text at the caller's
chosen depth, glues sections together, and truncates if
``max_chars`` is set.

Depth resolution uses :meth:`ContextRequest.depth_for` so callers can
override per-source without touching the raw data.
"""

from __future__ import annotations

from typing import Any

from work_buddy.context import registry
from work_buddy.context.types import (
    Context,
    ContextDepth,
    ContextRequest,
    ContextSection,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


_DEFAULT_HEADER = "## User's Current Context"


class ContextCurator:
    """Render a collected :class:`Context` for LLM prompts or bundle files."""

    def curate(
        self,
        context: Context,
        *,
        depth: ContextDepth | None = None,
        per_source_depth: dict[str, ContextDepth] | None = None,
        max_chars: int | None = None,
        header: str | None = _DEFAULT_HEADER,
        format: str = "markdown",
    ) -> str:
        """Render ``context`` into the chosen format.

        When ``format="markdown"`` the output is a single prompt-ready
        block starting with ``header`` (omit with ``header=None``)
        followed by one section per source. Each source's ``render``
        is called at its effective depth — the explicit
        ``per_source_depth`` or ``depth`` argument overrides the
        request's values when supplied; otherwise we use
        :meth:`ContextRequest.depth_for`.

        When ``format="json"`` the output is a compact JSON string —
        one top-level key per section, value is the source's
        ``items`` list. ``max_chars`` applies to the final string in
        both modes.
        """
        req = context.request
        effective_depth = depth if depth is not None else req.depth

        if format == "json":
            return self._render_json(context, max_chars=max_chars)

        if format != "markdown":
            raise ValueError(
                f"ContextCurator.curate: unknown format {format!r}; expected "
                "'markdown' or 'json'."
            )

        lines: list[str] = []
        if header:
            lines.append(header)
            lines.append("")

        for name, section in context.sections.items():
            block = self._render_section(
                section=section,
                name=name,
                depth=self._effective_depth(
                    name, effective_depth, per_source_depth, req,
                ),
            )
            if block:
                lines.append(block.rstrip())
                lines.append("")

        rendered = "\n".join(lines).rstrip() + "\n" if lines else ""

        if max_chars is not None and len(rendered) > max_chars:
            rendered = _truncate_markdown(rendered, max_chars)

        return rendered

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _effective_depth(
        name: str,
        default: ContextDepth,
        override: dict[str, ContextDepth] | None,
        req: ContextRequest,
    ) -> ContextDepth:
        if override and name in override:
            return override[name]
        return req.depth_for(name) if req.per_source_depth else default

    def _render_section(
        self,
        *,
        section: ContextSection,
        name: str,
        depth: ContextDepth,
    ) -> str:
        """Call the registered source's ``render``; swallow + log on failure."""
        source = registry.get(name)
        if source is None:
            logger.debug(
                "ContextCurator: source %r not registered; rendering raw items",
                name,
            )
            return _fallback_render(section)

        try:
            return source.render(section, depth)
        except Exception:
            logger.exception(
                "ContextCurator: source %r.render raised; falling back to raw items",
                name,
            )
            return _fallback_render(section)

    @staticmethod
    def _render_json(context: Context, *, max_chars: int | None) -> str:
        import json

        payload: dict[str, Any] = {
            name: {
                "items": section.items,
                "metadata": section.metadata,
                "fetched_at": section.fetched_at.isoformat(),
            }
            for name, section in context.sections.items()
        }
        rendered = json.dumps(payload, default=str, separators=(",", ":"))
        if max_chars is not None and len(rendered) > max_chars:
            rendered = rendered[: max_chars - 1].rstrip() + "…"
        return rendered


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fallback_render(section: ContextSection) -> str:
    """Render when the source isn't registered or its render fails.

    Emits a minimal ``### <source>`` heading + bullet list. Keeps the
    prompt usable even when a source has been removed mid-flight.
    """
    lines = [f"### {section.source} ({len(section.items)})"]
    for item in section.items[:10]:
        lines.append(f"- {item!r}")
    if len(section.items) > 10:
        lines.append(f"- … ({len(section.items) - 10} more)")
    return "\n".join(lines)


def _truncate_markdown(text: str, max_chars: int) -> str:
    """Truncate respecting section boundaries when possible.

    Prefers to cut at a section break (double newline) so we don't
    chop a bullet in half. Falls back to hard truncation if no break
    is in range.
    """
    if len(text) <= max_chars:
        return text
    # Look for the last section break within the budget.
    search_slice = text[: max_chars]
    last_break = search_slice.rfind("\n\n")
    if last_break > max_chars // 2:
        # Section break is at least halfway in — use it.
        return search_slice[: last_break].rstrip() + "\n\n[…truncated…]\n"
    # Hard truncate on word boundary.
    hard = search_slice.rstrip()
    last_space = hard.rfind(" ", max_chars - 200)
    if last_space > 0:
        hard = hard[:last_space]
    return hard + " […truncated…]\n"
