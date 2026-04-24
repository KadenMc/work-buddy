"""Characterization tests for work_buddy.journal_backlog.route — backlog routing.

These document the expected behavior of the consent-gated routing
primitives that translate user decisions into vault mutations.
"""

from __future__ import annotations

from pathlib import Path

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


def test_create_task_appends_line_to_master_list(vault: Path) -> None:
    from work_buddy.journal_backlog.route import create_task

    result = create_task(
        task_text="Review tax return", vault_root=vault, urgency="high",
    )
    assert result["success"] is True
    contents = (vault / "tasks" / "master-task-list.md").read_text(encoding="utf-8")
    assert "Review tax return" in contents


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

    plan = [
        {
            "id": "t_0", "action": "route", "destination_type": "task",
            "task_text": "Do the thing",
        },
        {"id": "t_1", "action": "delete", "reason": "noise"},
        {"id": "t_2", "action": "skip"},
    ]
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
