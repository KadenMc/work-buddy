from __future__ import annotations

from datetime import date
import sqlite3

import pytest

from work_buddy.tasks.models import Task
from work_buddy.tasks.workspace_query import WorkspaceQuery, read_workspace


def insert_tasks(store, rows):
    """Small disposable authority fixture; intentionally no vault/project reads."""
    with store.transaction() as conn:
        for row in rows:
            fields = {
                "description": "A task", "created_at": "2026-09-01T12:00:00Z",
                "updated_at": "2026-09-02T12:00:00Z", **row,
            }
            conn.execute(
                f"INSERT INTO task_metadata ({','.join(fields)}) VALUES "
                f"({','.join('?' for _ in fields)})", list(fields.values())
            )


def ids(result):
    return [item["task_id"] for item in result["tasks"]]


@pytest.mark.parametrize("needle", ["écg", "STRASSE", "straß"])
def test_search_retains_unicode_casefold_semantics_before_paging(task_store, needle):
    insert_tasks(task_store, [
        {"task_id": "one", "description": "Réviser ÉCG Straße"},
        {"task_id": "two", "description": "Other task", "state": "waiting"},
        {"task_id": "three", "description": "No match"},
    ])
    with task_store.transaction() as conn:
        conn.execute("INSERT INTO task_tags VALUES('two','écg-straße',0)")
    result = read_workspace(task_store, WorkspaceQuery(q=needle, limit=1, offset=1))
    assert result["total"] == 2
    assert ids(result) == ["two"]
    assert result["facets"]["attention"]["inbox"] == 1
    assert result["facets"]["attention"]["waiting"] == 1


def test_filter_and_sort_complete_collection_before_paging(task_store, monkeypatch):
    insert_tasks(task_store, [
        {"task_id": f"t-{index:05}", "description": "Needle" if index >= 5000 else "Other",
         "created_at": f"2026-09-{1 + index // 250:02}T12:00:00Z"}
        for index in range(5105)
    ])
    monkeypatch.setattr(Task, "from_row", lambda *_a, **_kw: pytest.fail("Hydrated full task"))
    first = read_workspace(task_store, WorkspaceQuery(q="Needle", limit=50))
    second = read_workspace(task_store, WorkspaceQuery(q="Needle", limit=50, offset=50))
    last = read_workspace(task_store, WorkspaceQuery(q="Needle", limit=50, offset=100))
    assert first["total"] == second["total"] == last["total"] == 105
    assert ids(first) == [f"t-{i:05}" for i in range(5000, 5050)]
    assert len(set(ids(first) + ids(second) + ids(last))) == 105
    assert first["page"] == {"limit": 50, "offset": 0, "has_more": True}
    assert last["page"] == {"limit": 50, "offset": 100, "has_more": False}
    all_open = read_workspace(task_store)
    assert all_open["total"] == 5105
    assert all_open["tasks"][0]["created_at"] == "2026-09-21T12:00:00Z"


def test_status_precedence_default_open_includes_snoozed(task_store):
    insert_tasks(task_store, [
        {"task_id": "inbox"}, {"task_id": "snoozed", "state": "snoozed"},
        {"task_id": "done", "state": "done", "completed_at": "2026-09-02"},
        {"task_id": "archive", "state": "done", "archived_at": "2026-09-03"},
        {"task_id": "trash", "state": "done", "archived_at": "2026-09-03", "deleted_at": "2026-09-04"},
    ])
    default = read_workspace(task_store)
    assert set(ids(default)) == {"inbox", "snoozed"}
    assert default["facets"]["statuses"] == {"open": 2, "completed": 1, "archived": 1, "trash": 1}
    selected = read_workspace(task_store, WorkspaceQuery(statuses=("completed", "archived")))
    assert {item["task_id"]: item["status"] for item in selected["tasks"]} == {
        "done": "completed", "archive": "archived"
    }
    assert read_workspace(task_store, WorkspaceQuery(statuses=()))["total"] == 5


