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


# ── Test 2b: Empty-result parameter-name hint ─────────────────
#
# When the agent forgets to pass step_result (or names the kwarg
# incorrectly so FastMCP drops it), the conductor sees an empty dict
# against a schema that requires keys. The generic "make sure your
# result dict has all the fields" hint misdirects in that case — the
# real cause is upstream of the dict's contents. The error message
# and the hint field both surface a parameter-name nudge.

print("\n=== Test 2b: Empty result triggers parameter-name hint ===")

dag2b = WorkflowDAG(name="test-empty:wf_test2b", description="test")
dag2b.add_task("step1", name="Required-keys step", metadata={
    "step_type": "reasoning",
    "instruction": "test",
    "result_schema": {
        "required_keys": ["units_read", "files_read"],
    },
})
dag2b.add_task("step2", name="Next", depends_on=["step1"], metadata={
    "step_type": "reasoning",
    "instruction": "test",
})

_ACTIVE_RUNS["wf_test2b"] = dag2b
dag2b.start_task("step1")

# Simulate the failure mode: caller passes no step_result. The
# gateway's _parse_params turns None into {}, so the conductor sees
# an empty dict.
result = advance_workflow("wf_test2b", {})
check(
    "Empty result rejected",
    result.get("type") == "validation_error",
    f"got type={result.get('type')}",
)
check(
    "Error message names step_result parameter",
    "step_result" in result.get("error", ""),
    result.get("error", ""),
)
check(
    "Error message warns about wrong-named result kwarg",
    "result=" in result.get("error", ""),
    result.get("error", ""),
)
check(
    "Hint pivots to parameter-name framing on empty result",
    "step_result" in result.get("hint", "")
    and "FastMCP" in result.get("hint", ""),
    result.get("hint", ""),
)
check(
    "Hint does NOT use the generic data-structure framing on empty result",
    "presentation dict" not in result.get("hint", ""),
    result.get("hint", ""),
)
check(
    "Step stays running after empty-result rejection",
    dag2b._graph.nodes["step1"].get("status") == "running",
    dag2b._graph.nodes["step1"].get("status"),
)

# Sanity: when the agent actually sends a non-empty (but still
# missing-key) result, the hint should fall back to the generic
# data-structure framing — the parameter-name nudge is only for the
# empty case.
result_partial = advance_workflow("wf_test2b", {"units_read": ["a"]})
check(
    "Non-empty missing-key still rejected",
    result_partial.get("type") == "validation_error",
    f"got type={result_partial.get('type')}",
)
check(
    "Non-empty hint stays generic (no parameter-name framing)",
    "FastMCP" not in result_partial.get("hint", "")
    and "presentation dict" in result_partial.get("hint", ""),
    result_partial.get("hint", ""),
)
check(
    "Non-empty error message does NOT carry the empty-result nudge",
    "FastMCP" not in result_partial.get("error", ""),
    result_partial.get("error", ""),
)

del _ACTIVE_RUNS["wf_test2b"]


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


# ── Test 5: Store loading parses result_schema ─────────────────

print("\n=== Test 5: result_schema parsed from knowledge store ===")

# task-new is a stable fixture: two reasoning steps (plan, confirm) carry
# result_schema. Validates that _discover_workflows_from_store correctly
# threads result_schema through into WorkflowStep.
store_wfs = _discover_workflows_from_store()
wf = next((w for w in store_wfs if w.name == "task-new"), None)
check("Workflow loaded from store", wf is not None)

if wf:
    step_lookup = {s.id: s for s in wf.steps}
    plan = step_lookup.get("plan")
    check(
        "plan has result_schema",
        plan is not None and plan.result_schema is not None,
    )
    check(
        "result_schema has required_keys",
        plan is not None
        and plan.result_schema is not None
        and plan.result_schema.get("required_keys") == ["task_text"],
        str(plan.result_schema.get("required_keys")) if plan and plan.result_schema else "",
    )

    confirm = step_lookup.get("confirm")
    check(
        "confirm has result_schema",
        confirm is not None and confirm.result_schema is not None,
    )


# ── Summary ───────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    exit(1)
else:
    print("All tests passed!")
