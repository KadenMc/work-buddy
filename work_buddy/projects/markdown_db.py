"""``ProjectMarkdownDB`` — the projects registry as a :class:`MarkdownDB`.

The second concrete MarkdownDB subclass. It gives project descriptions a
markdown surface so the long-form prose the original task (t-98d34cf6)
worried about losing becomes a first-class, git-syncable, hand-editable
file.

## Note layout — a single flat directory

All project notes live in ONE vault directory: ``<projects.markdown_dir>``
from config (default ``work-buddy/projects``, a sibling of
``contracts.vault_path``). Each project is ``<dir>/<slug>.md``. The
directory is a configurable Repository-Setup requirement
(``core/config/projects-markdown-dir``).

## Status: parallel, not yet the cutover

This is additive. The projects store and its dashboard edit path are
untouched. Wiring this in — repointing ``POST /api/projects/<slug>`` at
:meth:`apply_mutation`, scheduling a ``project_sync`` drift cron — is a
deliberate, reviewed cutover step, intentionally NOT done here.

## Materialization

:func:`materialize_projects` performs the one-time store → markdown flip
— writing a note for every project that lacks one, never overwriting an
existing file. It defaults to a dry run.

## Relation to the projects store

The projects store is a relational temporal model: a surrogate-``id``
PK, ``project_folders`` / ``project_aliases`` child tables, and
append-only ``project_revisions`` history. This subclass keys on
``slug`` (the markdown surface is per-slug) and goes through the
store's public CRUD — ``list_projects``, ``upsert_project``,
``update_project``, ``delete_project`` — so the store's
revision-writing and event-publishing happen automatically. The store's
``project_revisions`` history and this subsystem's ``lww_meta`` log are
distinct concerns (full-state snapshots for the temporal model vs.
per-field write provenance for cross-surface conflict resolution); a
``ProjectMarkdownDB`` defaults to :class:`NullLwwLog`, so no ``lww_meta``
rows are written for projects unless a caller wires one in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.markdown_db import FieldSpec, MarkdownDB
from work_buddy.markdown_db.storage_helpers import atomic_write_text
from work_buddy.markdown_db.types import ParsedFileRow
from work_buddy.projects import store as project_store
from work_buddy.projects.note_format import (
    ProjectNoteParseError,
    parse_project_note,
    render_project_note,
)

logger = get_logger(__name__)

# Fallback used only if config carries no projects.markdown_dir (it
# always does — see config.DEFAULTS). Vault-relative.
_DEFAULT_MARKDOWN_DIR = "work-buddy/projects"


def projects_markdown_dir() -> Path:
    """Absolute path to the single directory holding project notes.

    Resolved from ``projects.markdown_dir`` in config (vault-relative),
    joined onto ``vault_root``.
    """
    cfg = load_config()
    rel = cfg.get("projects", {}).get("markdown_dir") or _DEFAULT_MARKDOWN_DIR
    return Path(cfg.get("vault_root", "")) / rel


class ProjectMarkdownDB(MarkdownDB):
    """:class:`MarkdownDB` over project notes ⇄ the ``projects`` store."""

    table_name = "projects"
    pk_column = "slug"

    # Orphan-in-store deletion is OFF until the vault is materialized.
    # Before materialize_projects() runs, NO project has a markdown note,
    # so every store project looks like an "orphan in store" — with this
    # True, the first reconcile_drift pass would soft-delete the entire
    # projects registry. Flip to True only as part of the projects
    # cutover, AFTER materialization is confirmed. See architecture/
    # markdown-db and the cutover checklist.
    delete_orphans_in_store = False

    FIELDS = [
        FieldSpec("name", "name", "name"),
        # status: never let an empty markdown value clear it.
        FieldSpec("status", "status", "status"),
        FieldSpec("description", "description", "description"),
    ]

    def __init__(self, store: Any = project_store, **kw: Any) -> None:
        super().__init__(store, **kw)

    # ── Markdown surface ────────────────────────────────────────────

    def _projects_root(self) -> Path:
        return projects_markdown_dir()

    def markdown_path_for(self, pk: str) -> Path:
        """``<vault>/<projects.markdown_dir>/<slug>.md`` — one flat dir."""
        return self._projects_root() / f"{pk}.md"

    def parse_all_from_markdown(self) -> dict[str, ParsedFileRow]:
        """Parse every ``<slug>.md`` in the project-notes directory.

        Files that do not parse as a project note (bad frontmatter, the
        slug not matching the filename) are skipped with a warning
        rather than failing the whole pass.
        """
        root = self._projects_root()
        out: dict[str, ParsedFileRow] = {}
        if not root.is_dir():
            return out
        for note in sorted(root.glob("*.md")):
            if not note.is_file():
                continue
            try:
                parsed = parse_project_note(note.read_text(encoding="utf-8"))
            except (ProjectNoteParseError, OSError) as exc:
                logger.warning(
                    "ProjectMarkdownDB: skipping %s — %s", note, exc,
                )
                continue
            if parsed.slug != note.stem:
                logger.warning(
                    "ProjectMarkdownDB: skipping %s — frontmatter slug %r "
                    "does not match filename", note, parsed.slug,
                )
                continue
            out[parsed.slug] = ParsedFileRow(
                pk=parsed.slug,
                fields={
                    "name": parsed.name,
                    "status": parsed.status,
                    "description": parsed.description,
                },
            )
        return out

    def write_entity_to_markdown(self, pk: str, fields: dict[str, Any]) -> None:
        """Render and atomically write ``pk``'s project note."""
        path = self.markdown_path_for(pk)
        content = render_project_note(
            slug=pk,
            name=fields.get("name"),
            status=fields.get("status") or "active",
            description=fields.get("description"),
        )
        atomic_write_text(path, content)

    # ── Store adapters ──────────────────────────────────────────────

    def _store_query(self) -> list[dict[str, Any]]:
        # Excludes status='deleted' projects by default — correct: a
        # deleted project should not resurrect a markdown note.
        return list(project_store.list_projects())

    def _store_create(self, pk: str, fields: dict[str, Any]) -> None:
        project_store.upsert_project(
            pk,
            name=fields.get("name"),
            status=fields.get("status") or "active",
            description=fields.get("description"),
            origin="vault",
            author="agent",
            change_summary="markdown_db: created from project note",
        )

    def _store_update(self, pk: str, fields: dict[str, Any]) -> None:
        kwargs: dict[str, Any] = {}
        for col in ("name", "status", "description"):
            if col in fields:
                kwargs[col] = fields[col]
        if not kwargs:
            return
        project_store.update_project(
            pk, author="agent",
            change_summary="markdown_db: drift reconciliation",
            **kwargs,
        )

    def _store_delete(self, pk: str) -> None:
        project_store.delete_project(pk, author="agent")


# ── Entry points (not yet wired to the gateway / cron) ──────────────


def reconcile_projects() -> dict[str, Any]:
    """Run a project drift reconciliation via :class:`ProjectMarkdownDB`."""
    return ProjectMarkdownDB().reconcile_drift().to_dict()


def materialize_projects(*, dry_run: bool = True) -> dict[str, Any]:
    """One-time store → markdown flip: write a note for every project
    that lacks one.

    Defaults to a dry run — pass ``dry_run=False`` to actually write
    files. Never overwrites an existing project note.
    """
    return ProjectMarkdownDB().materialize_from_store(dry_run=dry_run)
