"""Project source adapter — project descriptions and observations to IR documents.

Indexes project identity records and their temporal observations from the
project store.  Each project description becomes one document; each
observation becomes one document.  This enables semantic search over project
state, trajectory, and chat-sourced intelligence.
"""

from __future__ import annotations

from typing import Any

from work_buddy.ir.sources.base import Document
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class ProjectsSource:
    """IR source adapter for the project store."""

    @property
    def name(self) -> str:
        return "projects"

    def default_field_weights(self) -> dict[str, float]:
        return {
            "project_name": 2.0,
            "content": 1.5,
            "source": 0.5,
            "status": 0.5,
        }

    def discover(self, days: int = 30) -> list[tuple[str, float]]:
        """Return project store DB path + mtime if it exists.

        Like ChromeSource, the project store is a single SQLite file.
        We return it as the single indexable item.
        """
        from work_buddy.projects.store import _db_path

        db = _db_path()
        if not db.exists():
            return []

        try:
            mtime = db.stat().st_mtime
        except OSError:
            return []

        return [(str(db), mtime)]

    def parse(self, item_id: str) -> list[Document]:
        """Parse the project store into documents.

        Produces:
        - One document per project with a description
        - One document per observation
        """
        from work_buddy.projects import store
        from work_buddy.config import load_config

        cfg = load_config()
        max_dense = cfg.get("ir", {}).get("dense_text_max_chars", 1500)

        docs: list[Document] = []

        projects = store.list_projects()
        for p in projects:
            slug = p["slug"]
            name = p.get("name", slug)
            status = p.get("status", "active")
            description = p.get("description")

            # Document from project description (if set)
            if description:
                doc_id = f"project:{slug}:desc"
                dense = f"{name} — {description}"[:max_dense]
                docs.append(Document(
                    doc_id=doc_id,
                    source="projects",
                    fields={
                        "project_name": name,
                        "content": description,
                        "source": "description",
                        "status": status,
                    },
                    dense_text=dense,
                    display_text=f"[{slug}] {description[:120]}",
                    metadata={
                        "project_slug": slug,
                        "status": status,
                        "type": "description",
                    },
                ))

            # Note: project observations now live in the Hindsight project
            # memory bank and are searched via recall_project_context().
            # Only project descriptions are indexed here for cross-source IR.

        logger.info(
            "Parsed %d documents from %d projects.",
            len(docs), len(projects),
        )
        return docs
