"""Unit tests for the morning routine workflow structure and phase resolution.

Covers the split of the former monolithic ``plan-today`` step into three
separate DAG steps (``propose-mits``, ``persist-briefing``, ``day-planner``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.morning import (
    _ALWAYS_ON,
    _PHASE_MAP,
    is_phase_enabled,
    resolve_phases,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_JSON = REPO_ROOT / "knowledge" / "store" / "workflows.json"
MORNING_JSON = REPO_ROOT / "knowledge" / "store" / "morning.json"


@pytest.fixture(scope="module")
def morning_workflow() -> dict:
    """Load the morning-routine workflow definition from the knowledge store."""
    with WORKFLOWS_JSON.open(encoding="utf-8") as f:
        data = json.load(f)
    return data["morning/morning-routine"]


@pytest.fixture(scope="module")
def morning_docs() -> dict:
    """Load the morning directions docs unit."""
    with MORNING_JSON.open(encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# morning.py phase resolution
# ---------------------------------------------------------------------------


class TestAlwaysOnSet:
    """The _ALWAYS_ON set controls which phases cannot be config-disabled."""

    def test_contains_sign_in(self):
        assert "sign-in" in _ALWAYS_ON

    def test_contains_synthesize(self):
        assert "synthesize" in _ALWAYS_ON

    def test_contains_propose_mits(self):
        assert "propose-mits" in _ALWAYS_ON

    def test_contains_persist_briefing(self):
        assert "persist-briefing" in _ALWAYS_ON

    def test_contains_day_planner(self):
        assert "day-planner" in _ALWAYS_ON

    def test_does_not_contain_plan_today(self):
        """The old monolithic step has been removed."""
        assert "plan-today" not in _ALWAYS_ON

    def test_exactly_five_always_on(self):
        """Exactly the expected 5 always-on steps — no drift."""
        assert _ALWAYS_ON == {
            "sign-in",
            "synthesize",
            "propose-mits",
            "persist-briefing",
            "day-planner",
        }


class TestIsPhaseEnabled:
    """is_phase_enabled returns True for always-on steps regardless of config."""

    def _cfg(self, **phases) -> dict:
        return {"morning": {"phases": phases}}

    def test_propose_mits_always_enabled_even_with_empty_config(self):
        assert is_phase_enabled("propose-mits", self._cfg()) is True

    def test_persist_briefing_always_enabled_even_with_empty_config(self):
        assert is_phase_enabled("persist-briefing", self._cfg()) is True

    def test_day_planner_always_enabled_even_with_empty_config(self):
        """Day-planner is always-on at the phase level. Its OWN config flag
        (morning.day_planner.enabled) gates execution inside the step."""
        assert is_phase_enabled("day-planner", self._cfg()) is True

    def test_plan_today_is_unknown_phase_now(self):
        """plan-today is not a real step anymore. The function defaults to True
        for unknown phases (with a warning), so this is still truthy — but it
        must NOT be treated as part of _ALWAYS_ON."""
        assert "plan-today" not in _ALWAYS_ON

    def test_optional_phase_disabled_via_config(self):
        """Sanity check: optional phases still obey the config flag."""
        cfg = self._cfg(blindspot_scan=False)
        assert is_phase_enabled("blindspot-scan", cfg) is False


class TestResolvePhases:
    """resolve_phases returns the full step_id -> enabled map for all phases."""

    def test_includes_propose_mits(self):
        result = resolve_phases({"morning": {"phases": {}}})
        assert result["propose-mits"] is True

    def test_includes_persist_briefing(self):
        result = resolve_phases({"morning": {"phases": {}}})
        assert result["persist-briefing"] is True

    def test_includes_day_planner(self):
        result = resolve_phases({"morning": {"phases": {}}})
        assert result["day-planner"] is True

    def test_does_not_include_plan_today(self):
        result = resolve_phases({"morning": {"phases": {}}})
        assert "plan-today" not in result

    def test_output_covers_always_on_and_phase_map(self):
        """Output should be exactly _ALWAYS_ON ∪ _PHASE_MAP.values()."""
        result = resolve_phases({"morning": {"phases": {}}})
        expected = _ALWAYS_ON | set(_PHASE_MAP.values())
        assert set(result.keys()) == expected


# ---------------------------------------------------------------------------
# workflows.json structure
# ---------------------------------------------------------------------------


class TestWorkflowDefinition:
    """The morning-routine workflow in workflows.json has the expected shape."""

    def test_workflow_name_is_morning_routine(self, morning_workflow):
        assert morning_workflow["workflow_name"] == "morning-routine"

    def test_has_thirteen_steps(self, morning_workflow):
        """2 auto-run (load-config, resolve-phases) + 8 original steps + 3 new steps."""
        assert len(morning_workflow["steps"]) == 13

    def test_step_ids_are_unique(self, morning_workflow):
        ids = [s["id"] for s in morning_workflow["steps"]]
        assert len(ids) == len(set(ids))

    def test_no_plan_today_step(self, morning_workflow):
        ids = {s["id"] for s in morning_workflow["steps"]}
        assert "plan-today" not in ids

    def test_propose_mits_step_exists(self, morning_workflow):
        ids = {s["id"] for s in morning_workflow["steps"]}
        assert "propose-mits" in ids

    def test_persist_briefing_step_exists(self, morning_workflow):
        ids = {s["id"] for s in morning_workflow["steps"]}
        assert "persist-briefing" in ids

    def test_day_planner_step_exists(self, morning_workflow):
        ids = {s["id"] for s in morning_workflow["steps"]}
        assert "day-planner" in ids


class TestWorkflowDependencies:
    """DAG dependencies are correct after the plan-today split."""

    def _get_step(self, workflow: dict, step_id: str) -> dict:
        for s in workflow["steps"]:
            if s["id"] == step_id:
                return s
        raise AssertionError(f"Step {step_id!r} not found")

    def test_propose_mits_depends_on_synthesize(self, morning_workflow):
        s = self._get_step(morning_workflow, "propose-mits")
        assert s["depends_on"] == ["synthesize"]

    def test_persist_briefing_depends_on_propose_mits(self, morning_workflow):
        s = self._get_step(morning_workflow, "persist-briefing")
        assert s["depends_on"] == ["propose-mits"]

    def test_day_planner_depends_on_propose_mits_and_calendar(self, morning_workflow):
        s = self._get_step(morning_workflow, "day-planner")
        assert set(s["depends_on"]) == {"propose-mits", "calendar-today"}

    def test_persist_briefing_and_day_planner_are_siblings(self, morning_workflow):
        """Neither depends on the other — they can run in parallel."""
        pb = self._get_step(morning_workflow, "persist-briefing")
        dp = self._get_step(morning_workflow, "day-planner")
        assert "day-planner" not in pb["depends_on"]
        assert "persist-briefing" not in dp["depends_on"]

    def test_all_dependencies_reference_existing_steps(self, morning_workflow):
        ids = {s["id"] for s in morning_workflow["steps"]}
        for s in morning_workflow["steps"]:
            for dep in s.get("depends_on", []):
                assert dep in ids, f"{s['id']} depends on missing step {dep!r}"


class TestStepInstructions:
    """Every step has an instruction entry and key content is present."""

    def test_instructions_dict_exists(self, morning_workflow):
        assert "step_instructions" in morning_workflow

    def test_every_non_autorun_step_has_instructions(self, morning_workflow):
        """Steps with auto_run don't need instructions (conductor-dispatched).
        All others must have a string entry in step_instructions."""
        instructions = morning_workflow["step_instructions"]
        for s in morning_workflow["steps"]:
            if s.get("auto_run"):
                continue
            assert s["id"] in instructions, (
                f"Step {s['id']!r} has no instructions"
            )
            assert isinstance(instructions[s["id"]], str)
            assert instructions[s["id"]].strip() != ""

    def test_no_plan_today_instruction(self, morning_workflow):
        assert "plan-today" not in morning_workflow["step_instructions"]

    def test_propose_mits_instruction_mentions_task_create(self, morning_workflow):
        inst = morning_workflow["step_instructions"]["propose-mits"]
        assert "task_create" in inst

    def test_propose_mits_instruction_mentions_focused_tag(self, morning_workflow):
        """Interim workaround: #tasker/state/focused must appear in task_text."""
        inst = morning_workflow["step_instructions"]["propose-mits"]
        assert "#tasker/state/focused" in inst

    def test_persist_briefing_instruction_gates_on_config(self, morning_workflow):
        inst = morning_workflow["step_instructions"]["persist-briefing"]
        assert "persist_briefing" in inst
        assert "journal_write" in inst

    def test_day_planner_instruction_has_five_substeps(self, morning_workflow):
        """The day-planner instruction must enumerate the 5 sub-steps explicitly
        so the agent can't skip them — that was the original failure mode."""
        inst = morning_workflow["step_instructions"]["day-planner"]
        assert "Sub-step 1" in inst
        assert "Sub-step 2" in inst
        assert "Sub-step 3" in inst
        assert "Sub-step 4" in inst
        assert "Sub-step 5" in inst

    def test_day_planner_instruction_gates_on_config(self, morning_workflow):
        inst = morning_workflow["step_instructions"]["day-planner"]
        assert "day_planner" in inst
        assert "enabled" in inst

    def test_day_planner_instruction_guards_calendar_duplication(
        self, morning_workflow
    ):
        """The `hasRemoteCalendars` guard must be called out — this is the
        bit the agent is likely to forget if only given a single prose blob."""
        inst = morning_workflow["step_instructions"]["day-planner"]
        assert "hasRemoteCalendars" in inst

    def test_synthesize_instruction_no_plan_today_reference(self, morning_workflow):
        """The synthesize step used to say 'for plan-today'. It should now
        reference 'downstream steps' (or not reference plan-today at all)."""
        inst = morning_workflow["step_instructions"]["synthesize"]
        assert "plan-today" not in inst


