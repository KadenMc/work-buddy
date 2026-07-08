"""Unit tests for the corpus-wide checks in ``work_buddy.knowledge.validate``.

These tests focus on individual check functions in isolation by
constructing synthetic stores. Cross-file integration / live-store
tests live in ``test_dev_document_validate_step.py``.
"""

from __future__ import annotations

import pytest

from work_buddy.knowledge.capability_loader import SCHEMA_VERSION
from work_buddy.knowledge.file_store import workflow_body_heading_issues
from work_buddy.knowledge.model import CapabilityUnit, DirectionsUnit, WorkflowUnit
from work_buddy.knowledge.validate import (
    _check_capability_op_resolution,
    _check_directions_workflow_resolution,
    _check_placeholder_duplicates,
    _check_workflow_delegation_resolution,
    _check_workflow_step_consistency,
    _check_workflow_step_dag,
    validate_store,
)
from work_buddy.mcp_server import op_registry


class TestPlaceholderDuplicateCheck:
    """``placeholder_duplicate`` is a hard error — duplicate
    placeholders inside a single unit's ``content.full`` produce zero
    new content at read time (subsequent references render as
    back-reference markers), so they're never the right authorial
    choice. The editor rejects them at write time; this corpus-wide
    check catches any that slipped in via direct JSON edits.
    """

    def test_no_duplicates_returns_empty(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:b>> and <<wb:c>>"},
        )
        store = {"a": a}
        assert _check_placeholder_duplicates(store) == []

    def test_duplicate_target_in_one_unit_flagged(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:b>> mid <<wb:b>>"},
        )
        store = {"a": a}
        errors = _check_placeholder_duplicates(store)
        assert len(errors) == 1
        err = errors[0]
        assert err["check"] == "placeholder_duplicate"
        assert err["path"] == "a"
        assert "'b'" in err["message"]
        assert "2" in err["message"]

    def test_recursive_flag_does_not_save_a_duplicate(self):
        """Even ``<<wb:b --recursive>>`` twice in one unit is a
        duplicate. The flag doesn't change the read-time semantics
        of the per-unit-occurrence cap."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:b --recursive>> some <<wb:b --recursive>>"},
        )
        store = {"a": a}
        errors = _check_placeholder_duplicates(store)
        assert len(errors) == 1
        assert errors[0]["path"] == "a"

    def test_same_target_across_different_units_is_fine(self):
        """The check is per-unit. Unit A and unit B may each reference
        ``<<wb:c>>``; that's not a duplicate."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:c>>"},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "<<wb:c>>"},
        )
        store = {"a": a, "b": b}
        assert _check_placeholder_duplicates(store) == []

    def test_multiple_distinct_duplicates_each_flagged(self):
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={
                "full": "<<wb:x>> <<wb:y>> <<wb:x>> <<wb:y>> <<wb:y>>",
            },
        )
        store = {"a": a}
        errors = _check_placeholder_duplicates(store)
        assert len(errors) == 2
        flagged = {e["message"] for e in errors}
        # x appears twice
        assert any("'x'" in m and "2" in m for m in flagged)
        # y appears three times
        assert any("'y'" in m and "3" in m for m in flagged)

    def test_units_without_placeholders_skipped(self):
        """Plain prose units shouldn't pay the cost of placeholder
        parsing."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "Plain text only, no markers."},
        )
        store = {"a": a}
        assert _check_placeholder_duplicates(store) == []

    def test_empty_content_full_skipped(self):
        """Units missing ``content.full`` shouldn't crash the check."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={},
        )
        store = {"a": a}
        assert _check_placeholder_duplicates(store) == []


