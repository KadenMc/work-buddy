"""Unit tests for workflow run lifecycle — cancel, idle sweep, restart recovery.

Covers ``conductor.cancel_workflow`` / ``sweep_idle_runs`` /
``recover_active_runs`` and the ``_load_dag_from_disk`` import-bug fix.
The ``_ACTIVE_RUNS`` module global is cleared around every test; the
workflow-consent revoke is stubbed so no real consent DB is touched.
"""

from __future__ import annotations

import json

import pytest
from freezegun import freeze_time

from work_buddy.mcp_server import conductor
from work_buddy.workflow import WorkflowDAG


@pytest.fixture(autouse=True)
def _clean_active_runs():
    """Each test starts and ends with an empty _ACTIVE_RUNS map."""
    conductor._ACTIVE_RUNS.clear()
    yield
    conductor._ACTIVE_RUNS.clear()


@pytest.fixture(autouse=True)
def _no_consent_db(monkeypatch):
    """Stub the workflow-consent revoke so tests never touch a real DB."""
    calls: list[dict] = []

    def _fake_revoke(workflow_run_id="", *, session_id=None):
        calls.append({"workflow_run_id": workflow_run_id, "session_id": session_id})

    import work_buddy.consent as consent_mod
    monkeypatch.setattr(consent_mod, "revoke_workflow_consent", _fake_revoke)
    return calls


def _make_dag(name: str, *, session_id: str = "agent-sess-1",
              started: bool = True, complete: bool = False) -> WorkflowDAG:
    """Build a minimal one-task DAG for lifecycle tests."""
    dag = WorkflowDAG(name=name, description="test")
    dag.agent_session_id = session_id
    dag.add_task("a", name="Step A")
    if started or complete:
        dag.start_task("a")
    if complete:
        dag.complete_task("a", result="done")
    return dag


# ---------------------------------------------------------------------------
# cancel_workflow
# ---------------------------------------------------------------------------

class TestCancelWorkflow:
    def test_cancel_active_run(self, tmp_agents_dir):
        dag = _make_dag("triage:wf_active1")
        conductor._ACTIVE_RUNS["wf_active1"] = dag

        result = conductor.cancel_workflow("wf_active1", reason="user_requested")

        assert result["cancelled"] is True
        assert result["was_active"] is True
        assert result["reason"] == "user_requested"
        assert "wf_active1" not in conductor._ACTIVE_RUNS
        # On-disk DAG records the cancellation.
        reloaded = WorkflowDAG.load(dag._get_save_path())
        assert reloaded.cancelled is True
        assert reloaded.cancelled_reason == "user_requested"

    def test_cancel_revokes_consent_with_pinned_session(self, tmp_agents_dir,
                                                        _no_consent_db):
        dag = _make_dag("triage:wf_consent1", session_id="agent-XYZ")
        conductor._ACTIVE_RUNS["wf_consent1"] = dag

        conductor.cancel_workflow("wf_consent1")

        assert _no_consent_db == [
            {"workflow_run_id": "wf_consent1", "session_id": "agent-XYZ"}
        ]

    def test_cancel_disk_only_run(self, tmp_agents_dir):
        """A run not in _ACTIVE_RUNS is still cancellable via its disk DAG."""
        dag = _make_dag("triage:wf_disk1")
        dag.save()  # on disk, but NOT registered in _ACTIVE_RUNS

        result = conductor.cancel_workflow("wf_disk1", reason="cleanup")

        assert result["cancelled"] is True
        assert result["was_active"] is False
        reloaded = WorkflowDAG.load(dag._get_save_path())
        assert reloaded.cancelled is True

    def test_cancel_unknown_run(self, tmp_agents_dir):
        result = conductor.cancel_workflow("wf_nonexistent")
        assert result["cancelled"] is False
        assert "error" in result

    def test_cancel_complete_run_refused(self, tmp_agents_dir):
        dag = _make_dag("triage:wf_done1", complete=True)
        conductor._ACTIVE_RUNS["wf_done1"] = dag

        result = conductor.cancel_workflow("wf_done1")

        assert result["cancelled"] is False
        assert "complete" in result["detail"].lower()
        # A refused cancel leaves the run in place.
        assert "wf_done1" in conductor._ACTIVE_RUNS

    def test_cancel_is_idempotent(self, tmp_agents_dir):
        dag = _make_dag("triage:wf_idem1")
        conductor._ACTIVE_RUNS["wf_idem1"] = dag

        first = conductor.cancel_workflow("wf_idem1")
        second = conductor.cancel_workflow("wf_idem1")

        assert first["cancelled"] is True
        assert second["cancelled"] is True
        assert second.get("already_cancelled") is True


