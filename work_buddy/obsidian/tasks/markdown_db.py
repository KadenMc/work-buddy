"""``TaskMarkdownDB`` — the task subsystem expressed as a :class:`MarkdownDB`.

This is the first concrete subclass of the markdown_db abstraction. It
declares the six reconcilable task fields and reuses the existing,
battle-tested parsing helpers from ``obsidian/tasks/sync.py`` and
``obsidian/tasks/mutations.py`` rather than reimplementing them.

## The production task reconciler

``obsidian.tasks.sync.task_sync()`` delegates to :func:`reconcile_tasks`
in this module — so ``TaskMarkdownDB`` IS the task drift reconciler the
``task-sync`` cron and the dashboard Sync button run. ``task_sync``
remains as the stable entry-point name; its return shape (``status``
plus per-category counts) is preserved.

## The 8-loops → 6-FieldSpec collapse

The pre-existing task reconciler hand-wrote eight reconciliation loops.
Six are field-drift loops (checkbox / note_uuid / description / urgency
/ deadline / completed_at); they collapse into the :data:`FIELDS` list
below and run through the generic :meth:`MarkdownDB.reconcile_drift`.
The remaining two — orphan handling and the tag-cache rebuild — are the
base class's orphan logic and :meth:`TaskMarkdownDB.post_reconcile`.

## Coverage notes

- ``write_entity_to_markdown`` rewrites the description and checkbox of
  an existing line; it deliberately leaves the plugin emoji metadata
  (📅 / ✅ / ⏫🔼🔽) untouched. Reconciliation runs markdown→store, so the
  store-wins write-back path for emoji fields is not exercised; the task
  mutation capabilities (``update_task`` etc. in ``mutations.py``) own
  emoji-bearing task-line writes — and they already write both surfaces.
- The ``task_sync_status`` freshness write and the ``task_tags`` cache
  rebuild run in :meth:`post_reconcile`, after the field-drift loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.markdown_db import FieldSpec, MarkdownDB, WriteProvenance
from work_buddy.markdown_db.types import ParsedFileRow, ReconcileReport
from work_buddy.obsidian.tasks import store as task_store
from work_buddy.obsidian.tasks.mutations import (
    MASTER_TASK_FILE,
    replace_description_in_line,
)
# Imported as a module (not by-name) on purpose: `_read_master_list`,
# `_parse_file_tasks`, and `_rebuild_tag_cache` are referenced via this
# module so a `patch.object(sync, ...)` — or any future reassignment —
# is seen here too. A by-value `from sync import _read_master_list`
# would bind a private copy that monkeypatching could not reach.
from work_buddy.obsidian.tasks import sync as _tasks_sync

logger = get_logger(__name__)


def _checkbox_in_sync(file_state: Any, store_state: Any) -> bool:
    """The task checkbox is a lossy projection of the 5-valued ``state``.

    An unchecked box is consistent with any non-done state
    (``inbox`` / ``mit`` / ``focused`` / ``snoozed``); a checked box
    means ``done``. So the only thing that can *drift* is done-ness —
    exactly the comparison the legacy ``task_sync`` checkbox loop makes.
    """
    return (file_state == "done") == (store_state == "done")


class TaskMarkdownDB(MarkdownDB):
    """:class:`MarkdownDB` over the task master list ⇄ ``task_metadata``."""

    table_name = "task_metadata"
    pk_column = "task_id"

    FIELDS = [
        # Checkbox → state. parse_value maps the bool to a state string;
        # ``equivalent`` makes the comparison done-ness-only so a focused
        # task with an unchecked box is not wrongly downgraded to inbox.
        FieldSpec(
            name="checkbox",
            file_key="is_done",
            store_col="state",
            parse_value=lambda done: "done" if done else "inbox",
            propagate_on_falsy=True,
            equivalent=_checkbox_in_sync,
        ),
        FieldSpec("note_uuid", "note_uuid", "note_uuid"),
        FieldSpec("description", "description", "description"),
        FieldSpec("urgency", "urgency", "urgency"),
        # deadline_date carries has_deadline in lockstep — the legacy
        # loop set both columns together.
        FieldSpec(
            name="deadline",
            file_key="deadline_date",
            store_col="deadline_date",
            extra_store_fields=lambda v: {"has_deadline": bool(v)},
        ),
        FieldSpec("completed", "completed_at", "completed_at"),
    ]

    # ── Markdown surface ────────────────────────────────────────────

    def markdown_path_for(self, pk: str) -> Path:
        """The single master task list — same file for every task."""
        cfg = load_config()
        vault_root = cfg.get("vault_root", "")
        return Path(vault_root) / MASTER_TASK_FILE

    def markdown_exists(self, pk: str) -> bool:
        """Single master file: a task 'exists' in markdown iff it has a line."""
        return pk in self.parse_all_from_markdown()

    def parse_all_from_markdown(self) -> dict[str, ParsedFileRow]:
        """Parse every 🆔-bearing task line from the master list.

        Reuses :func:`obsidian.tasks.sync._read_master_list` (bridge or
        filesystem) and :func:`obsidian.tasks.sync._parse_file_tasks`.
        """
        content = _tasks_sync._read_master_list()
        if content is None:
            logger.warning("TaskMarkdownDB: master task list unreadable")
            return {}
        out: dict[str, ParsedFileRow] = {}
        for task_id, info in _tasks_sync._parse_file_tasks(content).items():
            out[task_id] = ParsedFileRow(
                pk=task_id,
                fields=dict(info),
                line_number=info.get("line_number"),
            )
        return out

    def write_entity_to_markdown(self, pk: str, fields: dict[str, Any]) -> None:
        """Rewrite ``pk``'s task line — description and checkbox only.

        Plugin emoji metadata (📅 / ✅ / urgency) is preserved as-is; see
        the module docstring for why emoji write-back is out of scope.
        """
        path = self.markdown_path_for(pk)
        if not path.exists():
            raise FileNotFoundError(f"master task list not found: {path}")
        lines = path.read_text(encoding="utf-8").splitlines()
        marker = f"🆔 {pk}"
        idx = next((i for i, ln in enumerate(lines) if marker in ln), None)
        if idx is None:
            raise KeyError(f"task line for {pk} not found in master list")

        line = lines[idx]
        if "description" in fields and fields["description"]:
            line = replace_description_in_line(line, str(fields["description"]))
        if "checkbox" in fields:
            want_done = fields["checkbox"] == "done"
            if want_done and line.lstrip().startswith("- [ ]"):
                line = line.replace("- [ ]", "- [x]", 1)
            elif not want_done and line.lstrip().startswith("- [x]"):
                line = line.replace("- [x]", "- [ ]", 1)
        lines[idx] = line

        from work_buddy.markdown_db.storage_helpers import atomic_write_text
        atomic_write_text(path, "\n".join(lines) + "\n")

    # ── Store adapters ──────────────────────────────────────────────

    def _store_query(self) -> list[dict[str, Any]]:
        return list(task_store.query(include_archived=False))

    def build_create_kwargs(self, parsed: ParsedFileRow) -> dict[str, Any]:
        """Mirror the orphan-create logic of the legacy ``task_sync``.

        ``urgency`` falls back to ``medium`` only when the line carries
        no priority emoji (parsed value is ``None``); ``completed_at`` is
        carried here but applied post-create by :meth:`_store_create`
        (it is not a ``store.create`` parameter).
        """
        f = parsed.fields
        return {
            "state": "done" if f.get("is_done") else "inbox",
            "urgency": f.get("urgency") or "medium",
            "note_uuid": f.get("note_uuid"),
            "description": f.get("description") or None,
            "has_deadline": bool(f.get("deadline_date")),
            "deadline_date": f.get("deadline_date"),
            "completed_at": f.get("completed_at"),
        }

    def _store_create(
        self,
        pk: str,
        fields: dict[str, Any],
        provenance: "WriteProvenance | None" = None,
    ) -> None:
        """Create a task_metadata row, then backfill ``completed_at``.

        ``completed_at`` is stamped by the store's state-transition
        logic, not accepted by ``create`` — so a line that arrives
        already-done with a ✅ date gets it via a post-create update,
        exactly as the legacy reconciler does.

        ``provenance`` is accepted for the :class:`MarkdownDB` hook
        contract; the task store records a free-text ``reason`` rather
        than an author enum, so it is not threaded further here.
        """
        fields = dict(fields)
        completed_at = fields.pop("completed_at", None)
        state = fields.get("state", "inbox")
        task_store.create(pk, **fields)
        if state == "done" and completed_at:
            task_store.update(
                pk,
                completed_at=completed_at,
                reason="markdown_db: completed_at backfilled at create",
            )

    def _store_update(
        self,
        pk: str,
        fields: dict[str, Any],
        provenance: "WriteProvenance | None" = None,
    ) -> None:
        task_store.update(pk, reason="markdown_db: drift reconciliation", **fields)

    def _store_delete(self, pk: str) -> None:
        task_store.delete(pk)

    # ── Post-reconcile derived state ────────────────────────────────

    def post_reconcile(
        self,
        parsed: dict[str, ParsedFileRow],
        store_rows: dict[str, Any],
        report: ReconcileReport,
    ) -> None:
        """Rebuild the task tag cache and write ``task_sync_status``.

        These are the two things the legacy ``task_sync`` did beyond the
        field-drift loop: the ``task_tags`` cache (keyed off the parsed
        line tags, classified into namespaces) and the single-row
        ``task_sync_status`` freshness audit the dashboard reads to
        render "synced Xm ago". Neither belongs to the :data:`FIELDS`
        model, so they run here.
        """
        # Tag cache — survivors are tasks present in the file that also
        # have (or just got) a store row.
        file_tasks = {pk: row.fields for pk, row in parsed.items()}
        surviving = set(parsed) & (set(store_rows) | set(report.created))
        try:
            self._last_tag_rows = _tasks_sync._rebuild_tag_cache(
                file_tasks, surviving,
            )
        except Exception as exc:
            logger.warning("TaskMarkdownDB: tag cache rebuild failed: %s", exc)
            self._last_tag_rows = 0

        # Freshness audit row. ``updated`` counts markdown-wins field
        # drifts (the writes that hit the store this pass).
        updated = sum(
            1
            for entries in report.drift.values()
            for d in entries
            if d.get("winner") == "markdown"
        )
        try:
            task_store.set_sync_status(
                created=len(report.created),
                updated=updated,
                deleted=len(report.deleted),
            )
        except Exception as exc:
            logger.warning("TaskMarkdownDB: set_sync_status failed: %s", exc)


def reconcile_tasks() -> dict[str, Any]:
    """Reconcile the master task list against the SQLite store.

    The task-reconciler entry point: runs :class:`TaskMarkdownDB`'s
    generic drift loop (plus the tag-cache / freshness post-pass) and
    returns a summary dict in the shape the legacy ``task_sync``
    produced — ``status`` + per-category counts — so the ``task_sync``
    capability and the dashboard's Sync button keep their contract.
    """
    db = TaskMarkdownDB(task_store)
    report = db.reconcile_drift()

    def _n(field: str) -> int:
        return len(report.drift.get(field, []))

    resolved = {
        "resolved_mismatches": _n("checkbox"),
        "resolved_note_uuids": _n("note_uuid"),
        "resolved_descriptions": _n("description"),
        "resolved_urgencies": _n("urgency"),
        "resolved_deadlines": _n("deadline"),
        "resolved_completed_at": _n("completed"),
    }
    total_actions = (
        len(report.created) + len(report.deleted) + sum(resolved.values())
    )
    return {
        "status": "ok" if total_actions == 0 else "synced",
        "created": len(report.created),
        "deleted": len(report.deleted),
        **resolved,
        "tag_rows_written": getattr(db, "_last_tag_rows", 0),
        "errors": report.errors,
        "warnings": report.warnings,
    }
