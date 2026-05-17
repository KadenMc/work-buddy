"""``TaskMarkdownDB`` — the task subsystem expressed as a :class:`MarkdownDB`.

This is the first concrete subclass of the markdown_db abstraction. It
declares the six reconcilable task fields and reuses the existing,
battle-tested parsing helpers from ``obsidian/tasks/sync.py`` and
``obsidian/tasks/mutations.py`` rather than reimplementing them.

## Status: parallel, not yet the cutover

The legacy reconciler ``obsidian.tasks.sync.task_sync()`` is still the
production drift job. ``TaskMarkdownDB`` is an *additive* parallel
implementation: its :meth:`~MarkdownDB.reconcile_drift` produces the
same store mutations as ``task_sync`` (verified by
``tests/unit/test_markdown_db_tasks.py``). Repointing the ``task_sync``
cron + capability at this class is a deliberate, reviewed cutover step —
intentionally NOT done automatically.

## The 8-loops → 6-FieldSpec collapse

``task_sync`` hand-writes eight reconciliation loops. Six of them are
field-drift loops (checkbox / note_uuid / description / urgency /
deadline / completed_at); they collapse into the :data:`FIELDS` list
below and run through the generic
:meth:`MarkdownDB.reconcile_drift`. The remaining two — orphan handling
and the tag-cache rebuild — are the base class's orphan logic and a
subclass hook respectively.

## What is NOT yet covered here

- ``write_entity_to_markdown`` rewrites the description and checkbox of
  an existing line; it deliberately leaves the plugin emoji metadata
  (📅 / ✅ / ⏫🔼🔽) untouched. Reconciliation runs markdown→store, so the
  store-wins write-back path for emoji fields is not exercised; the task
  mutation capabilities (``update_task`` etc. in ``mutations.py``) own
  emoji-bearing task-line writes.
- The ``task_sync_status`` freshness write and the ``task_tags`` cache
  rebuild are not performed by ``reconcile_drift`` — ``task_sync`` still
  owns them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.markdown_db import FieldSpec, MarkdownDB
from work_buddy.markdown_db.types import ParsedFileRow
from work_buddy.obsidian.tasks import store as task_store
from work_buddy.obsidian.tasks.mutations import (
    MASTER_TASK_FILE,
    replace_description_in_line,
)
from work_buddy.obsidian.tasks.sync import _parse_file_tasks, _read_master_list

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
        content = _read_master_list()
        if content is None:
            logger.warning("TaskMarkdownDB: master task list unreadable")
            return {}
        out: dict[str, ParsedFileRow] = {}
        for task_id, info in _parse_file_tasks(content).items():
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

    def _store_create(self, pk: str, fields: dict[str, Any]) -> None:
        """Create a task_metadata row, then backfill ``completed_at``.

        ``completed_at`` is stamped by the store's state-transition
        logic, not accepted by ``create`` — so a line that arrives
        already-done with a ✅ date gets it via a post-create update,
        exactly as the legacy reconciler does.
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

    def _store_update(self, pk: str, fields: dict[str, Any]) -> None:
        task_store.update(pk, reason="markdown_db: drift reconciliation", **fields)

    def _store_delete(self, pk: str) -> None:
        task_store.delete(pk)


def reconcile_tasks() -> dict[str, Any]:
    """Run a task drift reconciliation via :class:`TaskMarkdownDB`.

    Convenience entry point mirroring ``obsidian.tasks.sync.task_sync``'s
    return shape closely enough for inspection. NOT yet wired into the
    cron / capability registry — see the module docstring.
    """
    db = TaskMarkdownDB(task_store)
    report = db.reconcile_drift()
    return report.to_dict()