class TestCapabilityOpResolutionCheck:
    """``capability_op_resolution`` resolves declaration-based capability
    units against the Op registry and reports failures as *warnings* — the
    direct and declaration-based registration paths coexist, so an unresolved
    declaration is surfaced without failing the store.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        op_registry.clear_ops()
        yield
        op_registry.clear_ops()

    def _declaration(self, **overrides) -> CapabilityUnit:
        fields = dict(
            path="tasks/sample_cap",
            name="Sample Cap",
            description="A sample capability.",
            capability_name="sample_cap",
            category="tasks",
            parameters={"x": {"type": "str", "required": True}},
            op="op.wb.sample",
            schema_version=SCHEMA_VERSION,
        )
        fields.update(overrides)
        return CapabilityUnit(**fields)

    def test_no_declarations_returns_empty(self):
        store = {"a": DirectionsUnit(path="a", name="A", description="a")}
        assert _check_capability_op_resolution(store) == []

    def test_unresolved_declaration_flagged_as_warning(self):
        # op.wb.sample is never registered.
        store = {"tasks/sample_cap": self._declaration()}
        issues = _check_capability_op_resolution(store)
        assert len(issues) == 1
        assert issues[0]["check"] == "capability_op_resolution"
        assert issues[0]["severity"] == "warning"

    def test_resolved_declaration_produces_no_issue(self):
        def sample_op(x):
            return x

        op_registry.register_op("op.wb.sample", sample_op)
        store = {"tasks/sample_cap": self._declaration()}
        assert _check_capability_op_resolution(store) == []


def _wf(path: str, steps: list[dict], instructions: dict | None = None) -> WorkflowUnit:
    return WorkflowUnit(
        path=path, name=path, description="d", workflow_name=path.replace("/", "-"),
        steps=steps, step_instructions=instructions or {},
    )


class TestWorkflowStepDagCheck:
    """``workflow_step_dag`` validates each workflow unit's internal step DAG
    at author time — the same cycle / dangling-dep failures the conductor only
    raises at run time (workflow.py add_task)."""

    def test_well_formed_dag_is_clean(self):
        wf = _wf("ok", [
            {"id": "a", "step_type": "code", "depends_on": []},
            {"id": "b", "step_type": "code", "depends_on": ["a"]},
            {"id": "c", "step_type": "code", "depends_on": ["a", "b"]},
        ])
        assert _check_workflow_step_dag({"ok": wf}) == []

    def test_dangling_dependency_flagged(self):
        wf = _wf("x", [{"id": "a", "step_type": "code", "depends_on": ["ghost"]}])
        errs = _check_workflow_step_dag({"x": wf})
        assert len(errs) == 1
        assert errs[0]["check"] == "workflow_step_dag"
        assert "ghost" in errs[0]["message"]

    def test_cycle_flagged(self):
        wf = _wf("x", [
            {"id": "a", "step_type": "code", "depends_on": ["c"]},
            {"id": "b", "step_type": "code", "depends_on": ["a"]},
            {"id": "c", "step_type": "code", "depends_on": ["b"]},
        ])
        errs = _check_workflow_step_dag({"x": wf})
        assert any("cycle" in e["message"] for e in errs)

    def test_duplicate_step_id_flagged(self):
        wf = _wf("x", [
            {"id": "a", "step_type": "code", "depends_on": []},
            {"id": "a", "step_type": "code", "depends_on": []},
        ])
        errs = _check_workflow_step_dag({"x": wf})
        assert any("duplicate" in e["message"] and "'a'" in e["message"] for e in errs)

    def test_non_workflow_units_ignored(self):
        d = DirectionsUnit(path="d", name="D", description="d")
        assert _check_workflow_step_dag({"d": d}) == []


class TestWorkflowStepConsistencyCheck:
    """``workflow_step_consistency`` — orphan instruction keys are errors;
    reasoning steps without instructions are non-blocking warnings."""

    def test_clean_workflow(self):
        wf = _wf(
            "ok",
            [{"id": "a", "step_type": "reasoning", "depends_on": []}],
            {"a": "do a"},
        )
        assert _check_workflow_step_consistency({"ok": wf}) == []

    def test_orphan_instruction_is_error(self):
        wf = _wf(
            "x",
            [{"id": "a", "step_type": "code", "depends_on": []}],
            {"ghost": "dead text"},
        )
        errs = _check_workflow_step_consistency({"x": wf})
        assert len(errs) == 1
        assert errs[0]["check"] == "workflow_step_consistency"
        assert "ghost" in errs[0]["message"]
        assert errs[0].get("severity", "error") != "warning"

    def test_reasoning_step_without_instructions_is_warning(self):
        wf = _wf("x", [{"id": "a", "step_type": "reasoning", "depends_on": []}])
        errs = _check_workflow_step_consistency({"x": wf})
        assert len(errs) == 1
        assert errs[0]["severity"] == "warning"

    def test_bare_reasoning_step_suppressed_when_directions_unit_binds(self):
        # House convention: a reasoning step's behavior may live in the bound
        # directions unit (whose ``workflow`` targets this workflow), not the
        # step body — so a bare reasoning step there is intentional, not a miss.
        wf = _wf("x", [{"id": "a", "step_type": "reasoning", "depends_on": []}])
        directions = DirectionsUnit(
            path="x-dir", name="X", description="d", workflow="x",
        )
        store = {"x": wf, "x-dir": directions}
        assert _check_workflow_step_consistency(store) == []

    def test_suppression_is_scoped_to_the_bound_workflow(self):
        # A directions unit binding workflow "x" must NOT silence a bare
        # reasoning step in an unrelated, unbound workflow "y".
        wf_x = _wf("x", [{"id": "a", "step_type": "reasoning", "depends_on": []}])
        wf_y = _wf("y", [{"id": "b", "step_type": "reasoning", "depends_on": []}])
        directions = DirectionsUnit(
            path="x-dir", name="X", description="d", workflow="x",
        )
        errs = _check_workflow_step_consistency(
            {"x": wf_x, "y": wf_y, "x-dir": directions}
        )
        assert len(errs) == 1
        assert errs[0]["path"] == "y"
        assert errs[0]["severity"] == "warning"

    def test_orphan_key_still_errors_even_when_directions_bound(self):
        # Suppression only covers the bare-reasoning warning; an orphan
        # ``step_instructions`` key is still dead text and must error.
        wf = _wf(
            "x",
            [{"id": "a", "step_type": "reasoning", "depends_on": []}],
            {"a": "do a", "ghost": "dead"},
        )
        directions = DirectionsUnit(
            path="x-dir", name="X", description="d", workflow="x",
        )
        errs = _check_workflow_step_consistency({"x": wf, "x-dir": directions})
        assert len(errs) == 1
        assert "ghost" in errs[0]["message"]
        assert errs[0].get("severity", "error") != "warning"

    def test_code_step_without_instructions_is_fine(self):
        wf = _wf("x", [{"id": "a", "step_type": "code", "depends_on": []}])
        assert _check_workflow_step_consistency({"x": wf}) == []


class TestDirectionsWorkflowResolutionCheck:
    """``directions_workflow_resolution`` — a directions unit's ``workflow``
    field must resolve to a real ``kind: workflow`` unit; a dangling binding
    is an error (it silently defeats the consistency-check suppression)."""

    def test_resolved_binding_is_clean(self):
        wf = _wf("tasks/task-me", [{"id": "a", "step_type": "code", "depends_on": []}])
        directions = DirectionsUnit(
            path="tasks/task-me-directions", name="D", description="d",
            workflow="tasks/task-me",
        )
        store = {"tasks/task-me": wf, "tasks/task-me-directions": directions}
        assert _check_directions_workflow_resolution(store) == []

    def test_dangling_binding_is_error(self):
        # The real bug this guards against: the bare slug instead of the path.
        directions = DirectionsUnit(
            path="tasks/task-me-directions", name="D", description="d",
            workflow="task-me",  # missing the "tasks/" prefix → resolves to nothing
        )
        wf = _wf("tasks/task-me", [{"id": "a", "step_type": "code", "depends_on": []}])
        errs = _check_directions_workflow_resolution(
            {"tasks/task-me": wf, "tasks/task-me-directions": directions}
        )
        assert len(errs) == 1
        assert errs[0]["check"] == "directions_workflow_resolution"
        assert errs[0]["path"] == "tasks/task-me-directions"
        assert errs[0].get("severity", "error") != "warning"

    def test_directions_without_workflow_field_is_ignored(self):
        directions = DirectionsUnit(path="d", name="D", description="d")
        assert _check_directions_workflow_resolution({"d": directions}) == []


def _wf_with_prose(path: str, full: str, *, steps: list[dict] | None = None) -> WorkflowUnit:
    return WorkflowUnit(
        path=path, name=path, description="d", workflow_name=path.replace("/", "-"),
        steps=steps or [{"id": "a", "step_type": "code", "depends_on": []}],
        content={"full": full},
    )


def _bare_reasoning_wf(path: str) -> WorkflowUnit:
    """A workflow with a bare (instruction-less) reasoning step."""
    return _wf(path, [{"id": "think", "step_type": "reasoning", "depends_on": []}])


class TestWorkflowDelegationResolutionCheck:
    """``workflow_delegation_resolution`` — nested wb_run delegations between
    workflows must resolve, and must not land on bare+unbound reasoning steps
    that runtime directions-delivery cannot rescue."""

    def test_dangling_hyphenated_delegation_is_error(self):
        # A kebab-shaped name that is neither a workflow nor a capability.
        caller = _wf_with_prose(
            "x", 'do `mcp__work-buddy__wb_run("ghost-workflow")` then advance',
        )
        errs = _check_workflow_delegation_resolution({"x": caller})
        assert len(errs) == 1
        assert errs[0]["check"] == "workflow_delegation_resolution"
        assert errs[0]["path"] == "x"
        assert "ghost-workflow" in errs[0]["message"]
        assert errs[0].get("severity", "error") != "warning"

    def test_blind_delegation_into_bare_unbound_workflow_is_error(self):
        caller = _wf_with_prose("x", 'wb_run("y")')
        target = _bare_reasoning_wf("y")  # bare reasoning, no directions bind it
        errs = _check_workflow_delegation_resolution({"x": caller, "y": target})
        assert len(errs) == 1
        assert "y" in errs[0]["message"]
        assert "think" in errs[0]["message"]      # names the bare step
        assert errs[0].get("severity", "error") != "warning"

    def test_covered_delegation_is_silent(self):
        # target is bare BUT has a bound directions unit -> runtime delivery
        # covers it -> intentional, no finding.
        caller = _wf_with_prose("x", 'wb_run("y")')
        target = _bare_reasoning_wf("y")
        directions = DirectionsUnit(
            path="y-dir", name="D", description="d", workflow="y",
        )
        store = {"x": caller, "y": target, "y-dir": directions}
        assert _check_workflow_delegation_resolution(store) == []

    def test_delegation_into_self_documented_workflow_is_silent(self):
        # target's reasoning step has an inline instruction -> not bare -> fine.
        caller = _wf_with_prose("x", 'wb_run("y")')
        target = _wf(
            "y", [{"id": "think", "step_type": "reasoning", "depends_on": []}],
            {"think": "do the thing"},
        )
        assert _check_workflow_delegation_resolution({"x": caller, "y": target}) == []

    def test_capability_call_is_not_a_delegation(self):
        cap = CapabilityUnit(
            path="c", name="C", description="d", capability_name="task_briefing",
        )
        caller = _wf_with_prose("x", 'wb_run("task_briefing")')
        assert _check_workflow_delegation_resolution({"x": caller, "c": cap}) == []

    def test_snake_case_unknown_is_not_flagged(self):
        # Assumed to be an op-registered capability without a store declaration;
        # left to capability_op_resolution, not flagged here.
        caller = _wf_with_prose("x", 'wb_run("some_unknown_cap")')
        assert _check_workflow_delegation_resolution({"x": caller}) == []

    def test_self_reference_is_ignored(self):
        # A workflow's own "Start via wb_run('self')" line must not flag itself.
        caller = _wf_with_prose("x", 'Start via wb_run("x").')
        assert _check_workflow_delegation_resolution({"x": caller}) == []

    def test_invokes_surface_is_scanned(self):
        # Delegation declared structurally via a step's `invokes` list, no prose.
        caller = _wf(
            "x",
            [{"id": "a", "step_type": "reasoning", "depends_on": [],
              "invokes": ["y"]}],
            {"a": "go"},
        )
        target = _bare_reasoning_wf("y")
        errs = _check_workflow_delegation_resolution({"x": caller, "y": target})
        assert len(errs) == 1
        assert "y" in errs[0]["message"]

    # --- param-contract checks (caller passes keys the callee must declare) ---

    def test_delegation_passing_undeclared_param_errors_update_journal_case(self):
        # The exact shape of the real bug: a caller passes {"target": ...} to a
        # workflow that declares no such param → rejected at the param gate.
        caller = _wf_with_prose(
            "morning",
            'step 6: `mcp__work-buddy__wb_run("upd", {"target": "yesterday"})`',
        )
        target = WorkflowUnit(
            path="upd", name="upd", description="d", workflow_name="upd",
            steps=[{"id": "a", "step_type": "code", "depends_on": []}],
        )  # no params_schema → rejects any params
        errs = _check_workflow_delegation_resolution({"morning": caller, "upd": target})
        assert len(errs) == 1
        assert errs[0]["check"] == "workflow_delegation_resolution"
        assert "target" in errs[0]["message"]
        assert errs[0].get("severity", "error") != "warning"

    def test_delegation_with_declared_optional_param_is_clean(self):
        # After the fix: target declares `target` (optional) → no flag. This is
        # the post-fix update-journal state.
        caller = _wf_with_prose("morning", 'wb_run("upd", {"target": "yesterday"})')
        target = WorkflowUnit(
            path="upd", name="upd", description="d", workflow_name="upd",
            steps=[{"id": "a", "step_type": "code", "depends_on": []}],
            params_schema={"target": {"type": "str", "required": False}},
        )
        assert _check_workflow_delegation_resolution({"morning": caller, "upd": target}) == []

    def test_bare_delegation_has_no_param_contract_error(self):
        caller = _wf_with_prose("morning", 'wb_run("upd")')
        target = WorkflowUnit(
            path="upd", name="upd", description="d", workflow_name="upd",
            steps=[{"id": "a", "step_type": "code", "depends_on": []}],
        )
        assert _check_workflow_delegation_resolution({"morning": caller, "upd": target}) == []

    def test_undeclared_key_when_target_has_schema_errors(self):
        caller = _wf_with_prose("morning", 'wb_run("upd", {"bogus": 1})')
        target = WorkflowUnit(
            path="upd", name="upd", description="d", workflow_name="upd",
            steps=[{"id": "a", "step_type": "code", "depends_on": []}],
            params_schema={"target": {"type": "str", "required": False}},
        )
        errs = _check_workflow_delegation_resolution({"morning": caller, "upd": target})
        assert len(errs) == 1
        assert "bogus" in errs[0]["message"]


class TestWorkflowBodyHeadingIssues:
    """The raw-file heading helper the commit step uses — catches a ``##``
    heading after the first step section that matches no step id (the codec
    would silently merge it into the previous step)."""

    _STEPS_FM = (
        "---\nname: T\nkind: workflow\nworkflow_name: t\nsteps:\n"
        "- id: one\n  step_type: reasoning\n  depends_on: []\n"
        "- id: two\n  step_type: reasoning\n  depends_on: [one]\n---\n\n"
    )

    def test_clean_workflow_body(self):
        txt = self._STEPS_FM + "Intro.\n\n## one\n\ndo one\n\n## two\n\ndo two\n"
        assert workflow_body_heading_issues(txt) == []

    def test_narrative_subheading_before_steps_not_flagged(self):
        txt = self._STEPS_FM + "## Overview\n\nstuff\n\n## one\n\ndo one\n\n## two\n\ndo two\n"
        assert workflow_body_heading_issues(txt) == []

    def test_typo_heading_after_steps_flagged(self):
        txt = self._STEPS_FM + "## one\n\ndo one\n\n## twoo\n\noops\n\n## two\n\ndo two\n"
        issues = workflow_body_heading_issues(txt)
        assert len(issues) == 1
        assert "twoo" in issues[0]

    def test_non_workflow_file_returns_empty(self):
        txt = "---\nname: D\nkind: directions\n---\n\n## any heading\n\nbody\n"
        assert workflow_body_heading_issues(txt) == []


class TestValidateStoreSeverity:
    """``validate_store`` splits blocking errors from non-blocking warnings."""

    def test_return_shape_has_severity_buckets(self):
        result = validate_store()
        for key in ("passed", "failed", "warnings", "errors", "issues", "summary"):
            assert key in result, f"missing key {key!r}"
        assert isinstance(result["passed"], bool)
        # warnings never count toward failure
        assert result["failed"] == len(result["errors"])
        # every error has error severity (default); warnings are separate
        for err in result["errors"]:
            assert err.get("severity", "error") != "warning"


class TestDurableSurfacesCheck:
    """``durable_surfaces`` is an advisory, store-wide scan for transient
    identifiers (stage labels, dates, VCS refs, migration narrative) in
    unit prose. Warnings only: the open list is the cleanup backlog, and
    the check exists because diff-scoped commit hygiene structurally
    cannot see archaeology in files no commit touches.
    """

    @staticmethod
    def _run(unit):
        from work_buddy.knowledge.validate import _check_durable_surfaces
        return _check_durable_surfaces({unit.path: unit})

    def test_clean_unit_produces_no_findings(self):
        u = DirectionsUnit(
            path="a", name="Action contexts", description="Resolves who can act.",
            content={"full": "The resolver consults the context registry."},
        )
        assert self._run(u) == []

    def test_stage_label_in_name_flagged_as_warning(self):
        u = DirectionsUnit(
            path="a", name="Action contexts (Slice 5a)", description="d",
            content={},
        )
        findings = self._run(u)
        assert len(findings) == 1
        f = findings[0]
        assert f["check"] == "durable_surfaces"
        assert f["severity"] == "warning"
        assert "stage_label" in f["message"]
        assert "(name)" in f["message"]

    def test_exempt_tag_suppresses_all_findings(self):
        u = DirectionsUnit(
            path="a", name="Retrieval funnel", description="Stage 1 ranks, stage 2 drills.",
            tags=["allow-transient-labels"],
            content={"full": "Slice 4 built this on 2026-05-01."},
        )
        assert self._run(u) == []

    def test_date_in_dev_notes_flagged_with_field(self):
        u = DirectionsUnit(
            path="a", name="N", description="d",
            content={}, dev_notes="Built 2026-05-09 during the overnight run.",
        )
        findings = self._run(u)
        cats = {f["message"].split(":")[0] for f in findings}
        assert "transient date" in cats
        assert any("(dev_notes)" in f["message"] for f in findings)

    def test_matches_aggregate_one_finding_per_category(self):
        full = " ".join(f"Slice {i} shipped." for i in range(1, 9))
        u = DirectionsUnit(path="a", name="N", description="d", content={"full": full})
        findings = self._run(u)
        assert len(findings) == 1
        assert "+3 more" in findings[0]["message"]

    def test_bare_version_labels_not_flagged(self):
        u = DirectionsUnit(
            path="a", name="Threads", description="v5 Threads is the canonical surface.",
            content={"full": "The schema is at v3; the API returns stage1_hits."},
        )
        assert self._run(u) == []

    def test_tier_reference_flagged_as_rename_tracker(self):
        u = DirectionsUnit(
            path="a", name="N", description="Caps the ceiling at tier-3 review.",
            content={},
        )
        findings = self._run(u)
        assert len(findings) == 1
        assert "stage_label" in findings[0]["message"]

    def test_capability_parameters_scanned(self):
        u = CapabilityUnit(
            path="a", name="N", description="d", content={},
            capability_name="cap",
            parameters={"task_id": {"description": "e.g. 't-a3f8c1e2'"}},
        )
        findings = self._run(u)
        assert len(findings) == 1
        assert "task_ref" in findings[0]["message"]
        assert "(parameters)" in findings[0]["message"]

    def test_scoped_commit_flagged_but_bare_hex_ignored(self):
        u = DirectionsUnit(
            path="a", name="N", description="Fixed in commit 36cb747.",
            content={"full": "The default session id is 00000000 and cafe1234 is an example."},
        )
        findings = self._run(u)
        assert len(findings) == 1
        assert "commit_ref" in findings[0]["message"]

    def test_migration_phrases_flagged(self):
        u = DirectionsUnit(
            path="a", name="N", description="d",
            content={"full": "This ships inert for now and is staged to replace the old path."},
        )
        findings = self._run(u)
        assert len(findings) == 1
        assert "migration_phrase" in findings[0]["message"]

    def test_check_registered_in_validate_store(self):
        result = validate_store(checks=["durable_surfaces"])
        assert "durable_surfaces" in result["checks_run"]
        # advisory only: findings must never block the store
        assert all(
            e.get("severity") == "warning"
            for e in result["issues"]
            if e["check"] == "durable_surfaces"
        )
