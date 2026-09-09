"""Namespace changes preserve task identity and fail safely across preview/retry/undo."""

import pytest

from work_buddy.tasks.errors import TaskIdempotencyConflict, TaskValidationError
from work_buddy.tasks.models import Tag
from work_buddy.tasks.namespaces import NamespaceConflict, TaskNamespaceService


def seed(service, task_id, *namespaces, state="inbox"):
    result = service.create(
        task_id=task_id, description=task_id, state="inbox" if state == "done" else state,
        tags=[Tag(name, True) for name in namespaces],
        client_mutation_id="seed-" + task_id, actor="dashboard:user",
    )
    if state == "done":
        return service.complete(task_id, expected_revision=result.task.revision, client_mutation_id="complete-" + task_id, actor="dashboard:user")
    return result


def apply(organizer, request, mutation="organize", preview=None):
    preview = preview or organizer.preview(request)
    return organizer.apply({"request": preview["request"], "expected_fingerprint": preview["fingerprint"], "client_mutation_id": mutation}, actor="dashboard:user")


def test_promote_requires_direct_disposition_and_collision_review(task_service):
    seed(task_service, "a", "projects", "projects/work-buddy", "work-buddy")
    seed(task_service, "b", "projects/work-buddy/docs")
    organizer = TaskNamespaceService(task_service)
    request = {"action": "promote", "sources": ["projects"]}
    preview = organizer.preview(request)
    assert not preview["can_apply"]
    assert any("directly" in message for message in preview["issues"])
    assert preview["collisions"]
    request.update(parent_assignment="keep", merge_collisions=True)
    preview = organizer.preview(request)
    assert preview["task_count"] == 2
    assert preview["can_apply"]
    applied = apply(organizer, request, preview=preview)
    assert set(task_service.store.get("a").namespace_tags) == {"projects", "work-buddy"}
    assert task_service.store.get("b").namespace_tags == ("work-buddy/docs",)
    assert applied["operation"]["can_undo"]
    organizer.undo(applied["operation"]["operation_id"], {"client_mutation_id": "undo"}, actor="dashboard:user")
    assert set(task_service.store.get("a").namespace_tags) == {"projects", "projects/work-buddy", "work-buddy"}


def test_all_statuses_scope_and_unique_inventory_counts(task_service):
    seed(task_service, "a", "group/one", "group/two")
    seed(task_service, "b", "group/one", state="done")
    seed(task_service, "c", "group/two")
    seed(task_service, "d", "group/two")
    with task_service.store.transaction() as conn:
        conn.execute("UPDATE task_metadata SET archived_at='2026-09-01' WHERE task_id='c'")
        conn.execute("UPDATE task_metadata SET deleted_at='2026-09-01' WHERE task_id='d'")
    organizer = TaskNamespaceService(task_service)
    group = next(item for item in organizer.inventory()["namespaces"] if item["path"] == "group")
    assert group["count"] == 4 and group["direct_count"] == 0
    assert group["status_counts"] == dict(open=1, completed=1, archived=1, trash=1)
    preview = organizer.preview({"action": "rename", "sources": ["group"], "name": "renamed"})
    assert preview["scope"] == "all_statuses"
    assert preview["status_counts"] == group["status_counts"]
    apply(organizer, {}, preview=preview)
    with task_service.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_tags WHERE tag LIKE 'group/%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0] == 4


def test_preview_is_readonly_and_new_matching_task_invalidates(task_service):
    seed(task_service, "a", "old")
    organizer = TaskNamespaceService(task_service)
    before = task_service.store.get("a")
    request = {"action": "rename", "sources": ["old"], "name": "new"}
    preview = organizer.preview(request)
    assert task_service.store.get("a") == before
    assert organizer.inventory()["operations"] == []
    seed(task_service, "b", "old")
    with pytest.raises(NamespaceConflict):
        apply(organizer, request, preview=preview)
    assert task_service.store.get("a").namespace_tags == ("old",)


def test_apply_and_undo_have_durable_idempotent_receipts(task_service):
    seed(task_service, "a", "old")
    organizer = TaskNamespaceService(task_service)
    request = {"action": "rename", "sources": ["old"], "name": "new"}
    preview = organizer.preview(request)
    first = apply(organizer, request, preview=preview)
    second = apply(organizer, request, preview=preview)
    assert second["replayed"] and first["receipt_id"] == second["receipt_id"]
    assert task_service.store.get("a").revision == 2
    with pytest.raises(TaskIdempotencyConflict):
        apply(organizer, {"action": "rename", "sources": ["new"], "name": "later"})
    operation_id = first["operation"]["operation_id"]
    first_undo = organizer.undo(operation_id, {"client_mutation_id": "undo"}, actor="dashboard:user")
    second_undo = organizer.undo(operation_id, {"client_mutation_id": "undo"}, actor="dashboard:user")
    assert second_undo["replayed"] and second_undo["receipt_id"] == first_undo["receipt_id"]
    assert task_service.store.get("a").revision == 3
    assert not organizer.inventory()["operations"][0]["can_undo"]