# ---------------------------------------------------------------------------
# morning.json documentation alignment
# ---------------------------------------------------------------------------


class TestMorningDocs:
    """morning.json docs reflect the new step structure."""

    def test_morning_summary_mentions_new_steps(self, morning_docs):
        summary = morning_docs["morning"]["content"]["summary"]
        assert "propose-mits" in summary
        assert "persist-briefing" in summary
        assert "day-planner" in summary

    def test_morning_summary_does_not_mention_plan_today(self, morning_docs):
        summary = morning_docs["morning"]["content"]["summary"]
        assert "plan-today" not in summary

    def test_directions_description_mentions_new_steps(self, morning_docs):
        desc = morning_docs["morning/directions"]["description"]
        assert "propose-mits" in desc or "persist-briefing" in desc or "day-planner" in desc

    def test_directions_description_does_not_mention_plan_today(self, morning_docs):
        desc = morning_docs["morning/directions"]["description"]
        assert "plan-today" not in desc

    def test_directions_full_content_has_no_plan_today(self, morning_docs):
        full = morning_docs["morning/directions"]["content"]["full"]
        assert "plan-today" not in full

    def test_directions_full_content_lists_new_steps(self, morning_docs):
        full = morning_docs["morning/directions"]["content"]["full"]
        assert "propose-mits" in full
        assert "persist-briefing" in full
        assert "day-planner" in full


