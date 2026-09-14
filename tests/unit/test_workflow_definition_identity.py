"""Invariants for stable Workflow-definition identity.

These tests keep definition IDs, executable aliases, compiled revisions, and
individual run IDs distinct across registry lookup and persisted DAG state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.frontmatter import parse_frontmatter
from work_buddy.knowledge.model import WorkflowUnit
from work_buddy.mcp_server import registry
from work_buddy.mcp_server.registry import WorkflowDefinition
from work_buddy.mcp_server.registry import WorkflowStep
from work_buddy.workflow import WorkflowDAG
from work_buddy.workflows.identity import (
    WORKFLOW_REVISION_PATTERN,
    compute_workflow_revision,
    derived_workflow_id,
    is_valid_workflow_id,
    new_workflow_id,
    require_workflow_id,
)


def _definition(
    name: str,
    workflow_id: str,
    *,
    aliases: tuple[str, ...] = (),
) -> WorkflowDefinition:
    return WorkflowDefinition(
        name=name,
        description=f"{name} workflow",
        workflow_file=f"store:test/{name}",
        execution="main",
        workflow_id=workflow_id,
        workflow_revision="sha256:" + "a" * 64,
        display_name=name.replace("-", " ").title(),
        aliases=aliases,
    )


def _unit(
    *,
    path: str,
    workflow_name: str,
    workflow_id: str,
    workflow_aliases: list[str] | None = None,
    steps: list[dict[str, object]] | None = None,
) -> WorkflowUnit:
    return WorkflowUnit(
        path=path,
        name=workflow_name.replace("-", " ").title(),
        description=f"{workflow_name} workflow",
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        workflow_aliases=workflow_aliases or [],
        steps=steps
        or [
            {
                "id": "execute",
                "name": "Execute",
                "step_type": "reasoning",
            }
        ],
    )


def test_definition_id_helpers_use_a_separate_stable_namespace() -> None:
    generated = new_workflow_id()

    assert is_valid_workflow_id(generated)
    assert generated.startswith("wfd_")
    assert not generated.startswith("wf_")
    assert require_workflow_id(generated) == generated

    first = derived_workflow_id("renamed-later")
    assert first == derived_workflow_id("renamed-later")
    assert first != derived_workflow_id("another-workflow")
    assert is_valid_workflow_id(first)

    for invalid in (None, "", "wf_run123", "wfd_ABC", "wfd_" + "0" * 31):
        assert not is_valid_workflow_id(invalid)
        with pytest.raises(ValueError, match="workflow_id must match"):
            require_workflow_id(invalid)


def test_revision_is_deterministic_path_independent_and_behavior_sensitive() -> None:
    authored = {
        "workflow_id": "wfd_" + "1" * 32,
        "workflow_name": "compile-report",
        "path": "old/location",
        "parents": ["old/parent"],
        "steps": [
            {
                "id": "compile",
                "auto_run": {
                    "callable": "work_buddy.reports.compile",
                    "kwargs": {"path": "runtime/input-a.json", "format": "md"},
                },
            }
        ],
    }
    moved = {
        **authored,
        "path": "new/location",
        "parents": ["new/parent"],
    }
    reordered = {
        "steps": authored["steps"],
        "path": authored["path"],
        "parents": authored["parents"],
        "workflow_name": authored["workflow_name"],
        "workflow_id": authored["workflow_id"],
    }
    changed_runtime_input = {
        **authored,
        "steps": [
            {
                "id": "compile",
                "auto_run": {
                    "callable": "work_buddy.reports.compile",
                    "kwargs": {"path": "runtime/input-b.json", "format": "md"},
                },
            }
        ],
    }

    revision = compute_workflow_revision(
        authored,
        bound_instructions={
            "path": "directions/old-location",
            "parents": ["directions/old-parent"],
            "workflow": "old/location",
            "content": "Compile the bounded report.",
        },
    )
    moved_revision = compute_workflow_revision(
        moved,
        bound_instructions={
            "path": "directions/new-location",
            "parents": ["directions/new-parent"],
            "workflow": "new/location",
            "content": "Compile the bounded report.",
        },
    )

    assert WORKFLOW_REVISION_PATTERN.fullmatch(revision)
    assert revision == moved_revision
    assert revision == compute_workflow_revision(
        reordered,
        bound_instructions={"content": "Compile the bounded report."},
    )
    assert revision != compute_workflow_revision(
        changed_runtime_input,
        bound_instructions={"content": "Compile the bounded report."},
    )
    assert revision != compute_workflow_revision(
        authored,
        bound_instructions={"content": "Compile a different report."},
    )


def test_every_authored_workflow_has_a_unique_valid_definition_id() -> None:
    store_root = Path(__file__).resolve().parents[2] / "knowledge" / "store"
    authored: list[tuple[Path, str]] = []

    for path in sorted(store_root.rglob("*.md")):
        frontmatter, _ = parse_frontmatter(path)
        if frontmatter.get("kind") == "workflow":
            authored.append((path, frontmatter.get("workflow_id", "")))

    assert authored, "expected at least one authored Workflow definition"
    invalid = [str(path.relative_to(store_root)) for path, value in authored if not is_valid_workflow_id(value)]
    assert not invalid, f"authored Workflows with invalid/missing workflow_id: {invalid}"

    by_id: dict[str, list[str]] = {}
    for path, value in authored:
        by_id.setdefault(value, []).append(str(path.relative_to(store_root)))
    duplicates = {value: paths for value, paths in by_id.items() if len(paths) > 1}
    assert not duplicates, f"duplicate authored workflow_id values: {duplicates}"


def test_registry_resolves_and_searches_definition_by_id_and_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_id = "wfd_" + "2" * 32
    workflow = _definition(
        "current-name",
        workflow_id,
        aliases=("former-name",),
    )
    by_id, by_alias = registry._build_workflow_indexes([workflow])
    monkeypatch.setattr(registry, "_REGISTRY", {workflow.name: workflow})
    monkeypatch.setattr(registry, "_WORKFLOW_ID_INDEX", by_id)
    monkeypatch.setattr(registry, "_WORKFLOW_ALIAS_INDEX", by_alias)

    assert registry.get_entry(workflow_id) is workflow
    assert registry.get_entry("former-name") is workflow

    for address in (workflow_id, "former-name"):
        assert registry.search_registry(address) == [
            {
                "name": "current-name",
                "display_name": "Current Name",
                "description": "current-name workflow",
                "category": "workflow",
                "type": "workflow",
                "parameters": {},
                "workflow_id": workflow_id,
                "workflow_revision": "sha256:" + "a" * 64,
                "execution": "main",
                "steps": [],
                "aliases": ["former-name"],
                "search_score": 1.0,
            }
        ]


def test_workflow_index_builder_rejects_identity_and_alias_collisions() -> None:
    shared_id = "wfd_" + "3" * 32
    first = _definition("first", shared_id, aliases=("former-name",))

    with pytest.raises(ValueError, match="duplicate workflow_id"):
        registry._build_workflow_indexes(
            [first, _definition("second", shared_id)]
        )

    with pytest.raises(ValueError, match="duplicate workflow alias"):
        registry._build_workflow_indexes(
            [
                first,
                _definition(
                    "second",
                    "wfd_" + "4" * 32,
                    aliases=("former-name",),
                ),
            ]
        )

    with pytest.raises(ValueError, match="reserved stable-ID namespace"):
        registry._build_workflow_indexes(
            [
                _definition(
                    "first",
                    shared_id,
                    aliases=("wfd_" + "5" * 32,),
                )
            ]
        )


def test_workflow_ref_is_compiled_to_the_target_definition_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_id = "wfd_" + "6" * 32
    parent_id = "wfd_" + "7" * 32
    child = _unit(
        path="test/child",
        workflow_name="child-current",
        workflow_id=child_id,
        workflow_aliases=["child-former"],
    )
    parent = _unit(
        path="test/parent",
        workflow_name="parent",
        workflow_id=parent_id,
        steps=[
            {
                "id": "delegate",
                "name": "Delegate",
                "step_type": "reasoning",
                "workflow_ref": "child-former",
            }
        ],
    )
    monkeypatch.setattr(
        "work_buddy.knowledge.store.load_store",
        lambda: {child.path: child, parent.path: parent},
    )

    definitions = registry._discover_workflows_from_store()
    compiled_parent = next(item for item in definitions if item.name == "parent")

    assert compiled_parent.steps[0].workflow_file == "child-former"
    assert compiled_parent.steps[0].target_workflow_id == child_id


def test_parent_revision_changes_when_alias_resolves_to_a_different_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_alias = "child-former"
    first_child = _unit(
        path="test/first-child",
        workflow_name="first-child",
        workflow_id="wfd_" + "4" * 32,
        workflow_aliases=[shared_alias],
    )
    second_child = _unit(
        path="test/second-child",
        workflow_name="second-child",
        workflow_id="wfd_" + "5" * 32,
    )
    parent = _unit(
        path="test/parent",
        workflow_name="parent",
        workflow_id="wfd_" + "6" * 32,
        steps=[{
            "id": "delegate",
            "name": "Delegate",
            "step_type": "reasoning",
            "workflow_ref": shared_alias,
        }],
    )
    current_store = {
        first_child.path: first_child,
        second_child.path: second_child,
        parent.path: parent,
    }
    monkeypatch.setattr(
        "work_buddy.knowledge.store.load_store",
        lambda: current_store,
    )

    first_definition = next(
        item for item in registry._discover_workflows_from_store()
        if item.name == "parent"
    )

    first_child.workflow_aliases = []
    second_child.workflow_aliases = [shared_alias]
    second_definition = next(
        item for item in registry._discover_workflows_from_store()
        if item.name == "parent"
    )

    assert first_definition.steps[0].target_workflow_id == first_child.workflow_id
    assert second_definition.steps[0].target_workflow_id == second_child.workflow_id
    assert first_definition.workflow_revision != second_definition.workflow_revision


def test_dependency_compilation_follows_workflow_refs_and_preserves_optionality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _definition("child", "wfd_" + "a" * 32)
    child.steps = [
        WorkflowStep(
            id="required",
            name="Required",
            instruction="Run the required step.",
            step_type="reasoning",
            requires=["required-component"],
        ),
        WorkflowStep(
            id="optional",
            name="Optional",
            instruction="Run the optional step when available.",
            step_type="reasoning",
            optional=True,
            requires=["optional-component"],
        ),
    ]
    parent = _definition("parent", "wfd_" + "b" * 32)
    parent.steps = [
        WorkflowStep(
            id="child",
            name="Child",
            instruction="Invoke the child Workflow.",
            step_type="reasoning",
            workflow_file=child.name,
            target_workflow_id=child.workflow_id,
        )
    ]
    monkeypatch.setattr(registry, "_DISABLED_SKILL_REGISTRY", {})

    registry._compute_workflow_requires({child.name: child, parent.name: parent})

    assert parent.requires == ["optional-component", "required-component"]
    assert parent.required_components == ["required-component"]


def test_dag_identity_round_trips_without_conflating_the_run_id(
    tmp_path: Path,
) -> None:
    target = tmp_path / "workflow.json"
    dag = WorkflowDAG(
        "compile-report:wf_run-a",
        "Compile report",
        workflow_id="wfd_" + "8" * 32,
        workflow_revision="sha256:" + "b" * 64,
        workflow_run_id="wf_run-a",
        workflow_name="compile-report",
    )
    dag.add_task("compile", "Compile")
    dag._loaded_from = target

    assert dag.save() == target
    loaded = WorkflowDAG.load(target)

    assert loaded.workflow_id == "wfd_" + "8" * 32
    assert loaded.workflow_revision == "sha256:" + "b" * 64
    assert loaded.workflow_run_id == "wf_run-a"
    assert loaded.workflow_name == "compile-report"
    assert loaded.name == "compile-report:wf_run-a"


def test_two_runs_pin_one_definition_identity_and_distinct_run_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy import consent
    from work_buddy.mcp_server import conductor

    workflow_id = "wfd_" + "9" * 32
    revision = "sha256:" + "c" * 64
    definition = _definition("repeatable-flow", workflow_id)
    definition.workflow_revision = revision
    definition.steps = [
        WorkflowStep(
            id="review",
            name="Review",
            instruction="Review the result.",
            step_type="reasoning",
        )
    ]
    monkeypatch.setattr(
        conductor,
        "get_entry",
        lambda address: definition
        if address in {definition.name, definition.workflow_id}
        else None,
    )
    monkeypatch.setattr(WorkflowDAG, "save", lambda self: Path("workflow.json"))
    monkeypatch.setattr(consent, "grant_workflow_run", lambda *_args, **_kwargs: None)
    with conductor._ACTIVE_RUNS_LOCK:
        conductor._ACTIVE_RUNS.clear()

    first = conductor.start_workflow(definition.workflow_id)
    second = conductor.start_workflow(definition.name)

    assert first["workflow_id"] == second["workflow_id"] == workflow_id
    assert first["workflow_revision"] == second["workflow_revision"] == revision
    assert first["workflow_run_id"] != second["workflow_run_id"]
    assert first["workflow_run_id"].startswith("wf_")
    assert second["workflow_run_id"].startswith("wf_")
    with conductor._ACTIVE_RUNS_LOCK:
        conductor._ACTIVE_RUNS.clear()


def test_advance_validation_error_preserves_definition_identity() -> None:
    from work_buddy.mcp_server import conductor

    run_id = "wf_schema1"
    workflow_id = "wfd_" + "a" * 32
    revision = "sha256:" + "d" * 64
    dag = WorkflowDAG(
        f"validated-flow:{run_id}",
        workflow_id=workflow_id,
        workflow_revision=revision,
        workflow_run_id=run_id,
        workflow_name="validated-flow",
    )
    dag.add_task(
        "review",
        "Review",
        metadata={"result_schema": {"required_keys": ["approved"]}},
    )
    dag.start_task("review")
    with conductor._ACTIVE_RUNS_LOCK:
        conductor._ACTIVE_RUNS[run_id] = dag
    try:
        result = conductor.advance_workflow(run_id, {})
    finally:
        with conductor._ACTIVE_RUNS_LOCK:
            conductor._ACTIVE_RUNS.pop(run_id, None)

    assert result["type"] == "validation_error"
    assert result["workflow_run_id"] == run_id
    assert result["workflow_id"] == workflow_id
    assert result["workflow_revision"] == revision
    assert result["workflow_name"] == "validated-flow"


def test_dag_loads_the_prior_persisted_shape(tmp_path: Path) -> None:
    target = tmp_path / "prior-workflow.json"
    target.write_text(
        json.dumps(
            {
                "name": "prior-name:wf_prior-run",
                "description": "Previously persisted workflow",
                "created_at": "2026-01-01T00:00:00+00:00",
                "nodes": {},
                "edges": [],
            }
        ),
        encoding="utf-8",
    )

    loaded = WorkflowDAG.load(target)

    assert loaded.workflow_id is None
    assert loaded.workflow_revision is None
    assert loaded.workflow_run_id == "wf_prior-run"
    assert loaded.workflow_name == "prior-name"
    assert loaded.name == "prior-name:wf_prior-run"


def test_definition_lookup_falls_back_to_saved_slug_when_id_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.mcp_server import conductor

    definition = _definition("saved-name", "wfd_" + "d" * 32)
    addresses: list[str] = []

    def resolve(address: str) -> WorkflowDefinition | None:
        addresses.append(address)
        return definition if address == "saved-name" else None

    monkeypatch.setattr(conductor, "get_entry", resolve)
    dag = WorkflowDAG(
        "saved-name:wf_old-run",
        "Previously persisted workflow",
        workflow_id="wfd_" + "e" * 32,
        workflow_run_id="wf_old-run",
        workflow_name="saved-name",
    )

    assert conductor._get_wf_def(dag) is definition
    assert addresses == ["wfd_" + "e" * 32, "saved-name"]