def test_multi_filters_are_or_within_and_between_and_projects_independent(task_store):
    insert_tasks(task_store, [
        {"task_id": "one", "state": "active", "urgency": "high"},
        {"task_id": "two", "state": "waiting", "urgency": "low"},
        {"task_id": "three", "state": "inbox", "urgency": "medium"},
        {"task_id": "four", "state": "done", "urgency": "high"},
    ])
    with task_store.transaction() as conn:
        conn.executemany("INSERT INTO task_tags VALUES (?, ?, 1)", [
            ("one", "research/ecg/a"), ("one", "research/ecg/b"),
            ("two", "projects/unrelated"), ("three", "research"), ("four", "research/ecg"),
        ])
        conn.executemany("INSERT INTO task_projects VALUES (?, ?)", [("one", 1), ("one", 2), ("two", 2)])
    query = WorkspaceQuery(
        namespaces=("research", "projects"), projects=("1", "2"),
        attention=("active", "waiting"), urgencies=("low", "high"),
    )
    result = read_workspace(task_store, query)
    assert set(ids(result)) == {"one", "two"}
    assert next(item for item in result["tasks"] if item["task_id"] == "one")["project_ids"] == [1, 2]
    assert result["facets"]["projects"]["2"] == 2
    assert read_workspace(task_store, WorkspaceQuery(projects=("1",), namespaces=("projects",)))["total"] == 0


def test_tree_synthesizes_ancestors_unique_counts_and_exact_selection(task_store):
    insert_tasks(task_store, [{"task_id": "one"}, {"task_id": "two"}, {"task_id": "empty"}])
    with task_store.transaction() as conn:
        conn.executemany("INSERT INTO task_tags VALUES (?, ?, 1)", [
            ("one", "work/ecg/lead"), ("one", "work/ecg/label"), ("two", "work/ecg"),
        ])
    result = read_workspace(task_store, WorkspaceQuery(namespaces=("absent/deep",)))
    assert result["total"] == 0
    tree = {item["path"]: item for item in result["namespace_tree"]}
    assert tree["work"] == {"path": "work", "parent": None, "label": "work", "count": 2, "direct_count": 0}
    assert tree["work/ecg"]["count"] == 2
    assert tree["work/ecg"]["direct_count"] == 1
    assert tree["absent/deep"]["count"] == 0
    assert result["facets"]["namespaces"]["__none__"] == 1
    assert set(ids(read_workspace(task_store, WorkspaceQuery(namespaces=("work",))))) == {"one", "two"}
    assert ids(read_workspace(task_store, WorkspaceQuery(exact_namespaces=("work/ecg",)))) == ["two"]
    assert ids(read_workspace(task_store, WorkspaceQuery(namespaces=("__none__",)))) == ["empty"]


def test_facets_ignore_only_their_own_filter(task_store):
    insert_tasks(task_store, [
        {"task_id": "active", "state": "active", "urgency": "high"},
        {"task_id": "waiting", "state": "waiting", "urgency": "low"},
        {"task_id": "completed", "state": "done", "urgency": "high"},
    ])
    result = read_workspace(task_store, WorkspaceQuery(urgencies=("high",)))
    assert result["facets"]["statuses"] == {"open": 1, "completed": 1, "archived": 0, "trash": 0}
    assert result["facets"]["urgencies"] == {"low": 1, "medium": 0, "high": 1}
    assert result["facets"]["attention"]["waiting"] == 0


def test_historical_empty_segments_and_numeric_namespaces_remain_distinct(task_store):
    insert_tasks(task_store, [
        {"task_id": "trailing", "archived_at": "2026-06-27"},
        {"task_id": "nested", "archived_at": "2026-06-27"},
        {"task_id": "sibling", "archived_at": "2026-06-27"},
        {"task_id": "numeric", "deleted_at": "2026-05-28"},
        {"task_id": "ordinary"}, {"task_id": "project"},
    ])
    with task_store.transaction() as conn:
        conn.executemany("INSERT INTO task_tags VALUES (?, ?, ?)", [
            ("trailing", "projects/", 1), ("nested", "projects//nested", 1),
            ("sibling", "projects/work", 1), ("numeric", "138", 1),
            ("ordinary", "138", 0),
        ])
        conn.execute("INSERT INTO task_projects VALUES ('project',138)")
    assert read_workspace(task_store)["namespace_tree"] == []
    selected = WorkspaceQuery(namespaces=("138",), exact_namespaces=("projects//nested", "projects/"))
    tree = {item["path"]: item for item in read_workspace(task_store, selected)["namespace_tree"]}
    assert tree["projects/"] == {
        "path": "projects/", "parent": "projects", "label": "(empty segment)",
        "count": 0, "direct_count": 0,
    }
    assert tree["projects//nested"]["parent"] == "projects/"
    assert tree["138"] == {
        "path": "138", "parent": None, "label": "138", "count": 0, "direct_count": 0,
    }
    exact = read_workspace(task_store, WorkspaceQuery(statuses=("archived",), exact_namespaces=("projects/",)))
    assert ids(exact) == ["trailing"]
    branch = read_workspace(task_store, WorkspaceQuery(statuses=("archived",), namespaces=("projects/",)))
    assert set(ids(branch)) == {"trailing", "nested"}
    assert ids(read_workspace(task_store, WorkspaceQuery(statuses=("trash",), namespaces=("138",)))) == ["numeric"]
    assert [item["path"] for item in read_workspace(task_store, WorkspaceQuery(statuses=("trash",)))["namespace_tree"]] == ["138"]
    missing = read_workspace(task_store, WorkspaceQuery(namespaces=("missing/",)))
    assert next(item for item in missing["namespace_tree"] if item["path"] == "missing/")["label"] == "(empty segment)"
    assert task_store.get("trailing").namespace_tags == ("projects/",)