# ---------------------------------------------------------------------------
# sweep_idle_runs
# ---------------------------------------------------------------------------

class TestSweepIdleRuns:
    def test_sweep_cancels_idle_run(self, tmp_agents_dir):
        with freeze_time("2026-05-01 12:00:00"):
            dag = _make_dag("triage:wf_idle1")
            conductor._ACTIVE_RUNS["wf_idle1"] = dag
        # 48h later — past the 24h threshold.
        with freeze_time("2026-05-03 12:00:00"):
            result = conductor.sweep_idle_runs(idle_threshold_hours=24)

        assert "wf_idle1" in result["cancelled"]
        assert "wf_idle1" not in conductor._ACTIVE_RUNS

    def test_sweep_spares_fresh_run(self, tmp_agents_dir):
        with freeze_time("2026-05-03 11:00:00"):
            dag = _make_dag("triage:wf_fresh1")
            conductor._ACTIVE_RUNS["wf_fresh1"] = dag
        # Only 1h later — well under threshold.
        with freeze_time("2026-05-03 12:00:00"):
            result = conductor.sweep_idle_runs(idle_threshold_hours=24)

        assert result["cancelled"] == []
        assert "wf_fresh1" in conductor._ACTIVE_RUNS

    def test_sweep_dry_run_lists_but_does_not_cancel(self, tmp_agents_dir):
        with freeze_time("2026-05-01 12:00:00"):
            dag = _make_dag("triage:wf_dry1")
            conductor._ACTIVE_RUNS["wf_dry1"] = dag
        with freeze_time("2026-05-03 12:00:00"):
            result = conductor.sweep_idle_runs(idle_threshold_hours=24, dry_run=True)

        assert result["dry_run"] is True
        assert any(c["workflow_run_id"] == "wf_dry1" for c in result["candidates"])
        assert result["cancelled"] == []
        assert "wf_dry1" in conductor._ACTIVE_RUNS  # untouched

    def test_sweep_skips_complete_run(self, tmp_agents_dir):
        with freeze_time("2026-05-01 12:00:00"):
            dag = _make_dag("triage:wf_cmpl1", complete=True)
            conductor._ACTIVE_RUNS["wf_cmpl1"] = dag
        with freeze_time("2026-05-03 12:00:00"):
            result = conductor.sweep_idle_runs(idle_threshold_hours=24)

        # A complete run is not "idle" — it's done; leave it for advance_workflow.
        assert result["cancelled"] == []

    def test_sweep_threshold_defaults_from_config(self, tmp_agents_dir, monkeypatch):
        monkeypatch.setattr(
            conductor, "_idle_threshold_hours",
            lambda override=None: 99.0 if override is None else float(override),
        )
        result = conductor.sweep_idle_runs()
        assert result["threshold_hours"] == 99.0

    def test_sweep_survives_concurrent_mutation(self, tmp_agents_dir):
        """The sweep snapshots _ACTIVE_RUNS under a lock — a concurrent
        insert/remove must never raise 'dict changed size during iteration'."""
        import threading

        stop = threading.Event()

        def _churn() -> None:
            i = 0
            while not stop.is_set():
                key = f"wf_churn{i}"
                conductor._ACTIVE_RUNS[key] = _make_dag(f"t:{key}", started=False)
                conductor._ACTIVE_RUNS.pop(key, None)
                i += 1

        churner = threading.Thread(target=_churn, daemon=True)
        churner.start()
        try:
            for _ in range(50):
                conductor.sweep_idle_runs(idle_threshold_hours=24)  # must not raise
        finally:
            stop.set()
            churner.join(timeout=2)


# ---------------------------------------------------------------------------
# recover_active_runs
# ---------------------------------------------------------------------------

