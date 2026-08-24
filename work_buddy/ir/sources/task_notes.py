"""Task-knowledge IR source with a frozen-legacy compatibility reader.

After the native authority epoch, item identity and freshness come from the
projection-free Co-work document head; no Markdown task file or vault path is
read or exposed.  The legacy branch remains read-only for pre-cutover use.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from work_buddy.ir.sources.base import Document, Projection, ProjectionSpec
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# First markdown H1, used as the title field when present
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


class TaskNoteSource:
    """IR adapter for task knowledge: live Co-work heads or legacy Markdown."""

    @property
    def name(self) -> str:
        return "task_note"

    def default_field_weights(self) -> dict[str, float]:
        # `line` is the authoritative native task description after cutover
        # (or the legacy task line before it). `title` is the document H1 and
        # `body` is the bulk knowledge content. BM25 spans all three.
        return {"line": 2.0, "title": 1.5, "body": 1.0}

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        # Two dense signals, mirroring the knowledge system's content+alias
        # split. They share no vector space and are RRF-fused alongside BM25:
        #
        # - ``line``  — short label-shaped canonical task text. Symmetric
        #   encoder (``leaf-mt``) because the query is also short/label-shaped.
        # - ``body``  — the note's full body. Asymmetric document encoder
        #   (``leaf-ir``) paired with ``leaf-ir-query`` at query time.
        return {
            "line": ProjectionSpec(kind="label"),
            "body": ProjectionSpec(kind="passage"),
        }

    # ------------------------------------------------------------------ discover

    def discover(self, days: int = 30, *, coverage: str = "active") -> list[tuple[str, float]]:
        """Return `(path, mtime)` for every task-linked note that exists on disk.

        `days` is accepted for protocol compatibility but ignored — task
        notes are long-lived and cheap to check (mtime lookup). The engine's
        `indexed_items` mtime skip handles "unchanged" efficiently.

        `coverage` controls which notes enter the corpus (keyword-only so the
        live IR's `discover(days=...)` is unaffected):
        - ``"active"`` (default): non-archived tasks only — the live IR's
          historical behavior, unchanged.
        - ``"all"``: include archived tasks' notes too, so retrospective
          search can find them. Whether archived notes are *shown* is then a
          query-time filter on the ``lifecycle_state`` metadata, not a
          build-time exclusion. See HISTORY-PARTITION-COVERAGE.md.
        """
        from work_buddy.tasks.runtime import native_authority_active

        if native_authority_active():
            from work_buddy.tasks.documents import TaskDocumentStoreManager
            from work_buddy.tasks.store import TaskStore
            from work_buddy.truth import documents, ydoc_store

            store = TaskStore()
            conn = store.connect()
            try:
                clauses = [
                    "l.note_uuid IS NOT NULL",
                    "l.lifecycle NOT IN ('retired', 'deleted')",
                    "t.deleted_at IS NULL",
                ]
                if coverage != "all":
                    clauses.append("t.archived_at IS NULL")
                rows = conn.execute(
                    "SELECT l.note_uuid, l.store_id, l.document_id "
                    "FROM task_document_links l "
                    "JOIN task_metadata t ON t.task_id = l.task_id "
                    f"WHERE {' AND '.join(clauses)}"
                ).fetchall()
            finally:
                conn.close()
            try:
                cowork_store = TaskDocumentStoreManager().open_existing()
            except Exception as exc:
                logger.warning("task_note source: Co-work store unavailable (%s)", exc)
                return []
            discovered: list[tuple[str, float]] = []
            for row in rows:
                try:
                    if cowork_store.store_id != row["store_id"]:
                        continue
                    document = documents.get_document(cowork_store, row["document_id"])
                    if document.ydoc_snapshot_sha256 is None:
                        fingerprint = document.content_sha256
                    else:
                        fingerprint = ydoc_store.current_structured_head(
                            cowork_store,
                            document_id=document.id,
                            snapshot_sha256=document.ydoc_snapshot_sha256,
                        )
                    # The IR protocol calls this value an mtime, but only
                    # equality/change semantics matter.  A 52-bit slice of
                    # the structured-head digest is exact as a Python float
                    # and changes for un-compacted editor updates too.
                    modified = float(int(fingerprint[:13], 16))
                except Exception as exc:
                    logger.warning(
                        "task_note source: document %s unavailable (%s)",
                        row["document_id"],
                        exc,
                    )
                    continue
                discovered.append((f"task_note:{row['note_uuid']}", modified))
            return discovered

        from work_buddy.config import load_config
        from work_buddy.obsidian.tasks import store as task_store
        from work_buddy.task_notes import get_task_note_adapter

        cfg = load_config()
        vault_root = cfg.get("vault_root")
        if not vault_root:
            logger.warning("task_note source: vault_root not configured")
            return []

        adapter = get_task_note_adapter(vault_root=vault_root)

        # Pull tasks with a note_uuid from the store. Default coverage excludes
        # archived (the working set); coverage="all" includes them for full
        # historical recall.
        where = "WHERE note_uuid IS NOT NULL"
        if coverage != "all":
            where += " AND archived_at IS NULL"
        conn = task_store.get_connection()
        try:
            rows = conn.execute(
                f"SELECT task_id, note_uuid FROM task_metadata {where}"
            ).fetchall()
        finally:
            conn.close()

        note_uuids = [str(row["note_uuid"]) for row in rows]
        descriptors = adapter.discover(note_uuids)
        results = [(item.item_id, item.modified_at) for item in descriptors]
        missing = len(note_uuids) - len(descriptors)

        if missing:
            logger.info(
                "task_note discover: %d notes referenced by tasks but missing on disk",
                missing,
            )
        return results

    # ------------------------------------------------------------------ lifecycle

    def lifecycle(self, item_ids: list[str]) -> dict[str, str]:
        """Map note path → current lifecycle state (open/done/archived/…).

        Optional, generic hook (no IR-engine dependency on it): a consolidated-
        index partition adapter calls this to (a) fold state into change
        detection so an archive/complete transition re-indexes the note even
        though its file mtime is unchanged, and (b) stamp a uniform
        ``lifecycle_state`` metadata key for query-time filtering. ``archived_at``
        being set wins over the raw ``state``. Items with no task row map to
        ``"unknown"``.
        """
        from work_buddy.tasks.runtime import native_authority_active

        if native_authority_active():
            from work_buddy.tasks.store import TaskStore

            conn = TaskStore().connect()
            try:
                rows = conn.execute(
                    "SELECT l.note_uuid, t.state, t.archived_at "
                    "FROM task_document_links l "
                    "JOIN task_metadata t ON t.task_id = l.task_id"
                ).fetchall()
            finally:
                conn.close()
            by_uuid = {
                str(row["note_uuid"]): (
                    "archived" if row["archived_at"] else (row["state"] or "open")
                )
                for row in rows
            }
            return {
                item_id: by_uuid.get(item_id.removeprefix("task_note:"), "unknown")
                for item_id in item_ids
            }

        from work_buddy.obsidian.tasks import store as task_store

        conn = task_store.get_connection()
        try:
            rows = conn.execute(
                """SELECT note_uuid, state, archived_at FROM task_metadata
                   WHERE note_uuid IS NOT NULL"""
            ).fetchall()
        finally:
            conn.close()
        by_uuid = {
            r["note_uuid"]: ("archived" if r["archived_at"] else (r["state"] or "open"))
            for r in rows
        }
        return {iid: by_uuid.get(Path(iid).stem, "unknown") for iid in item_ids}

    # ------------------------------------------------------------------ parse

    def parse(self, item_id: str) -> list[Document]:
        """Parse one task knowledge item into a single IR Document."""
        from work_buddy.tasks.runtime import native_authority_active

        if native_authority_active():
            return self._parse_native(item_id)

        from work_buddy.config import load_config
        from work_buddy.obsidian.tasks import store as task_store

        from work_buddy.task_notes import get_task_note_adapter, validate_note_uuid

        path = Path(item_id)
        note_uuid = validate_note_uuid(path.stem)
        try:
            # Discover keeps the historical absolute-path item identity.  Use
            # that identity's vault root so parse cannot drift to a different
            # configured vault between the two phases.
            vault_root = path.parents[2]
            raw = get_task_note_adapter(vault_root=vault_root).read(
                note_uuid,
                filesystem_fallback=True,
            )
        except Exception as exc:
            logger.warning("task_note parse: could not read %s: %s", path, exc)
            return []
        if raw is None:
            return []

        # Strip YAML frontmatter if present
        body = raw
        if raw.startswith("---\n"):
            end = raw.find("\n---", 4)
            if end != -1:
                body = raw[end + 4 :].lstrip("\n")

        # Title: first H1 if present, else filename stem
        h1_match = _H1_RE.search(body)
        title = h1_match.group(1).strip() if h1_match else path.stem

        cfg = load_config()
        max_dense = cfg.get("ir", {}).get("dense_text_max_chars", 1500)

        # Map note_uuid → task_id + canonical task-line text so hits can
        # link back to the task AND so the ``line`` projection uses the
        # authoritative text (not the note's H1, which can drift).
        task_id: str | None = None
        task_state: str | None = None
        conn = task_store.get_connection()
        try:
            row = conn.execute(
                "SELECT task_id, state FROM task_metadata WHERE note_uuid = ? LIMIT 1",
                (note_uuid,),
            ).fetchone()
            if row:
                task_id = row["task_id"]
                task_state = row["state"]
        finally:
            conn.close()

        # Authoritative task-line text, if available, for both the ``line``
        # BM25 field and the ``line`` projection. Falls back to the note's
        # H1 if the task store or master list doesn't resolve — keeps the
        # indexer resilient to partial data.
        task_line = _lookup_task_line_text(task_id) if task_id else None
        line_text = task_line or title

        # body projection text: the note body, capped. Dense encoder
        # gets a bounded passage; BM25 can still match anywhere in the
        # fuller ``body`` field.
        body_text = body.strip()[:max_dense] if body.strip() else ""

        # display_text: first non-empty body line (skipping the H1), capped
        display = ""
        for line in body.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            display = stripped[:200]
            break
        if not display:
            display = line_text[:200]

        # dense_text is kept populated for back-compat / diagnostic reads,
        # but it's no longer the primary encoding input — projections take
        # over. Building both means no regression for any tool that reads
        # the legacy column.
        dense_text = f"{line_text}\n{body_text}"[:max_dense]

        doc = Document(
            doc_id=f"task_note:{note_uuid}",
            source="task_note",
            fields={
                "line": line_text,
                "title": title,
                "body": body[:20000],
            },
            dense_text=dense_text,
            display_text=display,
            metadata={
                "note_uuid": note_uuid,
                "task_id": task_id,
                "task_state": task_state,
                "file_path": str(path),
                "indexed_at": time.time(),
            },
            projections={
                "line": Projection(text=line_text),
                "body": Projection(text=body_text) if body_text else Projection(text=line_text),
            },
        )
        return [doc]

    def _parse_native(self, item_id: str) -> list[Document]:
        """Read a projection-free Co-work head by stable task/document IDs."""

        from work_buddy.config import load_config
        from work_buddy.tasks.documents import (
            TaskDocumentStoreManager,
            project_live_markdown,
        )
        from work_buddy.tasks.store import TaskStore

        if not item_id.startswith("task_note:"):
            logger.warning("task_note parse: invalid native item id %r", item_id)
            return []
        note_uuid = item_id.removeprefix("task_note:")
        task_store = TaskStore()
        conn = task_store.connect()
        try:
            row = conn.execute(
                "SELECT t.task_id, t.description, t.state, l.store_id, "
                "l.document_id FROM task_document_links l "
                "JOIN task_metadata t ON t.task_id = l.task_id "
                "WHERE l.note_uuid = ? AND t.deleted_at IS NULL "
                "AND l.lifecycle NOT IN ('retired', 'deleted') LIMIT 1",
                (note_uuid,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return []
        try:
            cowork_store = TaskDocumentStoreManager().open_existing()
            if cowork_store.store_id != row["store_id"]:
                return []
            raw = project_live_markdown(cowork_store, row["document_id"])
        except Exception as exc:
            logger.warning("task_note parse: Co-work document unavailable: %s", exc)
            return []

        body = raw
        h1_match = _H1_RE.search(body)
        task_line = str(row["description"] or "").strip()
        title = h1_match.group(1).strip() if h1_match else task_line or note_uuid
        line_text = task_line or title
        max_dense = load_config().get("ir", {}).get("dense_text_max_chars", 1500)
        body_text = body.strip()[:max_dense] if body.strip() else ""
        display = next(
            (
                line.strip()[:200]
                for line in body.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ),
            line_text[:200],
        )
        return [
            Document(
                doc_id=f"task_note:{note_uuid}",
                source="task_note",
                fields={"line": line_text, "title": title, "body": body[:20000]},
                dense_text=f"{line_text}\n{body_text}"[:max_dense],
                display_text=display,
                metadata={
                    "note_uuid": note_uuid,
                    "task_id": str(row["task_id"]),
                    "task_state": str(row["state"]),
                    "store_id": str(row["store_id"]),
                    "document_id": str(row["document_id"]),
                    "indexed_at": time.time(),
                },
                projections={
                    "line": Projection(text=line_text),
                    "body": Projection(text=body_text)
                    if body_text
                    else Projection(text=line_text),
                },
            )
        ]


def _lookup_task_line_text(task_id: str) -> str | None:
    """Return the canonical task-line description for a task_id.

    Pulls from the Obsidian Tasks plugin cache when available, falling back
    to a direct scan of the master task list. Returns None if the task
    isn't resolvable — the caller should default to the note's H1.
    """
    from work_buddy.tasks.runtime import native_authority_active

    if native_authority_active():
        from work_buddy.tasks.store import TaskStore

        task = TaskStore().get(task_id, include_deleted=True)
        return None if task is None else task.description

    try:
        from work_buddy.obsidian.tasks.env import verify_task
        info = verify_task(task_id=task_id)
        if info.get("found"):
            desc = (info.get("description") or "").strip()
            if desc:
                return desc
    except Exception:
        pass  # Plugin or bridge unavailable — fall through

    # Fallback: scan the master list directly
    try:
        from work_buddy.config import load_config
        from work_buddy.obsidian.tasks.mutations import (
            MASTER_TASK_FILE, _find_task_line,
        )
        vault_root = load_config().get("vault_root")
        if not vault_root:
            return None
        master = Path(vault_root) / MASTER_TASK_FILE
        if not master.exists():
            return None
        lines = master.read_text(encoding="utf-8").split("\n")
        found = _find_task_line(lines, task_id=task_id)
        if not found:
            return None
        _, line = found
        # Strip checkbox + tags + wikilinks + plugin emojis, matching the
        # cleanup that assign_task does inline.
        desc = re.sub(r"^- \[.\]\s*", "", line)
        desc = re.sub(r"#\S+", "", desc)
        desc = re.sub(r"\[\[[^\]]+\]\]", "", desc)
        desc = re.sub(r"[🆔📅✅🔼⏫]\s*\S*", "", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        return desc or None
    except Exception:
        return None