def test_intervening_edit_blocks_entire_undo(task_service):
    seed(task_service, "a", "old")
    seed(task_service, "b", "old")
    organizer = TaskNamespaceService(task_service)
    result = apply(organizer, {"action": "rename", "sources": ["old"], "name": "new"})
    task_service.update("b", expected_revision=2, changes={"description": "Edited after organization"}, client_mutation_id="edit", actor="dashboard:user")
    with pytest.raises(NamespaceConflict):
        organizer.undo(result["operation"]["operation_id"], {"client_mutation_id": "undo"}, actor="dashboard:user")
    assert task_service.store.get("a").namespace_tags == ("new",)
    assert task_service.store.get("a").revision == 2


def test_failed_second_write_rolls_back_tasks_receipts_history(task_service, monkeypatch):
    seed(task_service, "a", "old")
    seed(task_service, "b", "old")
    organizer = TaskNamespaceService(task_service)
    original = task_service._append_history
    def fail_second(conn, **kwargs):
        if kwargs["task"].task_id == "b":
            raise RuntimeError("simulated write failure")
        return original(conn, **kwargs)
    monkeypatch.setattr(task_service, "_append_history", fail_second)
    with pytest.raises(RuntimeError, match="simulated"):
        apply(organizer, {"action": "rename", "sources": ["old"], "name": "new"})
    assert task_service.store.get("a").namespace_tags == ("old",)
    assert task_service.store.get("a").revision == 1
    assert organizer.inventory()["operations"] == []
    with task_service.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_mutation_receipts WHERE client_mutation_id='organize'").fetchone()[0] == 0


def test_assign_and_undo_preserve_ordinary_tags_and_project_links(task_service):
    seed(task_service, "a", "old")
    with task_service.store.transaction() as conn:
        conn.execute("INSERT INTO task_tags VALUES('a','note',0)")
        conn.execute("INSERT INTO task_projects VALUES('a',42)")
    organizer = TaskNamespaceService(task_service)
    result = apply(organizer, {"action": "assign", "task_ids": ["a"], "assignment_mode": "replace", "namespaces": ["new"]})
    assert task_service.store.get("a").namespace_tags == ("new",)
    assert task_service.store.get("a").project_ids == (42,)
    organizer.undo(result["operation"]["operation_id"], {"client_mutation_id": "undo"}, actor="dashboard:user")
    assert task_service.store.get("a").namespace_tags == ("old",)
    assert Tag("note", False) in task_service.store.get("a").tags
    assert task_service.store.get("a").project_ids == (42,)


def test_overlapping_selection_is_normalized_and_preview_pages_stable(task_service):
    seed(task_service, "a", "old/a")
    seed(task_service, "b", "old/b")
    organizer = TaskNamespaceService(task_service)
    request = {"action": "move", "sources": ["old", "old/a"], "destination": "parent", "tasks_limit": 1}
    first = organizer.preview(request)
    second = organizer.preview({**request, "tasks_offset": 1})
    assert first["request"]["sources"] == ["old"]
    assert first["fingerprint"] == second["fingerprint"]
    assert first["tasks_has_more"] and not second["tasks_has_more"]
    assert first["tasks"][0]["task_id"] != second["tasks"][0]["task_id"]
    with pytest.raises(TaskValidationError):
        organizer.preview({**request, "destination": "old/a"})


def test_exact_removal_does_not_remove_children_and_replace_can_clear(task_service):
    seed(task_service, "a", "parent", "parent/child")
    organizer = TaskNamespaceService(task_service)
    preview = organizer.preview({"action": "remove", "sources": ["parent"], "include_descendants": False})
    assert preview["unnamespaced_count"] == 0
    apply(organizer, {}, preview=preview)
    assert task_service.store.get("a").namespace_tags == ("parent/child",)
    request = {"action": "assign", "task_ids": ["a"], "assignment_mode": "replace", "namespaces": []}
    preview = organizer.preview(request)
    assert preview["unnamespaced_count"] == 1
    apply(organizer, request, mutation="clear", preview=preview)
    assert not task_service.store.get("a").namespace_tags


def test_implicit_grouping_collision_requires_explicit_merge(task_service):
    seed(task_service, "a", "projects/work-buddy/docs")
    seed(task_service, "b", "work-buddy/code")
    organizer = TaskNamespaceService(task_service)
    request = {"action": "promote", "sources": ["projects"]}
    preview = organizer.preview(request)
    assert not preview["can_apply"]
    assert {"from": "projects/work-buddy", "to": "work-buddy"} in preview["collisions"]
    assert not any("directly" in issue for issue in preview["issues"])
    request["merge_collisions"] = True
    assert organizer.preview(request)["can_apply"]
    apply(organizer, request)
    assert task_service.store.get("a").namespace_tags == ("work-buddy/docs",)


