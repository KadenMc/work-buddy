"""Miniaturized tests for workflow DAG resilience changes."""

import json
import os

# Ensure session ID is set for imports
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-schema-val")

from work_buddy.mcp_server.conductor import (
    advance_workflow,
    _ACTIVE_RUNS,
    _validate_step_result,
    _relevant_step_results,
    _cap_step_results,
)
from work_buddy.mcp_server.registry import (
    WorkflowStep,
    WorkflowDefinition,
    AutoRun,
    _discover_workflows_from_store,
)
from work_buddy.workflow import WorkflowDAG
from pathlib import Path

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} — {detail}")


# ── Test 1: Schema validation ─────────────────────────────────

print("\n=== Test 1: Schema validation in conductor ===")

dag = WorkflowDAG(name="test-schema:wf_test1", description="test")
dag.add_task("step1", name="Reasoning step", metadata={
    "step_type": "reasoning",
    "instruction": "test",
    "result_schema": {
        "required_keys": ["groups_by_action", "total_groups"],
        "key_types": {"groups_by_action": "dict", "total_groups": "int"},
    },
})
dag.add_task("step2", name="Next step", depends_on=["step1"], metadata={
    "step_type": "reasoning",
    "instruction": "test",
})

_ACTIVE_RUNS["wf_test1"] = dag
dag.start_task("step1")

# 1a: Bad result — should be rejected
result = advance_workflow("wf_test1", {"presentation_updated": True, "changes": []})
check(
    "Bad result rejected",
    result.get("type") == "validation_error",
    f"got type={result.get('type')}",
)
check(
    "Error mentions missing key",
    "groups_by_action" in result.get("error", ""),
    result.get("error", ""),
)
check(
    "Hint provided",
    "hint" in result,
)
check(
    "Step stays RUNNING after rejection",
    dag._graph.nodes["step1"].get("status") == "running",
    dag._graph.nodes["step1"].get("status"),
)

# 1b: Good result — should be accepted
result2 = advance_workflow("wf_test1", {"groups_by_action": {}, "total_groups": 5})
check(
    "Good result accepted",
    result2.get("type") in ("workflow_step", "workflow_complete"),
    f"got type={result2.get('type')}",
)
check(
    "Step completed",
    dag._graph.nodes["step1"].get("status") == "completed",
    dag._graph.nodes["step1"].get("status"),
)

# Cleanup
del _ACTIVE_RUNS["wf_test1"]


# ── Test 2: Schema validation — type mismatch ─────────────────

print("\n=== Test 2: Schema validation — type mismatch ===")

dag2 = WorkflowDAG(name="test-schema2:wf_test2", description="test")
dag2.add_task("step1", name="Typed step", metadata={
    "step_type": "reasoning",
    "instruction": "test",
    "result_schema": {
        "required_keys": ["data"],
        "key_types": {"data": "dict"},
    },
})
dag2.add_task("step2", name="Next", depends_on=["step1"], metadata={
    "step_type": "reasoning",
    "instruction": "test",
})

_ACTIVE_RUNS["wf_test2"] = dag2
dag2.start_task("step1")

result = advance_workflow("wf_test2", {"data": "not a dict"})
check(
    "Type mismatch rejected",
    result.get("type") == "validation_error",
    f"got type={result.get('type')}",
)
check(
    "Error mentions expected type",
    "dict" in result.get("error", "") and "str" in result.get("error", ""),
    result.get("error", ""),
)

del _ACTIVE_RUNS["wf_test2"]


# ── Test 3: Steps without schema skip validation ──────────────

print("\n=== Test 3: No schema = no validation ===")

dag3 = WorkflowDAG(name="test-noschema:wf_test3", description="test")
dag3.add_task("step1", name="Free step", metadata={
    "step_type": "reasoning",
    "instruction": "test",
    # no result_schema
})
dag3.add_task("step2", name="Next", depends_on=["step1"], metadata={
    "step_type": "reasoning",
    "instruction": "test",
})

