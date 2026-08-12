from __future__ import annotations

from types import SimpleNamespace

from work_buddy.truth import source_reconciliation as subject


def test_reconciler_reports_one_unavailable_store_without_stranding_others(
    monkeypatch,
) -> None:
    class Registry:
        @staticmethod
        def list_stores(*, refresh: bool):
            assert refresh is True
            return (
                SimpleNamespace(store_id="unavailable", reachable=True),
                SimpleNamespace(store_id="healthy", reachable=True),
            )

        @staticmethod
        def open_store(store_id: str):
            if store_id == "unavailable":
                raise RuntimeError("store moved during reconciliation")
            return SimpleNamespace(store_id=store_id)

    source_store = object()
    monkeypatch.setattr(subject, "resolve", lambda _resource: "sources")
    monkeypatch.setattr(
        subject,
        "SourceStore",
        SimpleNamespace(create=lambda _path: source_store),
    )
    monkeypatch.setattr(
        subject,
        "get_default_authority",
        lambda: SimpleNamespace(
            enrolled_actor=lambda: SimpleNamespace(
                issuer_authority_id="issuer-test-0001",
                tenant_scope_id="tenant-test-0001",
            )
        ),
    )

    examined: list[str] = []

    def reconcile(truth_store, sources, *, actor, limit):
        assert sources is source_store
        assert actor.kind == "service"
        assert limit == 17
        examined.append(truth_store.store_id)
        return {
            "examined": 1,
            "acknowledged": 1,
            "pending": 0,
            "redaction_pending": 0,
        }

    monkeypatch.setattr(subject, "reconcile_pending_source_usages", reconcile)
    result = subject.reconcile_truth_source_usages(
        limit_per_store=17,
        registry=Registry(),
    )

    assert examined == ["healthy"]
    assert result["ok"] is False
    assert result["stores"] == [
        {
            "store_id": "healthy",
            "ok": True,
            "examined": 1,
            "acknowledged": 1,
            "pending": 0,
            "redaction_pending": 0,
        },
        {
            "store_id": "unavailable",
            "ok": False,
            "error_code": "RuntimeError",
        },
    ]
