"""``obsidian`` + siblings — Obsidian collector wrapping.

``obsidian_collector.collect(cfg)`` returns a tuple
``(obsidian_md, tasks_md)``; wellness comes from a separate
``obsidian_collector.collect_wellness(cfg)`` call. We expose three
independent sources so a caller can ask for one without paying for
the whole Obsidian fetch:

- ``obsidian`` — journal / recent files / task-event block (index 0)
- ``obsidian_tasks`` — task list markdown (index 1; mirrors the
  legacy ``tasks_summary.md`` bundle file)
- ``obsidian_wellness`` — wellness section via ``collect_wellness``

The ``obsidian_tasks`` source is distinct from the structured
``tasks`` source in phase 5 — the structured one emits task-record
dicts for LLM prompts; this one preserves the legacy markdown for
bundle-file output and for callers already consuming that exact
format.
"""

from __future__ import annotations

from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry
from work_buddy.context.types import ContextRequest, ContextSection
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class _ObsidianTupleSource(MarkdownCollectorSource):
    """Either half of ``obsidian_collector.collect(cfg)`` as a source."""

    _index: int = 0

    def collect(self, request: ContextRequest) -> ContextSection:
        try:
            from work_buddy.collectors import obsidian_collector
        except Exception as exc:
            return ContextSection(
                source=self.name, items=[],
                metadata={"error": str(exc)},
            )
        cfg = self._build_cfg(request)
        try:
            result = obsidian_collector.collect(cfg)
        except Exception as exc:
            logger.debug("%s source: obsidian_collector.collect raised: %s", self.name, exc)
            return ContextSection(
                source=self.name, items=[],
                metadata={"error": f"{type(exc).__name__}: {exc}"},
            )
        try:
            markdown = result[self._index] or ""
        except (IndexError, TypeError):
            markdown = ""
        return ContextSection(
            source=self.name,
            items=[{"markdown": markdown, "length": len(markdown)}],
            metadata={"length": len(markdown)},
        )


class ObsidianSource(_ObsidianTupleSource):
    name = "obsidian"
    _index = 0
    _heading = "Obsidian Journal"
    _default_cfg: dict[str, Any] = {}


class ObsidianTasksSource(_ObsidianTupleSource):
    name = "obsidian_tasks"
    _index = 1
    _heading = "Obsidian Tasks"
    _default_cfg: dict[str, Any] = {}


class ObsidianWellnessSource(MarkdownCollectorSource):
    """Wellness block via the separate ``collect_wellness`` entry point."""

    name = "obsidian_wellness"
    _heading = "Obsidian Wellness"
    _default_cfg: dict[str, Any] = {}

    def collect(self, request: ContextRequest) -> ContextSection:
        try:
            from work_buddy.collectors import obsidian_collector
        except Exception as exc:
            return ContextSection(
                source=self.name, items=[],
                metadata={"error": str(exc)},
            )
        cfg = self._build_cfg(request)
        try:
            markdown = obsidian_collector.collect_wellness(cfg) or ""
        except Exception as exc:
            logger.debug(
                "%s source: obsidian_collector.collect_wellness raised: %s",
                self.name, exc,
            )
            return ContextSection(
                source=self.name, items=[],
                metadata={"error": f"{type(exc).__name__}: {exc}"},
            )
        return ContextSection(
            source=self.name,
            items=[{"markdown": markdown, "length": len(markdown)}],
            metadata={"length": len(markdown)},
        )


_registry.register(ObsidianSource())
_registry.register(ObsidianTasksSource())
_registry.register(ObsidianWellnessSource())
