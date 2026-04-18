"""Regression: workflow steps marked ``requires_individual_consent: true``
must suspend the workflow blanket when they are handed to the agent (main
execution path), then re-grant on advance. Prior to this fix, the flag was
only honored for ``auto_run`` steps, so a main-execution step inside a
workflow silently bypassed its ``@requires_consent`` gate.
"""
from __future__ import annotations


def test_main_execution_individual_consent_suspends_blanket(monkeypatch):
    from work_buddy import consent as c
    from work_buddy.mcp_server import conductor

    # Capture grant/revoke calls
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        c, "grant_workflow_consent",
        lambda run_id, **_: calls.append(("grant", run_id)),
    )
    monkeypatch.setattr(
        c, "revoke_workflow_consent",
        lambda run_id="": calls.append(("revoke", run_id)),
    )

    # Build a minimal fake DAG node with requires_individual_consent set
    class FakeDAG:
        def __init__(self):
            self._graph = _FakeGraph()
            self._available = [{
                "task_id": "write",
                "name": "write step",
                "metadata": {"requires_individual_consent": True},
            }]

        def next_available(self):
            return self._available

        def is_complete(self):
            return False

        def start_task(self, tid):
            pass

        def complete_task(self, tid, result=None):
            self._available = []

        def summary(self):
            return "summary"

        def get_all_results(self):
            return {}

        def save(self):
            pass

    class _NodeView:
        _nodes = {
            "write": {
                "status": "running",
                "metadata": {"requires_individual_consent": True},
            },
        }

        def __call__(self, data=False):
            if data:
                return list(self._nodes.items())
            return list(self._nodes.keys())

        def __getitem__(self, key):
            return self._nodes[key]

    class _FakeGraph:
        def __init__(self):
            self.nodes = _NodeView()

        def number_of_nodes(self):
            return 1

    dag = FakeDAG()
    run_id = "wf_test"
    conductor._ACTIVE_RUNS[run_id] = dag
    try:
        # Stub out helpers that need real DAG internals
        monkeypatch.setattr(conductor, "_get_wf_def", lambda dag: None)
        monkeypatch.setattr(conductor, "_visibility_filter_results", lambda dag: {})
        monkeypatch.setattr(conductor, "_dag_to_mermaid", lambda dag: "")
        monkeypatch.setattr(
            conductor, "_safe_serialize", lambda x: x,
        )

        # Simulate handing the step to the agent — should revoke blanket
        conductor._build_response(run_id, dag)
        assert ("revoke", run_id) in calls, (
            "Main-execution step with requires_individual_consent did not "
            "suspend the workflow blanket"
        )

        # Simulate the agent completing the step — should re-grant blanket
        calls.clear()
        conductor.advance_workflow(run_id, step_result={"ok": True})
        assert any(c[0] == "grant" and c[1] == run_id for c in calls), (
            "Workflow blanket was not re-granted after individual-consent "
            "step completed"
        )
    finally:
        conductor._ACTIVE_RUNS.pop(run_id, None)