_ACTIVE_RUNS["wf_test3"] = dag3
dag3.start_task("step1")

result = advance_workflow("wf_test3", "literally anything")
check(
    "Free-form result accepted (no schema)",
    result.get("type") in ("workflow_step", "workflow_complete"),
    f"got type={result.get('type')}",
)

del _ACTIVE_RUNS["wf_test3"]


# ── Test 4: Smart trimming ────────────────────────────────────

print("\n=== Test 4: Smart step_results trimming ===")

dag4 = WorkflowDAG(name="test-trim:wf_test4", description="test")
for i in range(5):
    deps = [f"s{i-1}"] if i > 0 else None
    dag4.add_task(f"s{i}", name=f"Step {i}", depends_on=deps, metadata={
        "step_type": "reasoning",
        "instruction": "test",
    })

# Complete s0..s3, start s4
for i in range(4):
    dag4.start_task(f"s{i}")
    dag4.complete_task(f"s{i}", result={"data": f"result_{i}", "big": "x" * 1000})

# Without a registered WorkflowDefinition, trimming falls back to cap-all
trimmed_fallback = _relevant_step_results(dag4, "s4", prior_step_id="s3")
check(
    "Fallback (no wf_def) includes all results",
    len(trimmed_fallback) == 4,
    f"expected 4, got {len(trimmed_fallback)} keys={list(trimmed_fallback.keys())}",
)

# Test with explicit needed set (simulating what happens with a real workflow)
# The _relevant_step_results function uses _get_wf_def internally.
# For real chrome-triage, we verified parsing above — the trimming
# correctly computes {build-presentation} for resolve-and-clarify.
# Here we verify the cap function itself works.
big_results = {f"s{i}": {"data": "x" * 60000} for i in range(4)}
capped = _cap_step_results(big_results)
oversized = [k for k, v in capped.items() if isinstance(v, dict) and v.get("_truncated")]
check(
    "Cap truncates oversized results",
    len(oversized) == 4,
    f"expected 4 truncated, got {len(oversized)}",
)


# ── Test 5: Chrome-triage store loading ─────────────────────────

print("\n=== Test 5: Chrome-triage workflow from knowledge store ===")

store_wfs = _discover_workflows_from_store()
wf = next((w for w in store_wfs if w.name == "chrome-triage"), None)
check("Workflow loaded from store", wf is not None)
check("11 steps", wf is not None and len(wf.steps) == 11, f"got {len(wf.steps) if wf else 0}")

if wf:
    step_lookup = {s.id: s for s in wf.steps}
    rac = step_lookup.get("resolve-and-clarify")
    check(
        "resolve-and-clarify has result_schema",
        rac is not None and rac.result_schema is not None,
    )
    check(
        "result_schema has required_keys",
        rac.result_schema.get("required_keys") == ["groups_by_action", "total_groups", "total_items"],
        str(rac.result_schema.get("required_keys")),
    )

    br = step_lookup.get("build-recommendations")
    check(
        "build-recommendations has result_schema",
        br is not None and br.result_schema is not None,
    )


# ── Test 6: Defensive validation in dispatch ──────────────────

print("\n=== Test 6: Defensive validation (_validate_presentation) ===")

from work_buddy.triage.dispatch import _validate_presentation

err = _validate_presentation({"groups_by_action": {}}, "test")
check("Valid presentation passes", err is None, str(err))

err = _validate_presentation({"changes": []}, "test")
check(
    "Missing groups_by_action rejected",
    err is not None and "groups_by_action" in err.get("error", ""),
    str(err),
)

err = _validate_presentation("not a dict", "test")
check(
    "Non-dict rejected",
    err is not None and "dict" in err.get("error", ""),
    str(err),
)


# ── Summary ───────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    exit(1)
else:
    print("All tests passed!")
