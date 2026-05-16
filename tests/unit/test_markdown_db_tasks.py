"""Parity tests: TaskMarkdownDB.reconcile_drift vs the legacy task_sync.

The legacy reconciler ``obsidian.tasks.sync.task_sync`` and the new
``TaskMarkdownDB.reconcile_drift`` must produce *identical* mutations to
the ``task_metadata`` store for the same inputs. These tests run both
against the same temp master-task-list + a fresh temp DB and assert the
resulting store state matches column-for-column.

If these pass, the cutover (repointing the cron + capability at
``TaskMarkdownDB``) is a behaviour-preserving change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

# Columns that both reconcilers are responsible for. task_sync also
# rebuilds the tag cache + writes task_sync_status; reconcile_drift does
# not (those are a post_reconcile hook). Parity is asserted on the
# task_metadata columns only.
_COMPARED = (
    "task_id", "state", "urgency", "description", "note_uuid",
    "deadline_date", "has_deadline", "completed_at",
)

# A master task list exercising every reconciliation path at once:
#   t-aaa1  orphan-in-file, not done
#   t-aaa2  orphan-in-file, done with ✅ date
#   t-bbb1  checkbox drift  (file done, store inbox)
#   t-bbb2  checkbox NON-drift (file unchecked, store 'focused' — must stay)
#   t-ccc1  description drift
#   t-ccc2  urgency drift (file ⏫, store medium)
#   t-ccc3  deadline drift (file 📅, store none)
#   t-ddd1  fully in sync — no change expected
# plus a DB-seeded t-zzz9 with NO file line → orphan-in-store → soft-delete
_MASTER = """\
# Master Task List

