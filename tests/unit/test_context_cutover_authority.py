from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _markdown(section) -> str:
    return str(section.items[0]["markdown"])


def test_obsidian_opt_out_preserves_native_journal_and_tasks_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.collectors import obsidian_collector
    from work_buddy.context import ContextCollector, ContextRequest
    from work_buddy.context import cache as cache_mod
    from work_buddy.tasks import runtime
    from work_buddy.tasks.store import TaskStore

    vault = tmp_path / "operator" / "private-vault"
    cfg = {
        "vault_root": str(vault),
        "obsidian": {"journal_dir": "journal", "journal_days": 1, "wellness_days": 1},
        "tasks": {"event_lookback_hours": 1},
    }
    monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: False if component_id == "obsidian" else None,
    )
    monkeypatch.setattr(obsidian_collector, "_native_journal_authority", lambda _cfg: True)
    monkeypatch.setattr(runtime, "native_authority_active", lambda *_args: True)
    monkeypatch.setattr(
        "work_buddy.journal_capture.native_ops.journal_state",
        lambda target=None, create_on_read=False: {
            "target_date": target or "2026-08-27",
            "exists": True,
            "items": [{"item_kind": "running_note", "value": "Native note"}],
            "fields": [{"field_id": "energy", "value": 8, "disposition": None}],
        },
    )
    monkeypatch.setattr(
        "work_buddy.journal_capture.native_ops.day_planner",
        lambda **_kwargs: {
            "target_date": "2026-08-27",
            "entries": [],
            "unscheduled": [],
            "authority": "journal_sqlite",
        },
    )
    monkeypatch.setattr(
        TaskStore,
        "list",
        lambda self, query: [SimpleNamespace(description="Native task")],
    )
    native_event_query = threading.Event()

    class _NativeConnection:
        def execute(self, sql, params):
            assert "task_state_history" in sql
            native_event_query.set()
            return SimpleNamespace(fetchall=lambda: [])

        def close(self):
            return None

    monkeypatch.setattr(TaskStore, "connect_readonly", lambda self: _NativeConnection())

    forbidden = {
        name: MagicMock(side_effect=AssertionError(f"legacy branch called: {name}"))
        for name in (
            "_get_journal_entries",
            "_get_journal_stats",
            "_get_recent_files",
            "_sealed_legacy_roots",
            "_parse_wellness",
        )
    }
    for name, probe in forbidden.items():
        monkeypatch.setattr(obsidian_collector, name, probe)
    bridge = MagicMock(side_effect=AssertionError("legacy bridge probed"))
    monkeypatch.setattr("work_buddy.obsidian.bridge.is_available", bridge)
    legacy_events = MagicMock(side_effect=AssertionError("legacy task store called"))
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.store.get_events_in_range",
        legacy_events,
    )

    sources = ["obsidian", "obsidian_tasks", "obsidian_wellness", "day_planner", "datacore"]
    result = ContextCollector().collect(
        ContextRequest(
            sources=sources,
            custom={name: cfg for name in sources},
            max_age_seconds=None,
        )
    )

    assert set(result.sections) == {
        "obsidian",
        "obsidian_tasks",
        "obsidian_wellness",
        "day_planner",
    }
    assert all(
        section.items for section in result.sections.values()
    ), {name: section.metadata for name, section in result.sections.items()}
    rendered = "\n".join(_markdown(result.sections[name]) for name in result.sections)
    assert "# Journal Summary" in rendered
    assert "native Journal SQLite" in rendered
    assert "Native note" in rendered
    assert "Native task" in rendered
    assert str(vault) not in rendered
    assert native_event_query.is_set()
    assert all(probe.call_count == 0 for probe in forbidden.values())
    bridge.assert_not_called()
    legacy_events.assert_not_called()


def test_task_preseal_cache_is_forced_stale_by_real_authority_latch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.context import ContextCollector, ContextRequest
    from work_buddy.context import cache as cache_mod
    from work_buddy.tasks import runtime
    from work_buddy.tasks import store as task_store_module
    from work_buddy.tasks.store import TaskStore

    database = tmp_path / "data" / "db" / "tasks.db"
    latch = tmp_path / "installation" / "task_authority_latch.json"
    monkeypatch.setattr(task_store_module, "default_task_db_path", lambda: database)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: database)
    monkeypatch.setattr(runtime, "_canonical_default_latch_path", lambda: latch)
    monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: True if component_id == "obsidian" else None,
    )
    frozen_events = MagicMock(side_effect=AssertionError("frozen task store called"))
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.store.get_events_in_range",
        frozen_events,
    )
    store = TaskStore(database)
    store.initialize()
    vault = tmp_path / "vault"
    master = vault / "tasks" / "master-task-list.md"
    master.parent.mkdir(parents=True)
    master.write_text("- [ ] Legacy cached task\n", encoding="utf-8")
    cfg = {"vault_root": str(vault), "tasks": {"event_lookback_hours": 1}}
    request = ContextRequest(
        sources=["obsidian_tasks"],
        custom={"obsidian_tasks": cfg},
        max_age_seconds=0,
    )

    before = ContextCollector().collect(request).sections["obsidian_tasks"]
    assert "Legacy cached task" in _markdown(before)
    assert "pre-cutover" in _markdown(before)

    runtime.arm_native_authority_latch(
        database,
        cohort_id="context-cache-cutover",
        target_authority_epoch="native:context-cache",
        cutover_receipt_id="context-cache-receipt",
        armed_at="2026-08-27T20:00:00+00:00",
    )
    current = store.system_state()
    store.set_system_state(
        expected_authority_epoch=current.authority_epoch,
        authority_epoch="native:context-cache",
        updated_at="2026-08-27T20:00:01+00:00",
        cutover_receipt_id="context-cache-receipt",
    )

    after = ContextCollector().collect(request).sections["obsidian_tasks"]
    assert "native task store" in _markdown(after)
    assert "Legacy cached task" not in _markdown(after)
    frozen_events.assert_not_called()


