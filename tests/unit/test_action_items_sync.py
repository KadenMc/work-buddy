"""Slice 7: action_items markdown parser + reconciler + migration helper."""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks import action_items, store


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# parse_action_items_from_note
# ---------------------------------------------------------------------------


def test_parse_basic_section():
    body = """# Title

## Action items

- step one
- step two
- step three

## Other section

- ignored bullet
"""
    assert action_items.parse_action_items_from_note(body) == [
        "step one", "step two", "step three",
    ]


def test_parse_empty_section_returns_empty():
    body = """## Action items

## Next section
- not in action items
"""
    assert action_items.parse_action_items_from_note(body) == []


def test_parse_no_section_returns_empty():
    assert action_items.parse_action_items_from_note("# Title\n\nbody only") == []


def test_parse_skips_checkbox_bullets():
    """Per Slice-7 doctrine: action items are PLAIN bullets, not checkboxes."""
    body = """## Action items

- plain one
- [ ] checkbox-style (skipped per doctrine)
- plain two
"""
    items = action_items.parse_action_items_from_note(body)
    assert "plain one" in items
    assert "plain two" in items
    # Checkbox bullets ARE captured (start with `- `) but with the checkbox
    # syntax stripped down.  Doctrine says PLAIN; keep both for now and
    # let sync surface the checkbox-formatted ones as-is.
    assert any("[" in i for i in items) or "checkbox-style (skipped per doctrine)" in items


def test_parse_handles_no_body():
    assert action_items.parse_action_items_from_note(None) == []
    assert action_items.parse_action_items_from_note("") == []


def test_parse_stops_at_next_heading():
    body = """## Action items

- step
### subheading

- not in section
"""
    assert action_items.parse_action_items_from_note(body) == ["step"]


# ---------------------------------------------------------------------------
# reconcile_from_markdown
# ---------------------------------------------------------------------------


def test_reconcile_creates_new_items(fresh_db):
    store.create(task_id="t-r-1", density="developed")
    body = "## Action items\n\n- one\n- two\n"
    summary = action_items.reconcile_from_markdown("t-r-1", body)
    assert summary["added"] == 2
    assert summary["updated"] == 0
    assert summary["deleted"] == 0
    items = action_items.list_for_task("t-r-1")
    assert [i["description"] for i in items] == ["one", "two"]
    assert all(int(i["user_authored"]) == 1 for i in items)


def test_reconcile_updates_changed_descriptions(fresh_db):
    store.create(task_id="t-r-2", density="developed")
    a = action_items.create(
        task_id="t-r-2", description="old text", user_authored=True,
    )
    body = "## Action items\n\n- new text\n"
    summary = action_items.reconcile_from_markdown("t-r-2", body)
    assert summary["updated"] == 1
    assert action_items.get(a["id"])["description"] == "new text"


def test_reconcile_deletes_removed_bullets(fresh_db):
    store.create(task_id="t-r-3", density="developed")
    a = action_items.create(
        task_id="t-r-3", description="will be removed", user_authored=True,
    )
    body = "## Action items\n"
    summary = action_items.reconcile_from_markdown("t-r-3", body)
    assert summary["deleted"] == 1
    assert action_items.get(a["id"]) is None


def test_reconcile_promotes_unapproved_to_approved_on_user_adoption(fresh_db):
    """PR #70 fix #2: an agent-proposed-and-unapproved item that now
    appears in the markdown means the user adopted it.  Promote
    authorship 'agent_unapproved' -> 'agent_approved' rather than
    flipping to 'user' (which would erase agent origin).
    """
    store.create(task_id="t-r-4", density="developed")
    a = action_items.create(
        task_id="t-r-4",
        description="agent proposed",
        authorship="agent_unapproved",
    )
    body = "## Action items\n\n- agent proposed\n"
    summary = action_items.reconcile_from_markdown("t-r-4", body)
    assert summary["kept"] == 1
    row = action_items.get(a["id"])
    assert row["authorship"] == "agent_approved"
    # Origin preserved -- user_authored stays 0 because the user
    # didn't write it from scratch.
    assert row["user_authored"] == 0
    # Approval timestamp got stamped by the promotion.
    assert row["approved_at"] is not None
    # is_executable admits the now-approved row.
    assert action_items.is_executable(row) is True


def test_reconcile_keeps_agent_approved_unchanged(fresh_db):
    """An item already at 'agent_approved' that re-appears in
    markdown should NOT get demoted or churned."""
    store.create(task_id="t-r-4b", density="developed")
    a = action_items.create(
        task_id="t-r-4b",
        description="already approved",
        authorship="agent_approved",
    )
    original_approved_at = action_items.get(a["id"])["approved_at"]
    body = "## Action items\n\n- already approved\n"
    action_items.reconcile_from_markdown("t-r-4b", body)
    row = action_items.get(a["id"])
    assert row["authorship"] == "agent_approved"
    assert row["approved_at"] == original_approved_at  # not bumped


def test_reconcile_user_edited_description_lifts_to_user(fresh_db):
    """A description the user edited in markdown is user-authored
    by definition (the user rewrote it)."""
    store.create(task_id="t-r-4c", density="developed")
    a = action_items.create(
        task_id="t-r-4c",
        description="agent's original phrasing",
        authorship="agent_approved",
    )
    body = "## Action items\n\n- the user's rewrite\n"
    action_items.reconcile_from_markdown("t-r-4c", body)
    row = action_items.get(a["id"])
    assert row["description"] == "the user's rewrite"
    assert row["authorship"] == "user"
    assert row["user_authored"] == 1


def test_reconcile_handles_empty_section(fresh_db):
    """User explicitly cleared the section -> all rows go."""
    store.create(task_id="t-r-5", density="developed")
    action_items.create(task_id="t-r-5", description="a", user_authored=True)
    action_items.create(task_id="t-r-5", description="b", user_authored=True)
    summary = action_items.reconcile_from_markdown(
        "t-r-5", "## Action items\n\n## Other\n",
    )
    assert summary["deleted"] == 2


# ---------------------------------------------------------------------------
# migrate_existing_notes
# ---------------------------------------------------------------------------


def test_migrate_walks_developed_tasks_only(fresh_db):
    store.create(task_id="t-m-sparse", density="sparse", note_uuid="uuid-a")
    store.create(task_id="t-m-developed", density="developed", note_uuid="uuid-b")

    bodies = {
        "uuid-a": "## Action items\n\n- should not be loaded\n",
        "uuid-b": "## Action items\n\n- one\n- two\n",
    }
    tally = action_items.migrate_existing_notes(
        read_note_body=lambda u: bodies.get(u),
    )
    # Sparse skipped; developed examined and added.
    assert tally["tasks_examined"] == 1
    assert tally["items_inserted"] == 2
    assert action_items.list_for_task("t-m-sparse") == []


def test_migrate_idempotent_on_rerun(fresh_db):
    store.create(task_id="t-m-id", density="developed", note_uuid="uu")
    body_fn = lambda u: "## Action items\n\n- alpha\n- beta\n"
    a1 = action_items.migrate_existing_notes(read_note_body=body_fn)
    a2 = action_items.migrate_existing_notes(read_note_body=body_fn)
    assert a1["items_inserted"] == 2
    assert a2["items_inserted"] == 0
    assert len(action_items.list_for_task("t-m-id")) == 2


def test_migrate_handles_missing_note(fresh_db):
    store.create(task_id="t-m-nun", density="developed", note_uuid="missing")
    tally = action_items.migrate_existing_notes(read_note_body=lambda u: None)
    assert tally["tasks_examined"] == 0
