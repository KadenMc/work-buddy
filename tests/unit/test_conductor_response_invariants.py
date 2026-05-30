"""Invariants every conductor response must satisfy.

These are not unit tests of any single function — they are *shape* assertions
that should hold for every dict returned by ``start_workflow`` /
``advance_workflow`` / ``get_step_result``. The flagship invariant is "the
response is a tree, not a graph": no non-trivial subtree should appear at
two or more paths inside the same response. That property was violated by
both ``auto_ran[*].result`` (mirroring ``step_results[id]``) and
``prior_step.result`` (mirroring ``step_results[prior_id]``); fixing those
is what these tests guard.

The detection helper lives in ``work_buddy.mcp_server._response_audit`` so
the conductor can import it for its own runtime warning path; this module
just wraps it with a pytest-flavored assertion plus the integration-style
tests that exercise real conductor codepaths.
"""

from __future__ import annotations

from typing import Any

import pytest

from work_buddy.mcp_server._response_audit import (
    DEFAULT_MIN_CONTAINED_KEYS,
    DEFAULT_MIN_SUBTREE,
    find_contained_subtrees,
    find_duplicated_subtrees,
    find_step_result_accumulations,
    format_containment_report,
    format_duplication_report,
)


@pytest.fixture(autouse=True)
def _isolate_agents_dir(tmp_agents_dir):
    """Redirect agent-session writes to a temp dir for every test here.

    The integration tests call ``start_workflow``, which persists a DAG
    via ``_save()``. Without isolation those files land in the live
    ``.data/agents/<session>/workflows/`` and the conductor's restart
    recovery later loads them as bogus active runs.
    """
    yield


def assert_no_duplicated_subtrees(
    resp: Any,
    *,
    min_size: int = DEFAULT_MIN_SUBTREE,
) -> None:
    """Assert no non-trivial subtree appears at two or more paths in ``resp``.

    Use as the standing invariant in any test exercising a conductor
    response.  Failure prints the offending paths so the diagnosis is
    self-evident.
    """
    dupes = find_duplicated_subtrees(resp, min_size=min_size)
    if dupes:
        pytest.fail(format_duplication_report(dupes, min_size=min_size))


def assert_no_contained_subtrees(
    resp: Any,
    *,
    min_size: int = DEFAULT_MIN_SUBTREE,
    min_keys: int = DEFAULT_MIN_CONTAINED_KEYS,
) -> None:
    """Assert no dict subtree is a non-trivial subset of another at a non-nested path.

    Catches the cross-step accumulation pattern (Problem C) where each
    step's result echoes a prior step's fields plus its own delta.  A
    stricter check than ``assert_no_duplicated_subtrees``: catches the
    case where individual values are below the duplication threshold but
    the cumulative key-by-key overlap is large.
    """
    triples = find_contained_subtrees(resp, min_size=min_size, min_keys=min_keys)
    if triples:
        pytest.fail(format_containment_report(triples))


def assert_auto_ran_ledger_has_corresponding_step_results(resp: Any) -> None:
    """For every ``auto_ran`` ledger entry, assert its data is in step_results.

    The auto_ran ledger advertises which auto-run steps just executed.
    The contract is that the data those steps produced is reachable via
    ``step_results[id]`` (no silent loss when we drop ``auto_ran[*].result``).
    Skipped and errored entries don't have results to surface, so they're
    exempt.
    """
    auto_ran = resp.get("auto_ran") or []
    step_results = resp.get("step_results") or {}
    missing: list[str] = []
    for entry in auto_ran:
        if entry.get("error") or entry.get("skipped"):
            continue
        sid = entry.get("id")
        if sid and sid not in step_results:
            missing.append(sid)
    if missing:
        pytest.fail(
            f"auto_ran advertises {missing} but step_results does not include them. "
            f"step_results keys: {sorted(step_results.keys())}"
        )


# ---------------------------------------------------------------------------
# Helper smoke tests (the cheap end of the pyramid)
# ---------------------------------------------------------------------------

def test_helper_passes_a_clean_response():
    """No subtree of significant size repeats — should pass."""
    clean = {
        "type": "workflow_step",
        "current_step": {"id": "next", "name": "X" * 300},
        "step_results": {"prior": {"data": "Y" * 300}},
    }
    assert_no_duplicated_subtrees(clean)


def test_helper_catches_auto_ran_result_mirroring_step_results():
    """The auto_ran[*].result == step_results[id] anti-pattern must trip the helper."""
    payload = {"data": "X" * 400, "meta": "short"}
    bad = {
        "step_results": {"scan": payload},
        "auto_ran": [{"id": "scan", "name": "Scan", "result": payload}],
    }
    with pytest.raises(pytest.fail.Exception, match=r"step_results.scan"):
        assert_no_duplicated_subtrees(bad)