@pytest.mark.parametrize(
    ("case", "source_name"),
    [
        ("journal", "obsidian"),
        ("recent", "obsidian"),
        ("wellness", "obsidian_wellness"),
        ("day_planner", "day_planner"),
    ],
)
def test_legacy_journal_context_snapshot_and_cache_publish_block_real_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    source_name: str,
) -> None:
    from work_buddy.collectors import obsidian_collector
    from work_buddy.context import ContextCollector, ContextRequest
    from work_buddy.context import cache as cache_mod
    from work_buddy.context import registry
    from work_buddy.journal_capture.store import JournalCaptureStore

    database = tmp_path / "journal_authority_fence.db"
    JournalCaptureStore(database)
    vault = tmp_path / "vault"
    journal = vault / "journal"
    journal.mkdir(parents=True)
    cfg = {
        "vault_root": str(vault),
        "obsidian": {
            "journal_dir": "journal",
            "journal_days": 1,
            "wellness_days": 1,
            "recent_modified_days": 1,
        },
    }
    monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: True if component_id == "obsidian" else None,
    )

    def native_authority(_cfg) -> bool:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT mode FROM journal_authority_control WHERE singleton=1"
            ).fetchone()
        return bool(row and row[0] != "legacy_compatibility")

    monkeypatch.setattr(obsidian_collector, "_native_journal_authority", native_authority)
    monkeypatch.setattr(obsidian_collector, "_sealed_legacy_roots", lambda _cfg: ())
    monkeypatch.setattr(obsidian_collector, "legacy_task_read_guard", nullcontext)
    monkeypatch.setattr(obsidian_collector, "_get_tasks", lambda _root: "")
    monkeypatch.setattr(obsidian_collector, "_get_task_events", lambda *_a, **_k: [])
    monkeypatch.setattr(obsidian_collector, "_get_journal_stats", lambda *_a, **_k: None)

    read_started = threading.Event()
    release_read = threading.Event()
    cache_published = threading.Event()
    seal_attempted = threading.Event()
    seal_finished = threading.Event()
    failures: list[BaseException] = []
    results = []

    def block(value):
        read_started.set()
        if not release_read.wait(5.0):
            raise AssertionError("test did not release legacy context read")
        return value

    if case == "journal":
        monkeypatch.setattr(
            obsidian_collector,
            "_get_journal_entries",
            lambda *_a, **_k: block([
                {
                    "date": "2026-08-27",
                    "header": "Journal for 2026-08-27 (Thursday)",
                    "sections": {"Log": "legacy journal snapshot"},
                }
            ]),
        )
        monkeypatch.setattr(obsidian_collector, "_get_recent_files", lambda *_a, **_k: [])
    elif case == "recent":
        monkeypatch.setattr(obsidian_collector, "_get_journal_entries", lambda *_a, **_k: [])
        monkeypatch.setattr(
            obsidian_collector,
            "_get_recent_files",
            lambda *_a, **_k: block([
                {"path": "notes/legacy.md", "modified": "2026-08-27 12:00"}
            ]),
        )
    elif case == "wellness":
        monkeypatch.setattr(
            obsidian_collector,
            "_parse_wellness",
            lambda *_a, **_k: block([
                {"date": "2026-08-27", "sleep": 7.0, "energy": 6.0, "mood": 8.0}
            ]),
        )
    else:
        monkeypatch.setattr("work_buddy.obsidian.bridge.is_available", lambda: True)
        monkeypatch.setattr("work_buddy.obsidian.day_planner.check_ready", lambda: {"ready": True})
        monkeypatch.setattr(
            "work_buddy.obsidian.day_planner.get_todays_plan",
            lambda _path: block({"found": True, "entries": [], "unscheduled": []}),
        )

    original_write = cache_mod.write_cached

    def observed_write(section, bucket):
        result = original_write(section, bucket)
        cache_published.set()
        return result

    monkeypatch.setattr(cache_mod, "write_cached", observed_write)
    request = ContextRequest(
        sources=[source_name],
        custom={source_name: cfg},
        max_age_seconds=None,
    )

    def collect() -> None:
        try:
            results.append(ContextCollector().collect(request))
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            failures.append(exc)

    def seal() -> None:
        try:
            seal_attempted.set()
            with sqlite3.connect(database, timeout=5.0) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE journal_authority_control SET mode='database_only' "
                    "WHERE singleton=1"
                )
                connection.commit()
            seal_finished.set()
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            failures.append(exc)

    collect_thread = threading.Thread(target=collect, daemon=True)
    seal_thread = threading.Thread(target=seal, daemon=True)
    collect_thread.start()
    assert read_started.wait(2.0)
    seal_thread.start()
    assert seal_attempted.wait(2.0)
    assert not seal_finished.wait(0.15)
    release_read.set()
    collect_thread.join(5.0)
    seal_thread.join(5.0)

    assert failures == []
    assert not collect_thread.is_alive()
    assert not seal_thread.is_alive()
    assert results and source_name in results[0].sections
    assert cache_published.is_set()
    assert seal_finished.is_set()
    cached = cache_mod.read_cached(source_name, cache_mod.bucket_key(source_name, request))
    assert cached is not None
    assert registry.get(source_name).is_stale(cached, request) is True