@pytest.mark.parametrize("filters", [
    {"q": "Needle"}, {"urgencies": ("high",)}, {"attention": ("active",)},
    {"projects": ("1",)}, {"note": "yes"}, {"due": "today"},
])
def test_namespace_picker_honors_each_other_filter(task_store, filters):
    insert_tasks(task_store, [
        {"task_id": "matching", "description": "Needle", "state": "active", "urgency": "high", "due_date": "2026-09-09"},
        {"task_id": "other", "description": "Other", "state": "waiting", "urgency": "low", "due_date": "2026-09-11"},
    ])
    with task_store.transaction() as conn:
        conn.executemany("INSERT INTO task_tags VALUES (?, ?, 1)", [
            ("matching", "work/matching/deep"), ("other", "work/other"),
        ])
        conn.executemany("INSERT INTO task_projects VALUES (?, ?)", [("matching", 1), ("other", 2)])
        conn.execute("INSERT INTO task_document_links VALUES ('matching','matching','store','doc','binding','active','2026-09-01','2026-09-01',NULL)")
    result = read_workspace(task_store, WorkspaceQuery(**filters), today=date(2026, 9, 9))
    assert ids(result) == ["matching"]
    assert {item["path"]: item["count"] for item in result["namespace_tree"]} == {
        "work": 1, "work/matching": 1, "work/matching/deep": 1,
    }
    assert {item["value"] for item in result["options"]["namespaces"]} == {
        "work", "work/matching", "work/matching/deep", "work/other",
    }


def test_authoring_namespace_options_include_filtered_statuses_and_exact_paths_without_hydration(task_store, monkeypatch):
    insert_tasks(task_store, [
        {"task_id": "visible", "description": "Needle", "state": "active"},
        {"task_id": "archived", "archived_at": "2026-06-27"},
        {"task_id": "trash", "deleted_at": "2026-05-28"},
        {"task_id": "other", "state": "waiting"},
    ])
    with task_store.transaction() as conn:
        conn.executemany("INSERT INTO task_tags VALUES (?, ?, ?)", [
            ("visible", "work/current", 1), ("archived", "projects/", 1),
            ("archived", "projects//nested", 1), ("trash", "projects/", 1),
            ("trash", "138", 1), ("other", "other", 1), ("visible", "ordinary", 0),
        ])
        conn.execute("INSERT INTO task_projects VALUES ('visible',1)")
    monkeypatch.setattr(Task, "from_row", lambda *_a, **_kw: pytest.fail("Hydrated full task"))
    query = WorkspaceQuery(q="Needle", attention=("active",), projects=("1",))
    result = read_workspace(task_store, query)
    assert ids(result) == ["visible"]
    assert [item["path"] for item in result["namespace_tree"]] == ["work", "work/current"]
    assert result["options"]["namespaces"] == [
        {"value": path, "label": path}
        for path in ("138", "other", "projects", "projects/", "projects//nested", "work", "work/current")
    ]
    assert read_workspace(task_store, WorkspaceQuery(statuses=("trash",)))["options"]["namespaces"] == result["options"]["namespaces"]


