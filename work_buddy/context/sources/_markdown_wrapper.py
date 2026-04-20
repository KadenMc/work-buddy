"""Shared helper: wrap a legacy markdown-emitting collector as a ``ContextSource``.

Wave-1 sources (git / tasks / projects / chrome) re-implement the
fetch and emit structured items. Wave-2/3 sources (obsidian / chat /
calendar / day_planner / session_activity / message / smart /
datacore) delegate to the existing ``work_buddy/collectors/*.py``
modules which already produce prompt-ready markdown.

The wrapper here is deliberately thin:

  - ``collect`` calls the legacy collector with a config dict derived
    from ``ContextRequest`` (window/custom params forwarded),
    captures the markdown string, and stashes it as a single item
    ``{"markdown": str, "length": int}``.
  - ``render`` returns that markdown verbatim, capped at a
    depth-aware character budget (BRIEF trims hard, NORMAL trims
    mildly, DEEP returns the full body).

Later phases can replace individual wrappers with structured sources
when a caller needs drill-down or per-item rendering. Until then, this
keeps the legacy pipeline usable from the unified context stack with
minimum rework.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from work_buddy.context.types import (
    BaseContextSource,
    ContextDepth,
    ContextRequest,
    ContextSection,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Default depth → character budget for wrapped markdown. Collectors
# commonly produce 2-10KB of text; BRIEF keeps a prompt-friendly slice,
# DEEP returns the whole thing.
_DEFAULT_DEPTH_BUDGET: dict[ContextDepth, int | None] = {
    ContextDepth.BRIEF: 600,
    ContextDepth.NORMAL: 2500,
    ContextDepth.DEEP: None,
}


class MarkdownCollectorSource(BaseContextSource):
    """Wraps a legacy ``collect(cfg) -> str`` collector as a ContextSource.

    Subclasses (or instances) need:
        - ``name``: stable source identifier used for cache paths.
        - ``_collect_fn``: the legacy function to call.
        - ``_default_cfg``: baseline cfg dict merged with caller overrides.
        - ``_heading``: the H3 used in the rendered block.
    """

    _collect_fn: Callable[[dict[str, Any]], str] | None = None
    _default_cfg: dict[str, Any] = {}
    _heading: str = ""

    def collect(self, request: ContextRequest) -> ContextSection:
        if self._collect_fn is None:
            return ContextSection(
                source=self.name,
                items=[],
                metadata={"error": "collector not bound"},
            )

        cfg = self._build_cfg(request)
        try:
            markdown = self._collect_fn(cfg) or ""
        except Exception as exc:
            logger.debug(
                "%s source: legacy collector raised: %s", self.name, exc,
            )
            return ContextSection(
                source=self.name,
                items=[],
                metadata={"error": f"{type(exc).__name__}: {exc}"},
            )

        return ContextSection(
            source=self.name,
            items=[{"markdown": markdown, "length": len(markdown)}],
            metadata={
                "cfg": _jsonable_cfg(cfg),
                "length": len(markdown),
            },
        )

    def render(self, section: ContextSection, depth: ContextDepth) -> str:
        items = section.items or []
        if not items:
            return ""
        markdown = (items[0].get("markdown") or "").strip()
        if not markdown:
            return ""
        budget = _DEFAULT_DEPTH_BUDGET.get(depth)
        if budget is not None and len(markdown) > budget:
            markdown = _smart_truncate(markdown, budget)
        if self._heading and not markdown.startswith("#"):
            return f"### {self._heading}\n{markdown}"
        return markdown

    # -- internal -----------------------------------------------------------

    def _build_cfg(self, request: ContextRequest) -> dict[str, Any]:
        """Compose the legacy cfg dict from the ContextRequest + overrides."""
        cfg = dict(self._default_cfg)

        since, until = _window_bounds(request)
        if since is not None:
            cfg.setdefault("since", since.isoformat())
        if until is not None:
            cfg.setdefault("until", until.isoformat())

        # Per-source overrides win over defaults and window bounds.
        cfg.update(request.custom_for(self.name))
        return cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _window_bounds(
    request: ContextRequest,
) -> tuple[date | None, date | None]:
    """``(since, until)`` derived from request's target_date + window_days.

    ``target_date=None`` leaves both as None so each legacy collector
    uses its own default window. When ``target_date`` is set the
    window is centered on it with ``±window_days``.
    """
    if request.target_date is None:
        return None, None
    center = request.target_date
    days = max(request.window_days, 0)
    return center - timedelta(days=days), center + timedelta(days=days)


def _smart_truncate(text: str, budget: int) -> str:
    """Truncate respecting section boundaries if one is in range."""
    if len(text) <= budget:
        return text
    head = text[:budget]
    last_break = head.rfind("\n\n")
    if last_break > budget // 2:
        return head[:last_break].rstrip() + "\n\n[…truncated…]"
    last_space = head.rfind(" ", budget - 200)
    if last_space > 0:
        return head[:last_space].rstrip() + " […truncated…]"
    return head + "…"


def _jsonable_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Shallow copy; drops non-JSON-safe values so sections roundtrip."""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x for x in v if isinstance(x, (str, int, float, bool))]
        elif isinstance(v, dict):
            out[k] = _jsonable_cfg(v)
        # else: drop quietly
    return out