def test_helper_catches_prior_step_result_mirroring_step_results():
    """The prior_step.result == step_results[prior_id] anti-pattern must trip too."""
    payload = {"value": "Z" * 400}
    bad = {
        "step_results": {"propose": payload},
        "prior_step": {"id": "propose", "result": payload},
    }
    with pytest.raises(pytest.fail.Exception, match=r"prior_step.result"):
        assert_no_duplicated_subtrees(bad)


def test_helper_ignores_small_subtrees():
    """Trivially small repeated subtrees (under threshold) are not flagged."""
    small = {"k": 1}
    payload = {"a": small, "b": small}
    assert_no_duplicated_subtrees(payload)


def test_no_silent_loss_helper_passes_clean_ledger():
    """Ledger entry has corresponding step_results entry — should pass."""
    clean = {
        "auto_ran": [{"id": "scan", "name": "Scan"}],
        "step_results": {"scan": {"data": [1, 2, 3]}},
    }
    assert_auto_ran_ledger_has_corresponding_step_results(clean)


def test_no_silent_loss_helper_catches_missing_step_results():
    """Ledger advertises a step but step_results is missing it — should trip."""
    bad = {
        "auto_ran": [{"id": "scan", "name": "Scan"}],
        "step_results": {},
    }
    with pytest.raises(pytest.fail.Exception, match=r"\['scan'\]"):
        assert_auto_ran_ledger_has_corresponding_step_results(bad)


def test_containment_helper_passes_distinct_dicts():
    """Two dicts with no key overlap — should pass."""
    clean = {
        "step_results": {
            "step_a": {"alpha": 1, "beta": 2, "gamma": "g" * 200},
            "step_b": {"delta": 3, "epsilon": 4, "zeta": "z" * 200},
        },
    }
    assert_no_contained_subtrees(clean)


def test_containment_helper_catches_step_b_supersetting_step_a():
    """Step B's dict contains every (k, v) pair from step A — must trip."""
    items = ["x" * 50] * 8  # ~500 chars serialized
    bad = {
        "step_results": {
            "categorize": {
                "items": items,
                "categories": ["c" * 30] * 5,
                "extra1": "e" * 100,
            },
            "summarize": {
                "items": items,
                "categories": ["c" * 30] * 5,
                "extra1": "e" * 100,
                "summary": "the new field",
            },
        },
    }
    with pytest.raises(pytest.fail.Exception, match=r"is a subset of"):
        assert_no_contained_subtrees(bad)


def test_containment_helper_ignores_small_dicts():
    """Trivial dicts (under min_keys) don't trigger containment warnings."""
    payload = {
        "step_results": {
            "a": {"id": "x"},
            "b": {"id": "x", "extra": "y"},
        },
    }
    assert_no_contained_subtrees(payload)  # < 3 keys → ignored


def test_containment_helper_ignores_exact_duplicates():
    """Exact-equality is duplication, not containment — should not double-fire."""
    val = {"a": 1, "b": 2, "c": 3, "long": "x" * 500}
    payload = {
        "step_results": {
            "step_a": val,
            "step_b": val,  # exactly equal — duplication, not containment
        },
    }
    # Containment check passes (not a strict subset).
    assert_no_contained_subtrees(payload)


def test_step_result_accumulations_helper_basic():
    """The runtime helper picks out (upstream, downstream, size) tuples."""
    items = ["x" * 50] * 8
    step_results = {
        "categorize": {
            "items": items,
            "categories": ["c" * 30] * 5,
            "extra": "e" * 100,
        },
        "summarize": {
            "items": items,
            "categories": ["c" * 30] * 5,
            "extra": "e" * 100,
            "summary": "delta",
        },
    }
    pairs = find_step_result_accumulations(step_results)
    assert any(
        a == "categorize" and b == "summarize"
        for a, b, _ in pairs
    ), pairs


def test_no_silent_loss_helper_exempts_failed_and_skipped():
    """Failed and skipped ledger entries don't need step_results — should pass."""
    payload = {
        "auto_ran": [
            {"id": "ok_step", "name": "OK"},
            {"id": "bad_step", "name": "Bad", "error": "boom"},
            {"id": "skipped_step", "name": "Skipped", "skipped": True, "reason": "n/a"},
        ],
        "step_results": {"ok_step": {"x": 1}},
    }
    assert_auto_ran_ledger_has_corresponding_step_results(payload)


