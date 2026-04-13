"""Documentation source adapter — knowledge store units to IR documents.

Indexes the unified knowledge store so documentation appears in the IR
engine alongside conversations, projects, and Chrome tabs. Each PromptUnit
becomes one Document with fields optimized for documentation retrieval.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from work_buddy.ir.sources.base import Document
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_STORE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "knowledge" / "store"


class DocsSource:
    """IR source adapter for the unified knowledge store."""

    @property
    def name(self) -> str:
        return "docs"

    def default_field_weights(self) -> dict[str, float]:
        return {
            "name": 3.0,
            "description": 2.5,
            "tags": 2.0,
            "content": 1.0,
        }

    def discover(self, days: int = 30) -> list[tuple[str, float]]:
        """Return all .json files in knowledge/store/ with their mtimes.

        Documentation doesn't expire — always return all files regardless
        of the days parameter.
        """
        if not _STORE_DIR.is_dir():
            return []

        items: list[tuple[str, float]] = []
        for path in sorted(_STORE_DIR.glob("*.json")):
            try:
                mtime = path.stat().st_mtime
                items.append((str(path), mtime))
            except OSError:
                continue

        return items

    def parse(self, item_id: str) -> list[Document]:
        """Parse a JSON store file into Documents (one per unit)."""
        import json
        from work_buddy.config import load_config

        path = Path(item_id)
        if not path.exists():
            return []

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to parse %s: %s", path.name, e)
            return []

        if not isinstance(data, dict):
            return []

        cfg = load_config()
        max_dense = cfg.get("ir", {}).get("dense_text_max_chars", 1500)

        docs: list[Document] = []
        for unit_path, unit_data in data.items():
            name = unit_data.get("name", unit_path.rsplit("/", 1)[-1])
            description = unit_data.get("description", "")
            kind = unit_data.get("kind", "system")
            tags = unit_data.get("tags", [])
            aliases = unit_data.get("aliases", [])
            content = unit_data.get("content", {})
            summary = content.get("summary", "")
            full = content.get("full", summary)

            # Dense text for embedding
            dense_parts = [name, description] + aliases
            if summary:
                dense_parts.append(summary[:800])
            dense_text = " ".join(dense_parts)[:max_dense]

            doc_id = f"docs:{unit_path}"

            docs.append(Document(
                doc_id=doc_id,
                source="docs",
                fields={
                    "name": name.replace("-", " ").replace("_", " "),
                    "description": description,
                    "tags": " ".join(tags),
                    "content": full[:3000],
                },
                dense_text=dense_text,
                display_text=f"[{kind}] {unit_path}: {description}",
                metadata={
                    "kind": kind,
                    "tags": tags,
                    "path": unit_path,
                    "file_path": str(path),
                    "indexed_at": time.time(),
                },
            ))

        return docs