# ---------------------------------------------------------------------------
# End-to-end: knowledge store loads the workflow cleanly
# ---------------------------------------------------------------------------


class TestWorkflowLoadsIntoRegistry:
    """The conductor's ``_discover_workflows_from_store`` must parse the
    updated workflow without errors and produce a valid WorkflowDefinition."""

    def test_workflow_discovered_by_registry(self):
        from work_buddy.mcp_server.registry import _discover_workflows_from_store

        workflows = _discover_workflows_from_store()
        names = {wf.name for wf in workflows}
        assert "morning-routine" in names

    def test_discovered_workflow_has_all_new_steps(self):
        from work_buddy.mcp_server.registry import _discover_workflows_from_store

        workflows = _discover_workflows_from_store()
        mr = next(wf for wf in workflows if wf.name == "morning-routine")
        step_ids = {s.id for s in mr.steps}
        assert "propose-mits" in step_ids
        assert "persist-briefing" in step_ids
        assert "day-planner" in step_ids
        assert "plan-today" not in step_ids

    def test_discovered_workflow_dag_is_acyclic(self):
        """Constructing a WorkflowDAG from the discovered steps should not
        raise (cycle detection happens during add_task)."""
        from work_buddy.mcp_server.registry import _discover_workflows_from_store
        from work_buddy.workflow import WorkflowDAG

        workflows = _discover_workflows_from_store()
        mr = next(wf for wf in workflows if wf.name == "morning-routine")

        dag = WorkflowDAG(name="test:morning-routine", description="test")
        for step in mr.steps:
            dag.add_task(
                step.id,
                name=step.name,
                depends_on=list(step.depends_on or []),
            )
        # If we got here, no cycle was raised and all deps resolved
        assert dag._graph.number_of_nodes() == 13