def test_namespace_picker_excludes_its_own_multi_selection_but_keeps_zero_selected_paths(task_store):
    insert_tasks(task_store, [
        {"task_id": "a"}, {"task_id": "b"}, {"task_id": "c"},
        {"task_id": "old", "archived_at": "2026-06-27"},
        {"task_id": "unselected-old", "archived_at": "2026-06-27"},
    ])
    with task_store.transaction() as conn:
        conn.executemany("INSERT INTO task_tags VALUES (?, ?, 1)", [
            ("a", "work/a"), ("b", "work/b"), ("c", "work/c"),
            ("old", "archive/old"), ("unselected-old", "archive/unselected"),
        ])
    query = WorkspaceQuery(namespaces=("work/a", "archive/old"), exact_namespaces=("work/c", "missing/deep/"))
    result = read_workspace(task_store, query)
    assert set(ids(result)) == {"a", "c"}
    assert {item["path"]: item["count"] for item in result["namespace_tree"]} == {
        "work": 3, "work/a": 1, "work/b": 1, "work/c": 1,
        "archive": 0, "archive/old": 0,
        "missing": 0, "missing/deep": 0, "missing/deep/": 0,
    }
    empty = next(item for item in result["namespace_tree"] if item["path"] == "missing/deep/")
    assert empty["label"] == "(empty segment)"
    assert empty["parent"] == "missing/deep"
    assert empty["direct_count"] == 0
    from work_buddy.tasks.namespaces import TaskNamespaceService
    from work_buddy.tasks.service import TaskApplicationService
    management = TaskNamespaceService(TaskApplicationService(task_store)).inventory()
    assert {item["path"]: item["count"] for item in management["namespaces"]} == {
        "work": 3, "work/a": 1, "work/b": 1, "work/c": 1,
        "archive": 2, "archive/old": 1, "archive/unselected": 1,
    }


@pytest.mark.parametrize("field", ["created_at", "updated_at", "due_date"])
@pytest.mark.parametrize("direction,expected", [("asc", ["old", "new"]), ("desc", ["new", "old"])])
def test_dates_handle_timezones_unknowns_and_ties(task_store, field, direction, expected):
    insert_tasks(task_store, [
        {"task_id": "old", field: "2026-09-01T23:30:00+02:00"},
        {"task_id": "new", field: "2026-09-01T22:00:00Z"},
        {"task_id": "unknown-a", field: ""},
        {"task_id": "unknown-b", field: "unknown"},
        {"task_id": "unknown-c", field: "now"},
    ])
    result = read_workspace(task_store, WorkspaceQuery(sort=field, direction=direction))
    assert ids(result) == expected + ["unknown-a", "unknown-b", "unknown-c"]
    if field == "created_at":
        assert result["tasks"][-3]["created_at"] is None
        assert result["tasks"][-3]["updated_at"] is not None


@pytest.mark.parametrize("field,direction,expected", [
    ("title", "asc", ["a", "b", "c"]), ("title", "desc", ["c", "b", "a"]),
    ("urgency", "desc", ["b", "a", "c"]), ("urgency", "asc", ["c", "a", "b"]),
])
def test_title_and_urgency_sort(task_store, field, direction, expected):
    insert_tasks(task_store, [
        {"task_id": "a", "description": "alpha", "urgency": "medium"},
        {"task_id": "b", "description": "Bravo", "urgency": "high"},
        {"task_id": "c", "description": "charlie", "urgency": "low"},
    ])
    assert ids(read_workspace(task_store, WorkspaceQuery(sort=field, direction=direction))) == expected


def test_project_unresolved_is_visible_and_not_unassigned(task_store):
    insert_tasks(task_store, [{"task_id": "orphan"}, {"task_id": "empty"}])
    with task_store.transaction() as conn:
        conn.execute(
            "INSERT INTO task_project_unresolved(task_id,legacy_value,source_tag,reason,candidate_ids_json) "
            "VALUES ('orphan','legacy','projects/legacy','ambiguous','[7,8]')"
        )
    assert ids(read_workspace(task_store, WorkspaceQuery(projects=("__none__",)))) == ["empty"]
    for filter_value in ("__unresolved__", "unresolved:legacy"):
        result = read_workspace(task_store, WorkspaceQuery(projects=(filter_value,)))
        assert ids(result) == ["orphan"]
        assert result["tasks"][0]["unresolved_projects"][0]["candidate_ids"] == [7, 8]
    missing = read_workspace(task_store, WorkspaceQuery(projects=("12345",)))
    assert missing["facets"]["projects"]["12345"] == 0