def test_historical_namespace_suffixes_can_be_removed_or_moved(task_service):
    seed(task_service, "a", "work/-draft")
    organizer = TaskNamespaceService(task_service)
    apply(organizer, {"action": "rename", "sources": ["work"], "name": "research"})
    assert task_service.store.get("a").namespace_tags == ("research/-draft",)
    apply(organizer, {"action": "remove", "sources": ["research/-draft"]}, mutation="remove")
    assert not task_service.store.get("a").namespace_tags


@pytest.mark.parametrize("operation, expected", [
    ({"action": "rename", "sources": ["work"], "name": "research"}, "research//draft"),
    ({"action": "move", "sources": ["work"], "destination": "research"}, "research/work//draft"),
    ({"action": "merge", "sources": ["work"], "destination": "research"}, "research//draft"),
])
def test_branch_changes_preserve_historical_repeated_separators(task_service, operation, expected):
    seed(task_service, "a", "work//draft")
    seed(task_service, "destination", "research")
    organizer = TaskNamespaceService(task_service)
    if operation["action"] == "rename":
        operation = {**operation, "merge_collisions": True}
    applied = apply(organizer, operation)
    assert task_service.store.get("a").namespace_tags == (expected,)
    organizer.undo(applied["operation"]["operation_id"], {"client_mutation_id": "undo"}, actor="dashboard:user")
    assert task_service.store.get("a").namespace_tags == ("work//draft",)


@pytest.mark.parametrize("action", ["rename", "assign"])
def test_ordinary_tag_collision_is_disclosed_and_refused(task_service, action):
    seed(task_service, "a", "old")
    with task_service.store.transaction() as conn:
        conn.execute("INSERT INTO task_tags VALUES('a','new',0)")
    organizer = TaskNamespaceService(task_service)
    request = {"action": "rename", "sources": ["old"], "name": "new"} if action == "rename" else {"action": "assign", "task_ids": ["a"], "assignment_mode": "add", "namespaces": ["new"]}
    preview = organizer.preview(request)
    assert not preview["can_apply"]
    assert any("ordinary tag 'new'" in issue for issue in preview["issues"])
    with pytest.raises(TaskValidationError):
        apply(organizer, request, preview=preview)
    assert Tag("new", False) in task_service.store.get("a").tags
    assert task_service.store.get("a").revision == 1


def test_namespace_undo_failure_rolls_back_all_domain_records(task_service, monkeypatch):
    seed(task_service, "a", "old")
    seed(task_service, "b", "old")
    organizer = TaskNamespaceService(task_service)
    result = apply(organizer, {"action": "rename", "sources": ["old"], "name": "new"})
    tables = ("task_tags", "task_metadata", "task_state_history", "task_event_outbox", "task_mutation_receipts", "task_namespace_operations", "task_system_state")
    def snapshot():
        with task_service.store.connect() as conn:
            return {table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)] for table in tables}
    before = snapshot()
    original = task_service._append_outbox
    def fail_second(conn, **kwargs):
        if kwargs["task"].task_id == "b":
            raise RuntimeError("simulated undo failure")
        return original(conn, **kwargs)
    monkeypatch.setattr(task_service, "_append_outbox", fail_second)
    with pytest.raises(RuntimeError, match="simulated undo"):
        organizer.undo(result["operation"]["operation_id"], {"client_mutation_id": "undo"}, actor="dashboard:user")
    assert snapshot() == before


def test_new_collision_invalidates_preview_and_all_project_links_survive(task_service):
    seed(task_service, "a", "old")
    with task_service.store.transaction() as conn:
        conn.executemany("INSERT INTO task_projects VALUES('a',?)", [(42,), (43,)])
        conn.execute("INSERT INTO task_project_unresolved(task_id,legacy_value,source_tag,reason,candidate_ids_json) VALUES('a','ambiguous','projects/ambiguous','ambiguous','[42,43]')")
    organizer = TaskNamespaceService(task_service)
    request = {"action": "rename", "sources": ["old"], "name": "new"}
    preview = organizer.preview(request)
    seed(task_service, "b", "new")
    with pytest.raises(NamespaceConflict):
        apply(organizer, request, preview=preview)
    before = task_service.store.get("a")
    result = apply(organizer, {**request, "merge_collisions": True})
    after = task_service.store.get("a")
    assert after.project_ids == before.project_ids == (42, 43)
    assert after.unresolved_projects == before.unresolved_projects
    organizer.undo(result["operation"]["operation_id"], {"client_mutation_id": "undo"}, actor="dashboard:user")
    assert task_service.store.get("a").unresolved_projects == before.unresolved_projects
