"""Characterization tests for work_buddy.journal_backlog.route — backlog routing.

These document the expected behavior of the consent-gated routing
primitives that translate user decisions into vault mutations.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.consent import grant_consent


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Set up a minimal Obsidian-style vault layout in a tmp dir."""
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "master-task-list.md").write_text(
        "# Master Task List\n\n", encoding="utf-8",
    )
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "considerations").mkdir()
    (tmp_path / "work" / "projects").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def grant_routing_consents() -> None:
    """The route.py public functions are consent-gated; grant for tests."""
    grant_consent("journal_backlog_create_task", mode="always")
    grant_consent("journal_backlog_create_consideration", mode="always")
    grant_consent("journal_backlog_append_to_note", mode="always")
    grant_consent("journal_backlog_execute_routing", mode="always")


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


def test_create_task_delegates_to_the_mutation_layer(vault: Path) -> None:
    """A journal-routed task goes through the WorkItem write path (Task.create
    → the task mutation layer), tagged agent-inferred. The mutation layer owns
    the real markdown + store write (covered by its own tests); here we assert
    the delegation + the preserved return shape, without a real write."""
    from work_buddy.journal_backlog.route import create_task
    from work_buddy.obsidian.tasks import mutations

    sentinel = {
        "success": True,
        "task_line": "- [ ] #todo Review tax return 🆔 t-x",
        "task_id": "t-x",
        "file": "tasks/master-task-list.md",
        "verified": {},
    }
    with patch.object(mutations, "create_task", return_value=sentinel) as m:
        result = create_task(
            task_text="Review tax return", vault_root=vault, urgency="high",
        )
    assert result["success"] is True
    assert result["task_id"] == "t-x"
    assert result["task_line"] == sentinel["task_line"]
    kwargs = m.call_args.kwargs
    assert kwargs["task_text"] == "Review tax return"
    assert kwargs["urgency"] == "high"
    # The journal path marks its tasks agent-inferred — the old direct-write
    # path left creation_provenance at the store default.
    assert kwargs["creation_provenance"] == "agent_inferred_from_journal"


def test_create_task_emits_a_work_item_event(tmp_path: Path, monkeypatch) -> None:
    """The reroute closes the audit gap the old direct-write path left open: a
    journal-routed task now produces a ``task.created`` WorkItem event. Runs the
    real mutation layer with the bridge faked and the store isolated, so the
    event fires without touching the real vault or store."""
    from work_buddy.journal_backlog.route import _create_task_impl
    from work_buddy.obsidian.tasks import mutations, store as task_store
    from work_buddy.threads import work_item_events

    monkeypatch.setattr(task_store, "_db_path", lambda: tmp_path / "tasks.sqlite")
    with patch("work_buddy.consent._cache") as cache, \
            patch.object(mutations, "bridge") as bridge:
        cache.is_granted.return_value = True
        cache.get_mode.return_value = "always"
        # A readable (empty) master list so create_task proceeds; the bridge
        # write is a no-op (no real vault touched). The store is the isolated
        # tmp DB monkeypatched above.
        bridge.read_file.return_value = "# Master Task List\n\n"
        bridge.write_file.return_value = True
        result = _create_task_impl(
            task_text="Pay quarterly taxes", vault_root=tmp_path, urgency="medium",
        )
    assert result["success"] is True
    kinds = [e["kind"] for e in work_item_events.list_events(result["task_id"])]
    assert "task.created" in kinds


# ---------------------------------------------------------------------------
# create_consideration
# ---------------------------------------------------------------------------


def test_create_consideration_writes_frontmatter_file(vault: Path) -> None:
    from work_buddy.journal_backlog.route import create_consideration

    result = create_consideration(
        title="Migrate to new ETF strategy",
        vault_root=vault,
        project="finance",
        type="consideration",
        body="Consider moving from VTI to VXUS for international exposure.",
    )
    assert result["success"] is True
    # Implementation places considerations at work/considerations/<slug>.md
    # (no project subdirectory). The project field is encoded in frontmatter.
    consideration_dir = vault / "work" / "considerations"
    files = list(consideration_dir.glob("*.md"))
    assert len(files) == 1
    contents = files[0].read_text(encoding="utf-8")
    # Frontmatter present
    assert contents.startswith("---")
    assert "type: consideration" in contents
    # Body present
    assert "Consider moving from VTI" in contents


# ---------------------------------------------------------------------------
# append_to_note
# ---------------------------------------------------------------------------


def test_append_to_note_rejects_path_traversal(vault: Path) -> None:
    """Security guard: paths that resolve outside the vault must be rejected."""
    from work_buddy.journal_backlog.route import append_to_note

    with pytest.raises(ValueError, match="Path traversal"):
        append_to_note(
            content="malicious",
            vault_root=vault,
            note_path="../../../etc/passwd.md",
        )


