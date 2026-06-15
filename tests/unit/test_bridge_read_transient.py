"""Transient-vs-absent handling for bridge reads in consequential consumers.

The soft `bridge.read_file()` returns ``None`` for BOTH a genuinely-absent file
AND a transient bridge failure. `bridge.read_file_raw()` is the typed read that
distinguishes them: content on 2xx, ``None`` only on a genuine 404, and a RAISED
typed ``ObsidianError`` on any transient/structural failure (so a
``@bridge_retry``-wrapped caller / the gateway retries it like a write).

These tests pin:
- the `read_file_raw` primitive contract;
- every consequential consumer migrated off the soft read (a transient must
  NOT become a false "absent/empty/not-found" — it raises/propagates; a genuine
  404 stays correctly "absent");
- the hybrid mass-delete circuit-breaker in `reconcile_drift` (a degraded read
  must never soft-delete most of the store) + its visible "degraded" status.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from work_buddy.obsidian.retry import is_bridge_failure
from work_buddy.obsidian.errors import (
    ObsidianRefused,
    ObsidianServerError,
    ObsidianStartupRace,
    ObsidianTimeout,
)


# ── 1. read_file_raw primitive ───────────────────────────────────────────


class TestReadFileRaw:
    def test_2xx_returns_content(self):
        from work_buddy.obsidian import bridge
        with patch.object(bridge, "_request_with_status",
                          return_value=(200, {"content": "hello"})):
            assert bridge.read_file_raw("notes/x.md") == "hello"

    def test_genuine_404_returns_none(self):
        from work_buddy.obsidian import bridge
        with patch.object(bridge, "_request_with_status",
                          side_effect=ObsidianRefused(404)):
            assert bridge.read_file_raw("notes/x.md") is None

    def test_transient_raises(self):
        from work_buddy.obsidian import bridge
        for exc in (ObsidianTimeout("t"), ObsidianStartupRace(),
                    ObsidianServerError(500)):
            with patch.object(bridge, "_request_with_status", side_effect=exc):
                with pytest.raises(type(exc)):
                    bridge.read_file_raw("notes/x.md")

    def test_other_4xx_reraises(self):
        from work_buddy.obsidian import bridge
        with patch.object(bridge, "_request_with_status",
                          side_effect=ObsidianRefused(403)):
            with pytest.raises(ObsidianRefused):
                bridge.read_file_raw("notes/x.md")


# ── 2. day_planner: get_todays_plan / write_plan ─────────────────────────


class TestDayPlanner:
    def test_get_todays_plan_transient_propagates(self):
        from work_buddy.obsidian.day_planner import env
        with patch.object(env, "bridge") as mb:
            mb.read_file_raw.side_effect = ObsidianTimeout("blink")
            with pytest.raises(ObsidianTimeout):
                env.get_todays_plan("journal/2026-06-15.md")

    def test_get_todays_plan_404_is_found_false(self):
        from work_buddy.obsidian.day_planner import env
        with patch.object(env, "bridge") as mb:
            mb.read_file_raw.return_value = None
            result = env.get_todays_plan("journal/2026-06-15.md")
        assert result["found"] is False

    def test_write_plan_transient_propagates(self):
        from work_buddy.obsidian.day_planner import env
        with patch.object(env, "bridge") as mb:
            mb.read_file_raw.side_effect = ObsidianTimeout("blink")
            with pytest.raises(ObsidianTimeout):
                env.write_plan("journal/2026-06-15.md", [])

    def test_write_plan_404_is_non_transient_failure(self):
        from work_buddy.obsidian.day_planner import env
        with patch.object(env, "bridge") as mb:
            mb.read_file_raw.return_value = None
            result = env.write_plan("journal/2026-06-15.md", [])
        assert result["success"] is False
        assert "does not exist" in result["reason"]


# ── 3. archive_completed: don't clobber on a transient ───────────────────


class TestArchiveCompleted:
    _MASTER = "- [x] old done task ✅ 2020-01-01 🆔 t-old1\n"

    def _raw(self):
        from work_buddy.obsidian.tasks import mutations
        return inspect.unwrap(mutations.archive_completed)

    def test_transient_archive_read_propagates_no_clobber(self):
        from work_buddy.obsidian.tasks import mutations

        def reader(path):
            if "archive" in path:
                raise ObsidianTimeout("blink")  # transient on the archive read
            return self._MASTER

        with patch.object(mutations, "bridge") as mb, patch.object(mutations, "store"):
            mb.read_file_raw.side_effect = reader
            mb.write_file.return_value = True
            with pytest.raises(ObsidianTimeout):
                self._raw()(older_than_days=0)
            # The clobbering fresh-header write must NOT have happened.
            mb.write_file.assert_not_called()

    def test_genuine_absent_creates_fresh_no_regression(self):
        from work_buddy.obsidian.tasks import mutations

        def reader(path):
            if "archive" in path:
                return None  # 404 — first-ever archive
            return self._MASTER

        with patch.object(mutations, "bridge") as mb, patch.object(mutations, "store") as ms:
            mb.read_file_raw.side_effect = reader
            mb.write_file.return_value = True
            ms.query.return_value = []
            result = self._raw()(older_than_days=0)

        assert not is_bridge_failure(result)
        wrote_fresh_header = any(
            "# Task Archive" in (
                call.args[1] if len(call.args) > 1
                else call.kwargs.get("content", "")
            )
            for call in mb.write_file.call_args_list
        )
        assert wrote_fresh_header, mb.write_file.call_args_list


# ── 4. Secondary consumers: transient ≠ false absent ─────────────────────


class TestSecondaryConsumers:
    def test_task_match_read_task_texts_transient_propagates(self):
        from work_buddy.clarify import task_match
        with patch("work_buddy.obsidian.bridge.read_file_raw",
                   side_effect=ObsidianTimeout("blink")):
            with pytest.raises(ObsidianTimeout):
                task_match._read_task_texts()

    def test_task_match_read_task_texts_404_is_empty(self):
        from work_buddy.clarify import task_match
        with patch("work_buddy.obsidian.bridge.read_file_raw", return_value=None):
            assert task_match._read_task_texts() == {}

    def test_calendar_run_js_transient_raises_not_crash(self):
        from work_buddy.calendar import env as cal_env
        with patch.object(cal_env.bridge, "require_available"), \
             patch.object(cal_env.bridge, "eval_js", return_value=None):
            # Previously None → get_calendars did None.get(...) → AttributeError.
            with pytest.raises(ObsidianTimeout):
                cal_env._run_js("get_calendars.js")


# ── 5. Mass-delete circuit-breaker + observable status ───────────────────


class TestMassDeleteBreaker:
    def _db(self):
        from work_buddy.obsidian.tasks.markdown_db import TaskMarkdownDB
        from work_buddy.obsidian.tasks import store as task_store
        return TaskMarkdownDB(task_store)

    @staticmethod
    def _rows(n):
        return [{"task_id": f"t-{i}"} for i in range(n)]

    def test_empty_parse_with_full_store_trips(self):
        db = self._db()
        with patch.object(db, "parse_all_from_markdown", return_value={}), \
             patch.object(db, "_store_query", return_value=self._rows(50)), \
             patch.object(db, "_store_delete") as mock_del, \
             patch.object(db, "post_reconcile"):
            report = db.reconcile_drift()
        assert report.aborted_bulk_delete == (50, 50)
        mock_del.assert_not_called()

    def test_proportion_over_threshold_trips(self):
        db = self._db()
        parsed = {f"t-{i}": object() for i in range(5)}  # 5 parsed, 50 store
        with patch.object(db, "parse_all_from_markdown", return_value=parsed), \
             patch.object(db, "_store_query", return_value=self._rows(50)), \
             patch.object(db, "_reconcile_one_entity"), \
             patch.object(db, "_store_delete") as mock_del, \
             patch.object(db, "post_reconcile"):
            report = db.reconcile_drift()
        # would-delete 45 > max(20, 25) → trip
        assert report.aborted_bulk_delete == (45, 50)
        mock_del.assert_not_called()

    def test_normal_small_delete_proceeds(self):
        db = self._db()
        parsed = {f"t-{i}": object() for i in range(49)}  # 1 orphan (t-49)
        with patch.object(db, "parse_all_from_markdown", return_value=parsed), \
             patch.object(db, "_store_query", return_value=self._rows(50)), \
             patch.object(db, "markdown_exists", return_value=False), \
             patch.object(db, "_reconcile_one_entity"), \
             patch.object(db, "_store_delete") as mock_del, \
             patch.object(db, "post_reconcile"):
            report = db.reconcile_drift()
        assert report.aborted_bulk_delete is None
        mock_del.assert_called_once_with("t-49")

    def test_small_store_empty_parse_proceeds(self):
        # Floor of 20: a tiny store whose parse comes back empty is below the
        # threshold, so deletes proceed normally (a 3-of-3 wipe is not a "mass"
        # delete). This is the low-false-positive behavior the floor buys.
        db = self._db()
        with patch.object(db, "parse_all_from_markdown", return_value={}), \
             patch.object(db, "_store_query", return_value=self._rows(3)), \
             patch.object(db, "markdown_exists", return_value=False), \
             patch.object(db, "_store_delete") as mock_del, \
             patch.object(db, "post_reconcile"):
            report = db.reconcile_drift()
        assert report.aborted_bulk_delete is None
        assert mock_del.call_count == 3

    def test_reconcile_tasks_reports_degraded_on_trip(self):
        from work_buddy.obsidian.tasks import markdown_db as mdb
        from work_buddy.markdown_db.types import ReconcileReport
        rep = ReconcileReport()
        rep.aborted_bulk_delete = (50, 50)
        with patch.object(mdb, "TaskMarkdownDB") as MockDB:
            MockDB.return_value.reconcile_drift.return_value = rep
            MockDB.return_value._last_tag_rows = 0
            result = mdb.reconcile_tasks()
        assert result["status"] == "degraded"
        assert result["aborted_bulk_delete"] == (50, 50)
