"""``smart`` context source — Smart Connections (Obsidian) readiness + events.

The legacy collector degrades gracefully when Obsidian / Smart
Connections isn't available, so this wrapper is safe to register even
on machines without the plugin.

Slice 6 adds ``drill_down`` so the reference-filing pipeline can
fetch the content of candidate vault items returned from
``semantic_search`` / ``find_related``.  This is the
high-level-association → fine-grain-detail bridge the ROADMAP §6
foundational principle calls out: the vault's semantic index already
provides the high-level associations; ``drill_down`` reuses the
existing ``get_item_content`` capability to bridge to file-level
detail without inventing a new pipeline.
"""

from __future__ import annotations

from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class SmartSource(MarkdownCollectorSource):
    name = "smart"
    _heading = "Smart Connections"
    _default_cfg: dict[str, Any] = {}

    def __init__(self):
        try:
            from work_buddy.collectors import smart_collector
            self._collect_fn = smart_collector.collect
        except Exception:
            self._collect_fn = None

    def drill_down(self, item_id: str, field: str) -> dict[str, Any]:
        """Fetch detail for a vault item by its SmartSource/SmartBlock key.

        Slice 6: implements the existing ``ContextSource.drill_down``
        protocol on the vault's semantic index.  ``item_id`` is a
        Smart key (e.g. ``"journal/2026-04-30.md"`` or
        ``"path/file.md#Heading"``).

        Supported fields:

        - ``"content"`` — full text of the item via
          :func:`work_buddy.obsidian.smart.env.get_item_content`.
        - ``"related"`` — semantic neighbours of the item via
          :func:`work_buddy.obsidian.smart.env.find_related`.

        Other fields raise ``KeyError`` with the supported set.

        Raises:
            NotImplementedError: when Obsidian / Smart Connections
                isn't reachable; the reference-filing pipeline catches
                this and degrades to a "no candidate content available"
                placement.
        """
        if not item_id:
            raise KeyError("SmartSource.drill_down: item_id required")

        try:
            from work_buddy.obsidian.smart.env import (
                find_related as _find_related,
                get_item_content as _get_item_content,
            )
        except ImportError as exc:  # pragma: no cover — defensive
            raise NotImplementedError(
                f"SmartSource.drill_down: smart.env not importable: {exc}"
            ) from exc

        if field == "content":
            try:
                return _get_item_content(item_id)
            except Exception as exc:
                logger.debug(
                    "SmartSource.drill_down: get_item_content failed for %s: %s",
                    item_id, exc,
                )
                raise NotImplementedError(
                    f"SmartSource.drill_down: bridge unavailable for content lookup ({exc})"
                ) from exc

        if field == "related":
            try:
                items = _find_related(item_id, limit=10)
                return {"key": item_id, "related": items}
            except Exception as exc:
                logger.debug(
                    "SmartSource.drill_down: find_related failed for %s: %s",
                    item_id, exc,
                )
                raise NotImplementedError(
                    f"SmartSource.drill_down: bridge unavailable for related lookup ({exc})"
                ) from exc

        raise KeyError(
            f"SmartSource.drill_down: unknown field {field!r}. "
            "Valid: 'content', 'related'."
        )


_registry.register(SmartSource())