class TestRecoverActiveRuns:
    def test_recovery_repopulates_incomplete_run(self, tmp_agents_dir):
        with freeze_time("2026-05-20 12:00:00"):
            dag = _make_dag("triage:wf_recov1", started=False)
            dag.save()
            result = conductor.recover_active_runs(idle_threshold_hours=24)

        assert "wf_recov1" in result["recovered"]
        assert "wf_recov1" in conductor._ACTIVE_RUNS

    def test_recovery_skips_complete_run(self, tmp_agents_dir):
        with freeze_time("2026-05-20 12:00:00"):
            dag = _make_dag("triage:wf_recovdone", complete=True)
            dag.save()
            result = conductor.recover_active_runs(idle_threshold_hours=24)

        assert "wf_recovdone" not in conductor._ACTIVE_RUNS
        assert result["skipped"] >= 1

    def test_recovery_skips_cancelled_run(self, tmp_agents_dir):
        with freeze_time("2026-05-20 12:00:00"):
            dag = _make_dag("triage:wf_recovcanc", started=False)
            dag.mark_cancelled("user_requested")
            result = conductor.recover_active_runs(idle_threshold_hours=24)

        assert "wf_recovcanc" not in conductor._ACTIVE_RUNS

    def test_recovery_expires_idle_run(self, tmp_agents_dir):
        # Created 3 days ago, never advanced — idle past the 24h threshold.
        with freeze_time("2026-05-17 12:00:00"):
            dag = _make_dag("triage:wf_recovidle", started=False)
            dag.save()
        with freeze_time("2026-05-20 12:00:00"):
            result = conductor.recover_active_runs(idle_threshold_hours=24)

        assert "wf_recovidle" in result["expired"]
        assert "wf_recovidle" not in conductor._ACTIVE_RUNS
        # Expired runs are marked cancelled on disk so they don't resurface.
        reloaded = WorkflowDAG.load(dag._get_save_path())
        assert reloaded.cancelled is True
        assert reloaded.cancelled_reason == "idle_timeout"

    def test_recovery_restores_agent_session_id(self, tmp_agents_dir):
        with freeze_time("2026-05-20 12:00:00"):
            dag = _make_dag("triage:wf_recovsess", session_id="agent-PINNED",
                            started=False)
            dag.save()
            conductor.recover_active_runs(idle_threshold_hours=24)

        assert conductor._ACTIVE_RUNS["wf_recovsess"].agent_session_id == "agent-PINNED"

    def test_recovery_tolerates_corrupt_file(self, tmp_agents_dir):
        wf_dir = tmp_agents_dir / "2026-05-20T00-00-00_corrupt0" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

        # Must not raise — corrupt files are counted and skipped.
        result = conductor.recover_active_runs(idle_threshold_hours=24)
        assert result["errors"] >= 1

    def test_recovery_skips_unkeyable_name(self, tmp_agents_dir):
        """A DAG whose name has no ':' yields no parseable run id."""
        with freeze_time("2026-05-20 12:00:00"):
            dag = WorkflowDAG(name="noколон", description="x")
            dag.add_task("a", name="A")
            dag.save()
            result = conductor.recover_active_runs(idle_threshold_hours=24)

        assert result["recovered"] == []
        assert result["skipped"] >= 1


# ---------------------------------------------------------------------------
# _load_dag_from_disk — BUG 1 (get_agent_dir ImportError) regression
# ---------------------------------------------------------------------------

class TestLoadDagFromDisk:
    def test_load_dag_from_disk_does_not_raise_importerror(self, tmp_agents_dir):
        """Regression: the old code imported a non-existent get_agent_dir."""
        result = conductor._load_dag_from_disk("wf_whatever")
        assert result is None  # nothing on disk — but no ImportError

    def test_load_dag_from_disk_finds_run_by_id(self, tmp_agents_dir):
        dag = _make_dag("triage:wf_findme01")
        dag.save()
        found = conductor._load_dag_from_disk("wf_findme01")
        assert found is not None
        assert found.name == "triage:wf_findme01"


# ---------------------------------------------------------------------------
# _run_last_activity
# ---------------------------------------------------------------------------

class TestRunLastActivity:
    def test_uses_freshest_node_timestamp(self, tmp_agents_dir):
        with freeze_time("2026-05-01 09:00:00"):
            dag = WorkflowDAG(name="t:wf_la1", description="x")
            dag.add_task("a", name="A")
        with freeze_time("2026-05-01 15:00:00"):
            dag.start_task("a")  # started_at = 15:00

        last = conductor._run_last_activity(dag)
        assert last.hour == 15

    def test_falls_back_to_created_at(self, tmp_agents_dir):
        with freeze_time("2026-05-01 08:00:00"):
            dag = WorkflowDAG(name="t:wf_la2", description="x")
            dag.add_task("a", name="A")  # no task started

        last = conductor._run_last_activity(dag)
        assert last.hour == 8