- [ ] #todo Brand new unstarted task #projects/work-buddy 🆔 t-aaa1
- [x] #todo Brand new done task ✅ 2026-05-10 🆔 t-aaa2
- [x] #todo Checkbox should flip to done 🆔 t-bbb1
- [ ] #todo Unchecked but focused in store 🆔 t-bbb2
- [ ] #todo Description edited in obsidian 🆔 t-ccc1
- [ ] #todo Urgency bumped ⏫ 🆔 t-ccc2
- [ ] #todo Deadline added 📅 2026-06-01 🆔 t-ccc3
- [ ] #todo Already perfectly synced 🆔 t-ddd1
"""


def _seed_store(store: Any) -> None:
    """Pre-seed the DB with rows for the drift / orphan-in-store cases."""
    # checkbox drift: store says inbox, file says done
    store.create("t-bbb1", state="inbox", urgency="medium",
                 description="Checkbox should flip to done")
    # checkbox NON-drift: store 'focused', file unchecked → must stay focused
    store.create("t-bbb2", state="focused", urgency="medium",
                 description="Unchecked but focused in store")
    # description drift
    store.create("t-ccc1", state="inbox", urgency="medium",
                 description="STALE description in store")
    # urgency drift
    store.create("t-ccc2", state="inbox", urgency="medium",
                 description="Urgency bumped")
    # deadline drift
    store.create("t-ccc3", state="inbox", urgency="medium",
                 description="Deadline added")
    # fully synced
    store.create("t-ddd1", state="inbox", urgency="medium",
                 description="Already perfectly synced")
    # orphan in store — no file line
    store.create("t-zzz9", state="inbox", urgency="low",
                 description="Ghost task only in store")


@pytest.fixture
def task_env(tmp_path, monkeypatch):
    """A temp vault + temp task DB, with config + bridge redirected.

    Yields a callable ``run(reconciler)`` that resets the DB to the
    seeded baseline, executes ``reconciler()``, and returns the
    resulting store snapshot (a list of dicts, ``_COMPARED`` columns,
    sorted by task_id, including soft-deleted rows).
    """
    vault = tmp_path / "vault"
    (vault / "tasks").mkdir(parents=True)
    (vault / "tasks" / "master-task-list.md").write_text(
        _MASTER, encoding="utf-8",
    )
    db_path = tmp_path / "task_metadata.db"

    fake_cfg = {
        "vault_root": str(vault),
        "tasks": {"db_path": str(db_path), "namespace_threshold": 2},
        "git": {"detail_days": 7},
    }

    # Redirect load_config in every module that reads it on this path.
    from work_buddy.obsidian.tasks import store as task_store
    from work_buddy.obsidian.tasks import sync as task_sync_mod
    from work_buddy.obsidian.tasks import markdown_db as md_mod

    monkeypatch.setattr(task_store, "load_config", lambda *a, **k: fake_cfg)
    monkeypatch.setattr(task_sync_mod, "load_config", lambda *a, **k: fake_cfg)
    monkeypatch.setattr(md_mod, "load_config", lambda *a, **k: fake_cfg)

    # Force the filesystem read path (no Obsidian bridge in tests).
    import work_buddy.obsidian.bridge as bridge
    monkeypatch.setattr(bridge, "is_available", lambda: False)

    def _reset_db() -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        _seed_store(task_store)

    def _snapshot() -> list[dict[str, Any]]:
        rows = task_store.query(include_archived=True, include_deleted=True)
        out = []
        for r in rows:
            snap = {k: r.get(k) for k in _COMPARED}
            # completed_at is auto-stamped to "now" by the store when a
            # task transitions to done — the two reconcilers run at
            # slightly different instants, so a full ISO timestamp would
            # differ on wall-clock seconds alone. Real ✅-date values
            # ('YYYY-MM-DD', 10 chars) still compare exactly; auto-stamps
            # (which contain a 'T') collapse to a sentinel.
            ca = snap.get("completed_at")
            if isinstance(ca, str) and "T" in ca:
                snap["completed_at"] = "<auto-stamped>"
            snap["_deleted"] = r.get("deleted_at") is not None
            out.append(snap)
        return sorted(out, key=lambda d: d["task_id"])

    def run(reconciler: Callable[[], Any]) -> list[dict[str, Any]]:
        _reset_db()
        reconciler()
        return _snapshot()

    return run


def test_parity_full_scenario(task_env):
    """Every reconciliation path, both reconcilers, identical store state."""
    from work_buddy.obsidian.tasks.markdown_db import TaskMarkdownDB
    from work_buddy.obsidian.tasks import store as task_store
    from work_buddy.obsidian.tasks.sync import task_sync

    legacy = task_env(task_sync)
    new = task_env(lambda: TaskMarkdownDB(task_store).reconcile_drift())

    assert legacy == new, (
        "TaskMarkdownDB.reconcile_drift diverged from task_sync.\n"
        f"legacy={legacy}\nnew={new}"
    )


def test_new_reconciler_specific_outcomes(task_env):
    """Spot-check the new reconciler's absolute outcomes (not just parity)."""
    from work_buddy.obsidian.tasks.markdown_db import TaskMarkdownDB
    from work_buddy.obsidian.tasks import store as task_store

    snap = task_env(lambda: TaskMarkdownDB(task_store).reconcile_drift())
    by_id = {r["task_id"]: r for r in snap}

    # Orphan-in-file created.
    assert by_id["t-aaa1"]["state"] == "inbox"
    assert by_id["t-aaa2"]["state"] == "done"
    assert by_id["t-aaa2"]["completed_at"] == "2026-05-10"
    # Checkbox drift flipped to done.
    assert by_id["t-bbb1"]["state"] == "done"
    # Checkbox NON-drift: focused stays focused (not downgraded to inbox).
    assert by_id["t-bbb2"]["state"] == "focused"
    # Description drift reconciled from the file.
    assert by_id["t-ccc1"]["description"] == "Description edited in obsidian"
    # Urgency drift: ⏫ → high.
    assert by_id["t-ccc2"]["urgency"] == "high"
    # Deadline drift: 📅 date + has_deadline lockstep.
    assert by_id["t-ccc3"]["deadline_date"] == "2026-06-01"
    assert by_id["t-ccc3"]["has_deadline"] in (1, True)
    # In-sync task untouched.
    assert by_id["t-ddd1"]["description"] == "Already perfectly synced"
    # Orphan-in-store soft-deleted.
    assert by_id["t-zzz9"]["_deleted"] is True


def test_idempotent_second_pass(task_env):
    """A second reconcile pass over already-synced state changes nothing."""
    from work_buddy.obsidian.tasks.markdown_db import TaskMarkdownDB
    from work_buddy.obsidian.tasks import store as task_store

    def _twice() -> None:
        TaskMarkdownDB(task_store).reconcile_drift()
        report = TaskMarkdownDB(task_store).reconcile_drift()
        # Second pass must be a clean no-op.
        assert not report.changed, f"second pass not idempotent: {report.to_dict()}"

    task_env(_twice)