def test_document_and_due_filters_use_visible_link_and_selected_week(task_store):
    insert_tasks(task_store, [
        {"task_id": "linked", "due_date": "2026-09-09"},
        {"task_id": "retired", "due_date": "2026-09-08"},
        {"task_id": "unlinked", "due_date": "2026-09-16"},
        {"task_id": "none", "due_date": None},
    ])
    with task_store.transaction() as conn:
        conn.executemany(
            "INSERT INTO task_document_links VALUES (?,?,?,?,?,?,?,?,?)",
            [(task_id, task_id, "store", task_id, task_id, lifecycle, "2026-09-01", "2026-09-01", None)
             for task_id, lifecycle in (("linked", "active"), ("retired", "retired"))],
        )
    today = date(2026, 9, 9)
    assert ids(read_workspace(task_store, WorkspaceQuery(note="yes", due="today"), today=today)) == ["linked"]
    assert ids(read_workspace(task_store, WorkspaceQuery(note="no", due="overdue"), today=today)) == ["retired"]
    assert set(ids(read_workspace(task_store, WorkspaceQuery(due="week"), today=today))) == {"linked", "unlinked"}
    assert ids(read_workspace(task_store, WorkspaceQuery(due="none"), today=today)) == ["none"]


def test_search_and_namespace_prefix_escape_sql_wildcards(task_store):
    insert_tasks(task_store, [
        {"task_id": "literal", "description": "50%_ progress"},
        {"task_id": "broad", "description": "50XX progress"},
    ])
    with task_store.transaction() as conn:
        conn.executemany("INSERT INTO task_tags VALUES (?, ?, 1)", [
            ("literal", "50%_/child"), ("broad", "50XX/child")
        ])
    assert ids(read_workspace(task_store, WorkspaceQuery(q="50%_"))) == ["literal"]
    assert ids(read_workspace(task_store, WorkspaceQuery(namespaces=("50%_",)))) == ["literal"]


def test_query_rejects_sql_and_invalid_filters():
    for kwargs in ({"sort": "created_at; DROP TABLE task_metadata"}, {"direction": "random"},
                   {"statuses": ("invalid",)}, {"due": "random"}, {"note": "unknown"}):
        with pytest.raises(ValueError):
            WorkspaceQuery(**kwargs)


@pytest.mark.parametrize("value", ["garbage", "-2", "0", "1.5", "unresolved:", "unresolved: ", True])
def test_project_query_validation_happens_before_database_read(value):
    with pytest.raises(ValueError, match="Project filters must identify"):
        WorkspaceQuery(projects=(value,))


def test_project_query_normalizes_duplicate_registry_ids_and_special_options():
    query = WorkspaceQuery(projects=("01", "1", " 2 ", "__none__", "__unresolved__", "unresolved: old-name "))
    assert query.projects == ("1", "2", "__none__", "__unresolved__", "unresolved:old-name")


def test_view_uses_one_read_snapshot_during_concurrent_change(task_store, monkeypatch):
    insert_tasks(task_store, [{"task_id": "before", "description": "Before the edit"}])
    original = task_store.connect_readonly
    changed = False

    def connect():
        conn = original()

        def concurrent_edit(sql):
            nonlocal changed
            if changed or "SELECT COUNT(*) FROM task_metadata" not in sql:
                return
            changed = True
            with sqlite3.connect(task_store.path) as writer:
                writer.execute("UPDATE task_metadata SET description = 'After the edit', revision = 2")
                writer.execute("UPDATE task_collection_state SET revision = revision + 1")

        conn.set_trace_callback(concurrent_edit)
        return conn

    monkeypatch.setattr(task_store, "connect_readonly", connect)
    result = read_workspace(task_store)
    assert changed
    assert result["tasks"][0]["title"] == "Before the edit"
    assert result["collection_revision"] == 0
    assert task_store.get("before").description == "After the edit"


def test_namespace_lookup_is_bounded_to_task_assignments(task_store):
    # The historical (is_namespace,tag) index led SQLite to scan all namespace
    # assignments for every task. Guard this access pattern, not wall time.
    conn = task_store.connect_readonly()
    try:
        plans = [row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT t.task_id FROM task_metadata t WHERE EXISTS "
            "(SELECT 1 FROM task_tags nt WHERE nt.task_id=t.task_id AND nt.is_namespace=1 "
            "AND (nt.tag = ? COLLATE NOCASE OR nt.tag LIKE ? COLLATE NOCASE))",
            ("research", "research/%"),
        )]
        assert any("task_id=? AND is_namespace=?" in plan for plan in plans)
    finally:
        conn.close()
