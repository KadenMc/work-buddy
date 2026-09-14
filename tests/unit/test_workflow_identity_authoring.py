"""Workflow identity survives every supported knowledge-store authoring path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.knowledge import editor
from work_buddy.knowledge import file_store
from work_buddy.knowledge import store as store_module
from work_buddy.workflows.identity import is_valid_workflow_id


@pytest.fixture
def authored_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store_dir = tmp_path / "store"
    local_dir = tmp_path / "store.local"
    store_dir.mkdir()
    local_dir.mkdir()
    monkeypatch.setattr(editor, "_STORE_DIR", store_dir)
    monkeypatch.setattr(store_module, "_STORE_DIR", store_dir)
    monkeypatch.setattr(store_module, "_LOCAL_DIR", local_dir)
    store_module.invalidate_store()
    yield store_dir
    store_module.invalidate_store()


def _workflow(workflow_id: str, workflow_name: str = "original-flow") -> dict:
    return {
        "kind": "workflow",
        "name": "Original Flow",
        "description": "A test Workflow.",
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "execution": "main",
        "steps": [
            {
                "id": "review",
                "name": "Review",
                "step_type": "reasoning",
                "depends_on": [],
            }
        ],
        "step_instructions": {"review": "Review the result."},
    }


def test_workflow_codec_round_trips_definition_id_and_addresses() -> None:
    original = _workflow("wfd_11111111111111111111111111111111")
    original["workflow_aliases"] = ["former-flow", "historic-flow"]

    encoded = file_store.unit_dict_to_markdown(original)
    decoded = file_store.markdown_to_unit_dict(encoded)

    assert decoded["workflow_id"] == original["workflow_id"]
    assert decoded["workflow_name"] == "original-flow"
    assert decoded["workflow_aliases"] == ["former-flow", "historic-flow"]


def test_create_assigns_identity_and_rejects_invalid_explicit_id(
    authored_store: Path,
) -> None:
    created = editor.create_unit(
        path="test/created-flow",
        kind="workflow",
        name="Created Flow",
        description="Create a stable Workflow.",
        extra={"steps": []},
    )

    assert created["status"] == "created"
    raw = file_store.read_unit(authored_store, "test/created-flow")
    assert raw is not None
    assert is_valid_workflow_id(raw["workflow_id"])
    assert raw["workflow_name"] == "created-flow"

    rejected = editor.create_unit(
        path="test/invalid-flow",
        kind="workflow",
        name="Invalid Flow",
        description="Reject invalid identity.",
        extra={"workflow_id": "wf_not-a-definition-id"},
    )
    assert rejected["error"] == "invalid_workflow_id"
    assert file_store.read_unit(authored_store, "test/invalid-flow") is None


def test_rename_keeps_id_and_retains_old_name_with_explicit_alias_update(
    authored_store: Path,
) -> None:
    workflow_id = "wfd_22222222222222222222222222222222"
    file_store.write_unit(
        authored_store,
        "test/original-flow",
        _workflow(workflow_id),
    )
    store_module.invalidate_store()

    result = editor.update_unit(
        "test/original-flow",
        {
            "workflow_name": "renamed-flow",
            "workflow_aliases": ["another-address"],
        },
    )

    assert result["status"] == "updated"
    raw = file_store.read_unit(authored_store, "test/original-flow")
    assert raw is not None
    assert raw["workflow_id"] == workflow_id
    assert raw["workflow_name"] == "renamed-flow"
    assert raw["workflow_aliases"] == ["another-address", "original-flow"]

    rejected = editor.update_unit(
        "test/original-flow",
        {"workflow_id": "wfd_33333333333333333333333333333333"},
    )
    assert rejected["error"] == "workflow_identity_immutable"
    assert file_store.read_unit(authored_store, "test/original-flow")[
        "workflow_id"
    ] == workflow_id


def test_move_keeps_id_and_rewrites_bound_directions_path(
    authored_store: Path,
) -> None:
    workflow_id = "wfd_44444444444444444444444444444444"
    file_store.write_unit(
        authored_store,
        "test/original-flow",
        _workflow(workflow_id),
    )
    file_store.write_unit(
        authored_store,
        "test/flow-directions",
        {
            "kind": "directions",
            "name": "Flow Directions",
            "description": "Directions bound to the Workflow.",
            "trigger": "When the Workflow runs.",
            "workflow": "test/original-flow",
            "content": {"full": "Review the result carefully."},
        },
    )
    store_module.invalidate_store()

    result = editor.move_unit("test/original-flow", "moved/renamed-file")

    assert result["status"] == "moved"
    moved = file_store.read_unit(authored_store, "moved/renamed-file")
    directions = file_store.read_unit(authored_store, "test/flow-directions")
    assert moved is not None
    assert directions is not None
    assert moved["workflow_id"] == workflow_id
    assert directions["workflow"] == "moved/renamed-file"


def test_local_overlay_cannot_replace_tracked_workflow_identity(
    authored_store: Path,
    tmp_path: Path,
) -> None:
    tracked_id = "wfd_55555555555555555555555555555555"
    file_store.write_unit(
        authored_store,
        "test/tracked-flow",
        _workflow(tracked_id, workflow_name="tracked-flow"),
    )
    overlay_file = tmp_path / "store.local" / "customizations.json"
    overlay_file.write_text(
        json.dumps({
            "test/tracked-flow": {
                "workflow_id": "wfd_66666666666666666666666666666666",
                "description": "Locally customized description.",
            }
        }),
        encoding="utf-8",
    )
    store_module.invalidate_store()

    loaded = store_module.load_store(force=True)["test/tracked-flow"]

    assert loaded.workflow_id == tracked_id
    assert loaded.description == "Locally customized description."
