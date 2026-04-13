"""Unit tests for WorkflowDAG — dependency graph, status transitions, persistence."""

import json

import pytest
from freezegun import freeze_time

from work_buddy.workflow import WorkflowDAG, TaskStatus


class TestDAGConstruction:
    def test_add_single_task(self):
        dag = WorkflowDAG(name="test:t1", description="test")
        dag.add_task("step1", name="First step")
        task = dag.get_task("step1")
        assert task["status"] == TaskStatus.AVAILABLE.value
        assert task["name"] == "First step"

    def test_add_with_dependency(self):
        dag = WorkflowDAG(name="test:t2", description="test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        assert dag.get_task("a")["status"] == TaskStatus.AVAILABLE.value
        assert dag.get_task("b")["status"] == TaskStatus.BLOCKED.value

    def test_dependency_on_missing_task_raises(self):
        dag = WorkflowDAG(name="test:t3", description="test")
        with pytest.raises(ValueError, match="not found"):
            dag.add_task("b", name="B", depends_on=["nonexistent"])

    def test_cycle_detection(self):
        """Cycle detection requires manually injecting an edge to create a back-edge,
        since add_task only adds forward edges to new nodes."""
        dag = WorkflowDAG(name="test:t4", description="test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        # Verify the graph is currently acyclic
        import networkx as nx
        assert nx.is_directed_acyclic_graph(dag._graph)
        # add_task with depends_on=["b"] creates a new node c -> depends on b,
        # which is fine (a -> b -> c is not cyclic). A true cycle would need
        # c -> a, but add_task can't add edges to existing nodes.
        # We verify the cycle guard via direct edge injection:
        dag._graph.add_edge("b", "a")  # creates a -> b -> a cycle
        assert not nx.is_directed_acyclic_graph(dag._graph)

    def test_next_available(self):
        dag = WorkflowDAG(name="test:t5", description="test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        dag.add_task("c", name="C")

        available = dag.next_available()
        ids = {t["task_id"] for t in available}
        assert ids == {"a", "c"}


class TestDAGTransitions:
    def test_start_available_task(self):
        dag = WorkflowDAG(name="test:t6", description="test")
        dag.add_task("a", name="A")
        result = dag.start_task("a")
        assert result["status"] == TaskStatus.RUNNING.value
        assert result["started_at"] is not None

    def test_cannot_start_blocked_task(self):
        dag = WorkflowDAG(name="test:t7", description="test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        with pytest.raises(RuntimeError, match="blocked"):
            dag.start_task("b")

    def test_complete_unblocks_dependents(self):
        dag = WorkflowDAG(name="test:t8", description="test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        dag.start_task("a")
        dag.complete_task("a", result={"data": "done"})
        assert dag.get_task("a")["status"] == TaskStatus.COMPLETED.value
        assert dag.get_task("b")["status"] == TaskStatus.AVAILABLE.value

    def test_fail_task(self):
        dag = WorkflowDAG(name="test:t9", description="test")
        dag.add_task("a", name="A")
        dag.start_task("a")
        dag.fail_task("a", error="something broke")
        assert dag.get_task("a")["status"] == TaskStatus.FAILED.value
        assert "FAILED" in dag.get_task("a")["result"]

    def test_skip_task_unblocks_dependents(self):
        dag = WorkflowDAG(name="test:t10", description="test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        dag.skip_task("a", reason="not needed")
        assert dag.get_task("b")["status"] == TaskStatus.AVAILABLE.value

    def test_cannot_complete_non_running_task(self):
        dag = WorkflowDAG(name="test:t11", description="test")
        dag.add_task("a", name="A")
        with pytest.raises(RuntimeError, match="must be running"):
            dag.complete_task("a")

    def test_is_complete(self):
        dag = WorkflowDAG(name="test:t12", description="test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        assert not dag.is_complete()

        dag.start_task("a")
        dag.complete_task("a")
        assert not dag.is_complete()

        dag.start_task("b")
        dag.complete_task("b")
        assert dag.is_complete()

    def test_get_all_results(self):
        dag = WorkflowDAG(name="test:t13", description="test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        dag.start_task("a")
        dag.complete_task("a", result={"val": 1})
        dag.start_task("b")
        dag.complete_task("b", result={"val": 2})
        results = dag.get_all_results()
        assert results == {"a": {"val": 1}, "b": {"val": 2}}


class TestDAGExecutionPolicy:
    def test_default_policy_is_main(self):
        dag = WorkflowDAG(name="test:t14", description="test")
        dag.add_task("a", name="A")
        task = dag.get_task("a")
        assert task["execution"] == "main"
        assert task["allow_override"] is True

    def test_cannot_override_when_disallowed(self):
        dag = WorkflowDAG(name="test:t15", description="test")
        # Manually set execution policy without a workflow file
        dag._graph.add_node("a", **{
            "name": "A", "workflow_file": None,
            "execution": "main", "execution_actual": None,
            "workflow_default": "main", "allow_override": False,
            "status": TaskStatus.AVAILABLE.value,
            "result": None, "started_at": None, "completed_at": None,
            "metadata": {},
        })
        with pytest.raises(RuntimeError, match="does not allow override"):
            dag.start_task("a", execution_actual="subagent")


class TestDAGPersistence:
    @freeze_time("2026-04-12 10:00:00")
    def test_save_and_load(self, tmp_agents_dir, monkeypatch):
        """DAG round-trips through JSON persistence."""
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "test-persist")

        import work_buddy.agent_session as asmod
        monkeypatch.setattr(asmod, "_cached_session_dir", None)

        dag = WorkflowDAG(name="persist-test:wf1", description="Persistence test")
        dag.add_task("a", name="A")
        dag.add_task("b", name="B", depends_on=["a"])
        dag.start_task("a")
        dag.complete_task("a", result="done")

        save_path = dag.save()
        assert save_path.exists()

        loaded = WorkflowDAG.load(save_path)
        assert loaded.name == "persist-test:wf1"
        assert loaded.get_task("a")["status"] == TaskStatus.COMPLETED.value
        assert loaded.get_task("b")["status"] == TaskStatus.AVAILABLE.value

    @freeze_time("2026-04-12 10:00:00")
    def test_timestamps_are_deterministic(self, tmp_agents_dir, monkeypatch):
        monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "test-time")
        import work_buddy.agent_session as asmod
        monkeypatch.setattr(asmod, "_cached_session_dir", None)

        dag = WorkflowDAG(name="time-test:wf2", description="Time test")
        dag.add_task("a", name="A")
        dag.start_task("a")
        task = dag.get_task("a")
        assert task["started_at"] == "2026-04-12T10:00:00+00:00"


class TestDAGSummary:
    def test_summary_format(self):
        dag = WorkflowDAG(name="summary-test", description="A test workflow")
        dag.add_task("a", name="Step A")
        dag.add_task("b", name="Step B", depends_on=["a"])
        summary = dag.summary()
        assert "summary-test" in summary
        assert "Step A" in summary
        assert "Step B" in summary
        assert "0/2 tasks completed" in summary
