"""Unit tests for the task tag cache (Phase 1 of namespace hierarchy).

Covers:
- extract_tags_from_line: regex correctness incl. nested paths, wikilinks
- classify_tags: reserved prefixes, opt-in prefixes, discovery threshold
- store.set_task_tags / tasks_with_tag / distinct_namespace_tags: roundtrip
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks.sync import (
    classify_tags,
    extract_tags_from_line,
)


# ── extract_tags_from_line ──────────────────────────────────────


class TestExtractTagsFromLine:
    def test_simple_tag(self):
        assert extract_tags_from_line("- [ ] #todo do the thing #paper") == [
            "todo",
            "paper",
        ]

    def test_nested_path(self):
        tags = extract_tags_from_line(
            "- [ ] #todo walk the dog #health/exercise/daily 🆔 t-abc123"
        )
        assert "health/exercise/daily" in tags

    def test_projects_tag_preserved(self):
        tags = extract_tags_from_line(
            "- [ ] #todo draft outline #projects/ecg-classifier 🆔 t-abc"
        )
        assert "projects/ecg-classifier" in tags

    def test_case_insensitive_dedup(self):
        # Same tag appearing twice with different cases is counted once.
        tags = extract_tags_from_line("- [ ] #todo #Foo #foo")
        assert tags.count("Foo") + tags.count("foo") == 1

    def test_wikilink_not_matched(self):
        # The task-note wikilink uses a UUID, and `#` does not appear inside
        # it. A line with a wikilink should not produce spurious tags.
        line = "- [ ] #todo task [[aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee|📓]] 🆔 t-x"
        tags = extract_tags_from_line(line)
        assert tags == ["todo"]

    def test_hash_inside_word_ignored(self):
        # e.g. "issue#123" should not produce "123" as a tag.
        tags = extract_tags_from_line("- [ ] #todo re issue#123 later")
        assert "123" not in tags
        assert tags == ["todo"]


# ── classify_tags ────────────────────────────────────────────────


class TestClassifyTags:
    def test_reserved_prefix_never_namespace(self):
        # `todo`, `wb/todo`, and anything under `tasker/` stay out of the
        # tree. `projects/` is NOT reserved anymore — it's a first-class
        # organizational axis.
        counts = {"todo": 50, "wb/todo": 5, "tasker": 2, "tasker/state": 2, "tasker/state/inbox": 2}
        result = classify_tags(
            counts,
            ["todo", "wb/todo", "tasker/state/inbox"],
            threshold=2,
        )
        for _, is_ns in result:
            assert is_ns is False

    def test_projects_prefix_is_namespace(self):
        """`projects/<slug>` is the canonical project axis; it belongs in the tree."""
        counts = {"projects": 10, "projects/ecg": 10}
        result = classify_tags(counts, ["projects/ecg"], threshold=2)
        assert result == [("projects/ecg", True)]

    def test_wb_prefix_is_namespace_except_reserved_exact(self):
        """`wb/` is the canonical work-buddy-dev namespace prefix.
        Only `wb/todo` and `wb/done` (inline-todo markers) are reserved."""
        counts = {
            "wb": 38,
            "wb/consent": 5,
            "wb/workflow": 3,
            "wb/todo": 20,    # reserved — inline-TODO marker
            "wb/done": 10,    # reserved — inline-DONE marker
        }
        result = dict(
            classify_tags(counts, ["wb/consent", "wb/workflow", "wb/todo", "wb/done"], threshold=2)
        )
        assert result["wb/consent"] is True
        assert result["wb/workflow"] is True
        assert result["wb/todo"] is False
        assert result["wb/done"] is False

    def test_opt_in_prefix_always_namespace(self):
        # Even with count=1 and threshold=2, opt-in prefixes are namespaces.
        counts = {"ns": 1, "ns/foo": 1, "task": 1, "task/bar": 1}
        result = classify_tags(
            counts, ["ns/foo", "task/bar"], threshold=2,
        )
        assert dict(result) == {"ns/foo": True, "task/bar": True}

    def test_discovery_threshold_below(self):
        counts = {"paper": 1, "paper/ecg": 1}
        result = classify_tags(counts, ["paper/ecg"], threshold=2)
        assert result == [("paper/ecg", False)]

    def test_discovery_threshold_at_boundary(self):
        counts = {"paper": 2, "paper/ecg": 2}
        result = classify_tags(counts, ["paper/ecg"], threshold=2)
        assert result == [("paper/ecg", True)]

    def test_parent_rescues_rare_leaf(self):
        # A one-off leaf (`research/electricrag/writing-prep` appears on
        # a single task) is rescued by a popular parent prefix. This was
        # the bug that hid the "Draft electricrag paper contract" task
        # from the namespace tree.
        counts = {
            "research": 11,
            "research/electricrag": 11,
            "research/electricrag/quickhacks": 10,
            "research/electricrag/writing-prep": 1,
        }
        result = classify_tags(
            counts, ["research/electricrag/writing-prep"], threshold=2,
        )
        assert result == [("research/electricrag/writing-prep", True)]

    def test_no_rescue_when_all_prefixes_rare(self):
        # Single-segment rare tag with no popular ancestor stays out.
        counts = {"admin": 1}
        result = classify_tags(counts, ["admin"], threshold=2)
        assert result == [("admin", False)]

    def test_mixed_tags(self):
        counts = {
            "projects": 5,
            "projects/ecg": 5,       # projects is first-class now → namespace
            "paper": 3,
            "paper/ecg": 3,          # over threshold → namespace
            "admin": 1,              # under threshold → not namespace
            "ns": 1,
            "ns/singleton": 1,       # opt-in → namespace
        }
        tags = ["projects/ecg", "paper/ecg", "admin", "ns/singleton"]
        result = dict(classify_tags(counts, tags, threshold=2))
        assert result == {
            "projects/ecg": True,
            "paper/ecg": True,
            "admin": False,
            "ns/singleton": True,
        }

    def test_reserved_wins_over_opt_in(self):
        # Reserved check runs first; a `tasker/...` tag is never a namespace
        # even if prefix counts would otherwise classify it as one.
        counts = {"tasker": 10, "tasker/foo": 10}
        result = classify_tags(counts, ["tasker/foo"], threshold=2)
        assert result == [("tasker/foo", False)]


# ── store: roundtrip ────────────────────────────────────────────


@pytest.fixture
def _isolated_store(monkeypatch, tmp_path):
    """Redirect store._db_path to a tmp SQLite file for isolation."""
    db_file = tmp_path / "tasks.sqlite"

    def _patched_db_path() -> Path:
        return db_file

    monkeypatch.setattr(store, "_db_path", _patched_db_path)
    return db_file


class TestStoreTagRoundtrip:
    def test_set_and_get_tags(self, _isolated_store):
        store.create(task_id="t-abc", state="inbox")
        store.set_task_tags(
            "t-abc",
            [("paper/ecg", True), ("todo", False), ("admin", False)],
        )

        rows = store.get_task_tags("t-abc")
        by_tag = {r["tag"]: r["is_namespace"] for r in rows}
        assert by_tag == {"paper/ecg": 1, "todo": 0, "admin": 0}

    def test_set_tags_replaces_existing(self, _isolated_store):
        store.create(task_id="t-abc", state="inbox")
        store.set_task_tags("t-abc", [("first", True)])
        store.set_task_tags("t-abc", [("second", True)])

        rows = store.get_task_tags("t-abc")
        assert [r["tag"] for r in rows] == ["second"]

    def test_tasks_with_tag_prefix_match(self, _isolated_store):
        for tid in ("t-a", "t-b", "t-c"):
            store.create(task_id=tid, state="inbox")
        store.set_task_tags("t-a", [("paper", True)])
        store.set_task_tags("t-b", [("paper/ecg", True)])
        store.set_task_tags("t-c", [("paper/ecg/exp", True)])

        flat = store.tasks_with_tag("paper", prefix_match=False)
        assert flat == ["t-a"]

        deep = store.tasks_with_tag("paper", prefix_match=True)
        assert set(deep) == {"t-a", "t-b", "t-c"}

    def test_distinct_namespace_tags_counts(self, _isolated_store):
        for tid in ("t-a", "t-b"):
            store.create(task_id=tid, state="inbox")
        store.set_task_tags(
            "t-a",
            [("paper/ecg", True), ("admin", False)],
        )
        store.set_task_tags("t-b", [("paper/ecg", True)])

        out = store.distinct_namespace_tags()
        by_tag = {r["tag"]: r["count"] for r in out}
        # Namespace tag appears on 2 tasks; non-namespace is excluded.
        assert by_tag == {"paper/ecg": 2}

    def test_archived_tasks_excluded(self, _isolated_store):
        store.create(task_id="t-a", state="inbox")
        store.create(task_id="t-b", state="inbox")
        store.set_task_tags("t-a", [("paper/ecg", True)])
        store.set_task_tags("t-b", [("paper/ecg", True)])
        store.mark_archived("t-b")

        ids = store.tasks_with_tag("paper/ecg")
        assert ids == ["t-a"]

        counts = store.distinct_namespace_tags()
        assert {r["tag"]: r["count"] for r in counts} == {"paper/ecg": 1}

    def test_distinct_namespace_tags_recent_count(self, _isolated_store):
        """recent_count should reflect tasks created inside the window."""
        import sqlite3
        from datetime import datetime, timedelta, timezone

        store.create(task_id="t-new", state="inbox")
        store.create(task_id="t-old", state="inbox")
        store.set_task_tags("t-new", [("paper/ecg", True)])
        store.set_task_tags("t-old", [("paper/ecg", True)])

        # Backdate t-old's created_at to 30 days ago so it's outside a
        # 14-day window but still counted in `count`.
        old_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn = store.get_connection()
        try:
            conn.execute(
                "UPDATE task_metadata SET created_at = ? WHERE task_id = ?",
                (old_iso, "t-old"),
            )
            conn.commit()
        finally:
            conn.close()

        rows = store.distinct_namespace_tags(recent_days=14)
        assert len(rows) == 1
        r = rows[0]
        assert r["tag"] == "paper/ecg"
        assert r["count"] == 2
        assert r["recent_count"] == 1  # only t-new is inside the window

        # Zero window → no tasks qualify as recent.
        rows2 = store.distinct_namespace_tags(recent_days=0)
        assert rows2[0]["recent_count"] == 0

    def test_distinct_namespace_tags_returns_recent_count_key(self, _isolated_store):
        """Even for fresh tasks with no archive, recent_count must be an int."""
        store.create(task_id="t-a", state="inbox")
        store.set_task_tags("t-a", [("paper/ecg", True)])
        rows = store.distinct_namespace_tags()
        assert rows[0]["recent_count"] >= 1
        assert isinstance(rows[0]["recent_count"], int)