# ---------------------------------------------------------------------------
# Integration tests — exercise the real conductor codepaths
# ---------------------------------------------------------------------------
#
# These tests register a minimal in-memory workflow (via WorkflowDefinition),
# then drive ``start_workflow`` / ``advance_workflow`` against
# the real conductor.  They are the live regression gate for the
# canonical-home rule: ``auto_ran[*]`` is a ledger, ``prior_step`` is a
# pointer, and step result data lives in exactly one place
# (``step_results[id]``).


@pytest.fixture
def minimal_auto_run_workflow():
    """Register a 2-step workflow (auto_run scan -> reasoning report).

    The auto_run step returns a reasonably-sized dict so the duplication
    pattern is detectable.  The reasoning step is the surface where the
    conductor hands back to the agent.
    """
    from work_buddy.mcp_server.registry import (
        AutoRun,
        ResultVisibility,
        WorkflowDefinition,
        WorkflowStep,
        get_registry,
    )

    name = "test_minimal_auto_run"
    wf = WorkflowDefinition(
        name=name,
        description="Test fixture for response invariants.",
        workflow_file="test:in-memory",
        execution="main",
        steps=[
            WorkflowStep(
                id="scan",
                name="Scan (auto_run)",
                step_type="code",
                depends_on=[],
                instruction="",
                auto_run=AutoRun(
                    # Note the dotted path — the auto_run subprocess only
                    # accepts ``work_buddy.*`` callables, so we tuck the
                    # fake into the work_buddy package via this module.
                    callable="work_buddy.mcp_server._test_fakes.fake_scan_changes",
                    kwargs={},
                    input_map={},
                    timeout=15,
                ),
                visibility=ResultVisibility(mode="full"),
            ),
            WorkflowStep(
                id="report",
                name="Report (reasoning)",
                step_type="reasoning",
                depends_on=["scan"],
                instruction="Read scan and report.",
            ),
        ],
    )
    registry = get_registry()
    registry[name] = wf
    yield name
    registry.pop(name, None)


def test_start_response_has_no_duplication(minimal_auto_run_workflow):
    """``start_workflow`` response must not duplicate the auto_run output."""
    from work_buddy.mcp_server import conductor

    resp = conductor.start_workflow(minimal_auto_run_workflow)
    try:
        assert resp.get("type") == "workflow_step", f"unexpected response: {resp}"
        assert_no_duplicated_subtrees(resp)
        assert_auto_ran_ledger_has_corresponding_step_results(resp)
    finally:
        conductor._ACTIVE_RUNS.pop(resp.get("workflow_run_id"), None)


def test_advance_response_has_no_duplication(minimal_auto_run_workflow):
    """``advance_workflow`` response must not duplicate the prior step result."""
    from work_buddy.mcp_server import conductor

    start = conductor.start_workflow(minimal_auto_run_workflow)
    run_id = start["workflow_run_id"]
    try:
        # Advance through the reasoning step with a non-trivial result.
        report_payload = {"narrative": "X" * 600, "verdict": "ok"}
        adv = conductor.advance_workflow(run_id, step_result=report_payload)
        # This minimal workflow only has two steps, so advance completes it.
        assert adv.get("type") == "workflow_complete", f"unexpected: {adv}"
        assert_no_duplicated_subtrees(adv)
    finally:
        conductor._ACTIVE_RUNS.pop(run_id, None)


def test_complete_response_has_no_duplication(minimal_auto_run_workflow):
    """workflow_complete responses must satisfy the same invariant."""
    from work_buddy.mcp_server import conductor

    start = conductor.start_workflow(minimal_auto_run_workflow)
    run_id = start["workflow_run_id"]
    try:
        adv = conductor.advance_workflow(run_id, step_result={"narrative": "Y" * 600})
        assert adv.get("type") == "workflow_complete"
        assert_no_duplicated_subtrees(adv)
    finally:
        conductor._ACTIVE_RUNS.pop(run_id, None)


def test_auto_ran_ledger_has_corresponding_step_results_in_real_response(
    minimal_auto_run_workflow,
):
    """The conductor must surface every advertised auto_run step's data."""
    from work_buddy.mcp_server import conductor

    resp = conductor.start_workflow(minimal_auto_run_workflow)
    try:
        assert_auto_ran_ledger_has_corresponding_step_results(resp)
        # Also explicitly assert that the scan data made it through.
        assert "scan" in resp.get("step_results", {}), (
            f"step_results missing 'scan': {sorted(resp.get('step_results', {}).keys())}"
        )
    finally:
        conductor._ACTIVE_RUNS.pop(resp.get("workflow_run_id"), None)