def test_append_to_note_appends_to_existing_note(vault: Path) -> None:
    from work_buddy.journal_backlog.route import append_to_note

    target = vault / "work" / "projects" / "notes.md"
    target.write_text("# Existing\n\nContent here.\n", encoding="utf-8")

    result = append_to_note(
        content="\nNew content appended.",
        vault_root=vault,
        note_path="work/projects/notes.md",
    )
    assert result["success"] is True
    final = target.read_text(encoding="utf-8")
    assert "Content here." in final
    assert "New content appended." in final


# ---------------------------------------------------------------------------
# execute_routing_plan
# ---------------------------------------------------------------------------


def test_execute_routing_plan_mixed_actions(vault: Path) -> None:
    from work_buddy.journal_backlog.route import execute_routing_plan
    from work_buddy.obsidian.tasks import mutations

    plan = [
        {
            "id": "t_0", "action": "route", "destination_type": "task",
            "task_text": "Do the thing",
        },
        {"id": "t_1", "action": "delete", "reason": "noise"},
        {"id": "t_2", "action": "skip"},
    ]
    with patch.object(
        mutations, "create_task",
        return_value={"success": True, "task_line": "x", "task_id": "t-x",
                      "file": "tasks/master-task-list.md"},
    ):
        result = execute_routing_plan(plan, vault_root=vault)
    assert result["success"] is True
    summary = result["summary"]
    assert summary["routed"] == 1
    assert summary["deleted"] == 1
    assert summary["skipped"] == 1
    assert summary["errors"] == 0


def test_execute_routing_plan_records_per_item_results(vault: Path) -> None:
    from work_buddy.journal_backlog.route import execute_routing_plan

    plan = [
        {"id": "t_0", "action": "skip"},
        {"id": "t_1", "action": "skip"},
    ]
    result = execute_routing_plan(plan, vault_root=vault)
    assert "results" in result
    assert len(result["results"]) == 2
    assert all(r.get("id") in {"t_0", "t_1"} for r in result["results"])


# ---------------------------------------------------------------------------
# Manifest helpers in segment.py (substrate-agnostic JSONL utilities)
# ---------------------------------------------------------------------------


def test_load_manifest_round_trip(tmp_path: Path) -> None:
    import json

    from work_buddy.journal_backlog.segment import load_manifest

    manifest = tmp_path / "m.jsonl"
    entries = [
        {"id": "t_0", "tags": ["a"], "summary": "first"},
        {"id": "t_1", "tags": ["b"], "summary": "second"},
    ]
    manifest.write_text(
        "\n".join(json.dumps(e) for e in entries), encoding="utf-8",
    )
    loaded = load_manifest(manifest)
    assert loaded == entries


def test_validate_manifest_detects_missing_thread(tmp_path: Path) -> None:
    import json

    from work_buddy.journal_backlog.segment import validate_manifest

    manifest = tmp_path / "m.jsonl"
    manifest.write_text(
        json.dumps({"id": "t_0", "tags": ["a"], "summary": "x"}),
        encoding="utf-8",
    )
    result = validate_manifest(manifest, thread_ids=["t_0", "t_1"])
    assert result["valid"] is False
    assert any("missing" in e.lower() for e in result["errors"])


def test_validate_manifest_passes_on_complete(tmp_path: Path) -> None:
    import json

    from work_buddy.journal_backlog.segment import validate_manifest

    manifest = tmp_path / "m.jsonl"
    manifest.write_text(
        "\n".join(json.dumps(e) for e in [
            {"id": "t_0", "tags": ["a"], "summary": "x"},
            {"id": "t_1", "tags": ["b"], "summary": "y"},
        ]),
        encoding="utf-8",
    )
    result = validate_manifest(manifest, thread_ids=["t_0", "t_1"])
    assert result["valid"] is True


def test_generate_review_doc_groups_by_primary_tag() -> None:
    from work_buddy.journal_backlog.segment import generate_review_doc

    threads = [
        {"id": "t_0", "raw_text": "first", "line_count": 1,
         "source_dates": [], "has_multi_flag": False, "lines": [1]},
        {"id": "t_1", "raw_text": "second", "line_count": 1,
         "source_dates": [], "has_multi_flag": False, "lines": [2]},
    ]
    manifest = [
        {"id": "t_0", "tags": ["alpha"], "summary": "First."},
        {"id": "t_1", "tags": ["alpha"], "summary": "Second."},
    ]
    md = generate_review_doc(
        threads, manifest, journal_date="2026-04-24", source_dates=[],
    )
    assert "## alpha" in md
    assert "t_0" in md and "t_1" in md
