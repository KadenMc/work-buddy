"""Compatibility checks for persistent user-authored knowledge overlays."""

from work_buddy.knowledge.model import DirectionsUnit, SkillUnit, unit_from_dict
from work_buddy.knowledge.store import _deep_merge, _normalize_local_unit_input


def test_capability_shaped_local_unit_loads_as_canonical_skill() -> None:
    persisted = {
        "kind": "capability",
        "name": "Task create",
        "description": "Create a task",
        "capability_name": "task_create",
        "category": "tasks",
        "schema_version": "wb-capability/v1",
    }

    unit = unit_from_dict("tasks/create", _normalize_local_unit_input(persisted))

    assert isinstance(unit, SkillUnit)
    assert unit.skill_name == "task_create"
    serialized = unit.to_dict()
    assert serialized["kind"] == "skill"
    assert serialized["skill_name"] == "task_create"
    assert serialized["schema_version"] == "wb-skill/v1"
    assert "capability_name" not in serialized


def test_old_local_field_overrides_corresponding_canonical_base_field() -> None:
    base = {
        "kind": "skill",
        "name": "Task create",
        "description": "Create a task",
        "skill_name": "task_create",
        "category": "tasks",
    }
    patch = _normalize_local_unit_input({"capability_name": "custom_task_create"})

    unit = unit_from_dict("tasks/create", _deep_merge(base, patch))

    assert isinstance(unit, SkillUnit)
    assert unit.skill_name == "custom_task_create"


def test_local_directions_capabilities_load_and_serialize_as_skills() -> None:
    persisted = {
        "kind": "directions",
        "name": "New task",
        "description": "Create a task",
        "capabilities": ["task_create"],
    }

    unit = unit_from_dict("tasks/new", _normalize_local_unit_input(persisted))

    assert isinstance(unit, DirectionsUnit)
    assert unit.skills == ["task_create"]
    serialized = unit.to_dict()
    assert serialized["skills"] == ["task_create"]
    assert "capabilities" not in serialized


def test_canonical_local_fields_win_when_both_forms_are_present() -> None:
    normalized = _normalize_local_unit_input({
        "skill_name": "canonical_name",
        "capability_name": "older_name",
        "skills": ["canonical_skill"],
        "capabilities": ["older_skill"],
    })

    assert normalized["skill_name"] == "canonical_name"
    assert normalized["skills"] == ["canonical_skill"]
    assert "capability_name" not in normalized
    assert "capabilities" not in normalized