# ---------------------------------------------------------------------------
# Gateway startup wiring (server._recover_workflow_runs)
# ---------------------------------------------------------------------------

class TestServerStartupWiring:
    def test_recovery_skipped_when_disabled(self, monkeypatch):
        from work_buddy.mcp_server import server
        import work_buddy.config as config_mod

        called: list[bool] = []
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda *a, **k: {
                "workflows": {"run_lifecycle": {"recovery_enabled": False}}
            },
        )
        monkeypatch.setattr(
            conductor, "recover_active_runs",
            lambda *a, **k: called.append(True) or {},
        )
        server._recover_workflow_runs()
        assert called == []  # the config kill-switch was honored

    def test_recovery_runs_when_enabled(self, monkeypatch):
        from work_buddy.mcp_server import server
        import work_buddy.config as config_mod

        called: list[bool] = []
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda *a, **k: {
                "workflows": {"run_lifecycle": {"recovery_enabled": True}}
            },
        )
        monkeypatch.setattr(
            conductor, "recover_active_runs",
            lambda *a, **k: called.append(True) or {"recovered": [], "expired": []},
        )
        server._recover_workflow_runs()
        assert called == [True]

    def test_recovery_swallows_errors(self, monkeypatch):
        """A recovery failure must never block the gateway from booting."""
        from work_buddy.mcp_server import server
        import work_buddy.config as config_mod

        monkeypatch.setattr(
            config_mod, "load_config",
            lambda *a, **k: {
                "workflows": {"run_lifecycle": {"recovery_enabled": True}}
            },
        )

        def _boom(*a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(conductor, "recover_active_runs", _boom)
        # Must not raise.
        server._recover_workflow_runs()


# ---------------------------------------------------------------------------
# End-to-end — start a real run through the conductor, then cancel / recover
# ---------------------------------------------------------------------------

@pytest.fixture
def _minimal_workflow():
    """Register a one-step reasoning workflow for end-to-end exercises."""
    from work_buddy.mcp_server.registry import (
        WorkflowDefinition, WorkflowStep, get_registry,
    )

    name = "test_lifecycle_e2e"
    wf = WorkflowDefinition(
        name=name,
        description="End-to-end lifecycle fixture.",
        workflow_file="test:in-memory",
        execution="main",
        steps=[
            WorkflowStep(
                id="only", name="Only step", step_type="reasoning",
                depends_on=[], instruction="Do the thing.",
            ),
        ],
    )
    registry = get_registry()
    registry[name] = wf
    yield name
    registry.pop(name, None)


class TestEndToEnd:
    def test_start_persists_real_json_then_cancel(self, tmp_agents_dir,
                                                  _minimal_workflow):
        start = conductor.start_workflow(_minimal_workflow)
        run_id = start["workflow_run_id"]
        assert run_id in conductor._ACTIVE_RUNS

        # BUG 2 end-to-end: the persisted DAG is a real, non-empty .json,
        # not a 0-byte colon-mangled husk.
        dag = conductor._ACTIVE_RUNS[run_id]
        path = dag._get_save_path()
        assert path.exists() and path.suffix == ".json"
        assert path.stat().st_size > 0
        assert ":" not in path.name

        result = conductor.cancel_workflow(run_id, reason="user_requested")
        assert result["cancelled"] is True
        assert run_id not in conductor._ACTIVE_RUNS

        # A cancelled run is not resurrected by restart recovery.
        conductor._ACTIVE_RUNS.clear()
        conductor.recover_active_runs(idle_threshold_hours=24)
        assert run_id not in conductor._ACTIVE_RUNS

    def test_start_then_restart_recovery_repopulates(self, tmp_agents_dir,
                                                     _minimal_workflow):
        start = conductor.start_workflow(_minimal_workflow)
        run_id = start["workflow_run_id"]

        # Simulate an MCP-server restart: the in-memory map is wiped.
        conductor._ACTIVE_RUNS.clear()
        assert run_id not in conductor._ACTIVE_RUNS

        rec = conductor.recover_active_runs(idle_threshold_hours=24)
        assert run_id in rec["recovered"]
        assert run_id in conductor._ACTIVE_RUNS
