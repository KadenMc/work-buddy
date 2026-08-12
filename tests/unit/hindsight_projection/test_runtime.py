from __future__ import annotations

from dataclasses import dataclass

from work_buddy.hindsight_projection.runtime import run_projection_tick
from work_buddy.hindsight_projection.service import (
    ProcessResult,
    ReconciliationReport,
)
from work_buddy.hindsight_projection.redaction_dispatch import (
    RedactionDispatchReport,
)
from work_buddy.mcp_server.op_registry import get_op


@dataclass
class _TruthStore:
    store_id: str
    paths: object = type("Paths", (), {"db": "truth.db"})()


class _Registry:
    def __init__(self) -> None:
        self.store = _TruthStore("a" * 32)

    def open_store(self, store_id):
        assert store_id == self.store.store_id
        return self.store

    def list_stores(self, refresh=True):
        raise AssertionError("an exact store tick must not scan the registry")


class _Service:
    def __init__(self) -> None:
        self.calls = 0

    def reconcile_truth(self):
        return ReconciliationReport(1, 2, 0, 0, 0)

    def process_next(self, *, worker_id):
        assert worker_id.startswith("truth-hindsight-")
        self.calls += 1
        if self.calls == 1:
            return ProcessResult("delivered", "effect-1")
        return ProcessResult("idle")


class _Redactions:
    def prepare(self, *, limit):
        return ()

    def settle(self, prepared):
        return RedactionDispatchReport()


def test_runtime_entrypoint_reconciles_and_drains_bounded_work(monkeypatch) -> None:
    service = _Service()
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.runtime.TruthHindsightProjectionStore",
        lambda _path: type(
            "ProjectionStore",
            (),
            {"has_tracked_projection_state": lambda self: True},
        )(),
    )
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.runtime.build_projection_service",
        lambda store, config=None: service,
    )
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.runtime._build_redaction_dispatcher",
        lambda *args, **kwargs: _Redactions(),
    )

    result = run_projection_tick(
        store_id="a" * 32,
        limit_per_store=5,
        registry=_Registry(),
        config={},
    )

    assert result["ok"] is True
    assert result["stores"] == [
        {
            "store_id": "a" * 32,
            "ok": True,
            "reconciliation": {
                "enqueued": 1,
                "unchanged": 2,
                "destination_repairs": 0,
                "dependency_repairs": 0,
                "source_cleanups": 0,
            },
            "states": {"delivered": 1, "idle": 1},
            "errors": {},
            "source_redactions": {
                "prepared": 0,
                "completed": 0,
                "deferred": 0,
                "failed": 0,
            },
        }
    ]


def test_capability_op_is_registered_without_importing_optional_hindsight_client() -> None:
    import work_buddy.mcp_server.ops.hindsight_projection_ops  # noqa: F401

    assert get_op("op.wb.truth_hindsight_projection_tick") is not None


def test_disabled_untracked_rollout_is_dormant_before_service_composition(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.runtime.TruthHindsightProjectionStore",
        lambda _path: type(
            "ProjectionStore",
            (),
            {"has_tracked_projection_state": lambda self: False},
        )(),
    )
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.runtime.build_projection_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled untracked rollout must not compose services")
        ),
    )

    result = run_projection_tick(
        store_id="a" * 32,
        registry=_Registry(),
        config={},
    )

    assert result["ok"] is True
    assert result["stores"][0]["state"] == "dormant"


def test_tick_does_not_spin_its_budget_on_one_durable_retry(monkeypatch) -> None:
    class RetryingService(_Service):
        def reconcile_truth(self):
            return ReconciliationReport(0, 0, 0, 0, 0)

        def process_next(self, *, worker_id):
            self.calls += 1
            return ProcessResult(
                "failed_retryable",
                "effect-1",
                "source_dependency_reservation_failed",
            )

    service = RetryingService()
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.runtime.TruthHindsightProjectionStore",
        lambda _path: type(
            "ProjectionStore",
            (),
            {"has_tracked_projection_state": lambda self: True},
        )(),
    )
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.runtime.build_projection_service",
        lambda store, config=None: service,
    )
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.runtime._build_redaction_dispatcher",
        lambda *args, **kwargs: _Redactions(),
    )

    result = run_projection_tick(
        store_id="a" * 32,
        limit_per_store=500,
        registry=_Registry(),
        config={},
    )

    assert service.calls == 1
    assert result["stores"][0]["states"] == {"failed_retryable": 1}
