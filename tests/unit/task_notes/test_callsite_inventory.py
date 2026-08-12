from pathlib import Path


def test_production_task_note_content_calls_are_routed_through_adapter() -> None:
    root = Path(__file__).resolve().parents[3] / "work_buddy"
    owned = {
        root / "task_notes" / "adapter.py",
        root / "task_notes" / "migration.py",
    }
    production_consumers = (
        root / "obsidian" / "tasks" / "mutations.py",
        root / "obsidian" / "tasks" / "backfill_created_by.py",
        root / "obsidian" / "tasks" / "density_heuristic.py",
        root / "context" / "sources" / "tasks.py",
        root / "ir" / "sources" / "task_notes.py",
        root / "email" / "thread_actions.py",
    )
    forbidden = (
        "bridge.read_file(note_path)",
        "bridge.write_file(note_path",
        "read_file_raw(f\"tasks/notes/",
        "/ TASK_NOTES_DIR /",
        "Path(vault_root) / TASK_NOTES_DIR",
    )
    for path in production_consumers:
        assert path not in owned
        text = path.read_text(encoding="utf-8")
        assert "task_notes" in text, f"{path} does not use the task-note adapter"
        for token in forbidden:
            assert token not in text, f"{path} bypasses the task-note adapter: {token}"