def test_task_context_snapshot_blocks_real_latch_activation_and_cache_replays_native(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.context import ContextCollector, ContextRequest
    from work_buddy.context import cache as cache_mod
    from work_buddy.tasks import runtime
    from work_buddy.tasks import store as task_store_module
    from work_buddy.tasks.store import TaskStore

    database = tmp_path / "data" / "db" / "tasks.db"
    latch = tmp_path / "installation" / "task_authority_latch.json"
    monkeypatch.setattr(task_store_module, "default_task_db_path", lambda: database)
    monkeypatch.setattr(runtime, "default_task_db_path", lambda: database)
    monkeypatch.setattr(runtime, "_canonical_default_latch_path", lambda: latch)
    monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        "work_buddy.health.preferences.is_wanted",
        lambda component_id: True if component_id == "obsidian" else None,
    )
    store = TaskStore(database)
    store.initialize()
    vault = tmp_path / "vault"
    master = vault / "tasks" / "master-task-list.md"
    master.parent.mkdir(parents=True)
    master.write_text("- [ ] Racing legacy task\n", encoding="utf-8")
    request = ContextRequest(
        sources=["obsidian_tasks"],
        custom={"obsidian_tasks": {"vault_root": str(vault)}},
        max_age_seconds=None,
    )
    original_read_text = Path.read_text
    read_started = threading.Event()
    release_read = threading.Event()
    seal_attempted = threading.Event()
    seal_finished = threading.Event()
    failures: list[BaseException] = []
    results = []

    def blocking_read_text(path: Path, *args, **kwargs):
        if path == master:
            read_started.set()
            if not release_read.wait(5.0):
                raise AssertionError("test did not release task snapshot")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocking_read_text)

    def collect() -> None:
        try:
            results.append(ContextCollector().collect(request))
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            failures.append(exc)

    def seal() -> None:
        try:
            seal_attempted.set()
            with store.transaction() as connection:
                runtime.arm_native_authority_latch(
                    database,
                    cohort_id="context-race",
                    target_authority_epoch="native:context-race",
                    cutover_receipt_id="context-race-receipt",
                    armed_at="2026-08-27T20:30:00+00:00",
                )
                connection.execute(
                    "UPDATE task_system_state SET authority_epoch=?, "
                    "cutover_receipt_id=?, updated_at=? WHERE id=1",
                    (
                        "native:context-race",
                        "context-race-receipt",
                        "2026-08-27T20:30:01+00:00",
                    ),
                )
            seal_finished.set()
        except BaseException as exc:  # pragma: no cover - diagnostic handoff
            failures.append(exc)

    collect_thread = threading.Thread(target=collect, daemon=True)
    seal_thread = threading.Thread(target=seal, daemon=True)
    collect_thread.start()
    assert read_started.wait(2.0)
    seal_thread.start()
    assert seal_attempted.wait(2.0)
    assert not seal_finished.wait(0.15)
    release_read.set()
    collect_thread.join(5.0)
    seal_thread.join(5.0)

    assert failures == []
    assert not collect_thread.is_alive()
    assert not seal_thread.is_alive()
    assert results and "Racing legacy task" in _markdown(
        results[0].sections["obsidian_tasks"]
    )
    assert seal_finished.is_set()

    replay = ContextCollector().collect(
        ContextRequest(
            sources=["obsidian_tasks"],
            custom={"obsidian_tasks": {"vault_root": str(vault)}},
            max_age_seconds=0,
        )
    )
    assert "native task store" in _markdown(replay.sections["obsidian_tasks"])
    assert "Racing legacy task" not in _markdown(replay.sections["obsidian_tasks"])
