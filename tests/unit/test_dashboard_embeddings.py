"""C — the /api/embeddings data function: System (IR+knowledge) + User (vaults)."""
from __future__ import annotations

from work_buddy.dashboard import api
from work_buddy.indexing.protocol import IndexStatus, PartitionStatus


def setup_function():
    api._embeddings_cache = None
    api._embeddings_cache_ts = 0.0


def _patch(monkeypatch, *, statuses, vaults, db_mb=400.0, locked=False):
    monkeypatch.setattr("work_buddy.indexing.status.aggregate_status", lambda: statuses)
    monkeypatch.setattr("work_buddy.utils.index_lock.is_locked", lambda t: locked)
    monkeypatch.setattr("work_buddy.vault_index.status.effective_vault_configs", lambda: vaults)
    monkeypatch.setattr(
        "work_buddy.vault_index.status.index_status",
        lambda: {"status": "ok", "size_on_disk_mb": db_mb},
    )


def test_system_is_ir_and_knowledge_only(monkeypatch):
    _patch(
        monkeypatch,
        statuses=[
            IndexStatus(name="ir",
                        partitions=[PartitionStatus("conversation", 10, 8, 8, 0, size_on_disk_mb=1.2)],
                        size_on_disk_mb=1.2),
            IndexStatus(name="knowledge", partitions=[PartitionStatus("content", 5, 5, 5, 0)]),
            IndexStatus(name="vault_index",
                        partitions=[PartitionStatus("vault", 100, 100, 100, 0)], size_on_disk_mb=400.0),
        ],
        vaults=[{"id": "vault", "path": "/v", "is_default": True, "in_config": True,
                 "chunk_count": 100, "vector_count": 100, "pending": 0, "file_count": 5,
                 "health": "ok", "last_build": None}],
        locked=True,
    )
    out = api._build_embeddings_summary()
    assert {r["index"] for r in out["system"]} == {"ir", "knowledge"}  # vault_index excluded
    assert out["db_size_mb"] == 400.0
    assert out["vaults"][0]["building"] is True  # lock held → building surfaced on vault rows
    # IR partition size flows through; no fallback to index total for size-less rows
    conv = next(r for r in out["system"] if r["source"] == "conversation")
    assert conv["size_mb"] == 1.2


def test_caches_between_calls(monkeypatch):
    calls = {"n": 0}

    def agg():
        calls["n"] += 1
        return []

    monkeypatch.setattr("work_buddy.indexing.status.aggregate_status", agg)
    monkeypatch.setattr("work_buddy.utils.index_lock.is_locked", lambda t: False)
    monkeypatch.setattr("work_buddy.vault_index.status.effective_vault_configs", lambda: [])
    monkeypatch.setattr("work_buddy.vault_index.status.index_status",
                        lambda: {"status": "ok", "size_on_disk_mb": 1})
    a = api.get_embeddings_summary()
    b = api.get_embeddings_summary()
    assert a == b and calls["n"] == 1  # second call served from cache


def test_degrades_per_section(monkeypatch):
    def boom():
        raise RuntimeError("agg fail")

    monkeypatch.setattr("work_buddy.indexing.status.aggregate_status", boom)
    monkeypatch.setattr("work_buddy.utils.index_lock.is_locked", lambda t: False)
    monkeypatch.setattr("work_buddy.vault_index.status.effective_vault_configs",
                        lambda: [{"id": "v", "path": "/v"}])
    monkeypatch.setattr("work_buddy.vault_index.status.index_status",
                        lambda: {"status": "ok", "size_on_disk_mb": 1})
    out = api._build_embeddings_summary()
    assert "system_error" in out          # system failed...
    assert out["vaults"] and out["system"] == []  # ...but vaults still populated


def test_runtime_summary_reports_effective_pending_and_actual_route(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.settings.broker.get_values",
        lambda **_kwargs: ({
            "values": [{
                "effective_value": "prefer-lmstudio",
                "configured_value": "require-lmstudio",
                "pending_value": "require-lmstudio",
                "apply_status": "restart-required",
                "revision": "value:4",
            }],
        }, []),
    )
    monkeypatch.setattr(
        "work_buddy.embedding.client.health_status",
        lambda **_kwargs: {
            "status": "ok",
            "default_model": "leaf-mt",
            "models": [{
                "key": "leaf-ir",
                "name": "MongoDB/mdbr-leaf-ir-asym",
                "dims": 768,
                "status": "pending",
                "routing": {
                    "requested_provider": "lmstudio",
                    "provider_model": "remote-leaf",
                    "on_error": "fallback",
                    "cooldown_remaining_s": 0,
                    "last_route": {
                        "provider": "lmstudio",
                        "fallback": False,
                        "status": "ok",
                        "reason": "provider_success",
                    },
                },
            }],
        },
    )
    monkeypatch.setattr(
        "work_buddy.health.checks.check_lmstudio",
        lambda: {
            "ok": True,
            "detail": "reachable",
            "model_ids": ["remote-leaf", "another-model"],
        },
    )

    out = api.get_embedding_runtime_summary()

    assert out["policy"] == {
        "effective": "prefer-lmstudio",
        "configured": "require-lmstudio",
        "pending": "require-lmstudio",
        "apply_status": "restart-required",
        "revision": "value:4",
        "running": "prefer-lmstudio",
        "authority_matches_runtime": True,
    }
    assert out["document_model"]["loaded_locally"] is False
    assert out["document_model"]["routing"]["last_route"]["provider"] == "lmstudio"
    assert out["lmstudio"] == {
        "ok": True,
        "detail": "reachable",
        "model_ids": ["remote-leaf", "another-model"],
        "configured_model": "remote-leaf",
        "model_advertised": True,
        "model_available": True,
    }


def test_runtime_summary_does_not_treat_unadvertised_model_as_unavailable(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.settings.broker.get_values",
        lambda **_kwargs: ({"values": [{
            "effective_value": "require-lmstudio",
            "configured_value": "require-lmstudio",
            "pending_value": None,
            "apply_status": "effective",
            "revision": "value:1",
        }]}, []),
    )
    monkeypatch.setattr(
        "work_buddy.embedding.client.health_status",
        lambda **_kwargs: {
            "status": "ok",
            "models": [{
                "key": "leaf-ir",
                "status": "pending",
                "routing": {
                    "requested_provider": "lmstudio",
                    "provider_model": "required-model",
                    "on_error": "fail",
                    "last_route": None,
                },
            }],
        },
    )
    monkeypatch.setattr(
        "work_buddy.health.checks.check_lmstudio",
        lambda: {"ok": True, "detail": "reachable", "model_ids": ["other-model"]},
    )

    out = api.get_embedding_runtime_summary()

    assert out["lmstudio"]["ok"] is True
    assert out["lmstudio"]["configured_model"] == "required-model"
    assert out["lmstudio"]["model_advertised"] is False
    assert out["lmstudio"]["model_available"] is None


def test_runtime_summary_does_not_probe_lmstudio_for_local_policy(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.settings.broker.get_values",
        lambda **_kwargs: ({"values": [{
            "effective_value": "local",
            "configured_value": "local",
            "pending_value": None,
            "apply_status": "effective",
            "revision": "value:0",
        }]}, []),
    )
    monkeypatch.setattr(
        "work_buddy.embedding.client.health_status",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "work_buddy.health.checks.check_lmstudio",
        lambda: (_ for _ in ()).throw(AssertionError("local policy must not probe remote")),
    )

    out = api.get_embedding_runtime_summary()

    assert out["service_status"] == "unavailable"
    assert out["policy"]["running"] is None
    assert out["policy"]["authority_matches_runtime"] is None
    assert out["lmstudio"] is None


def test_runtime_summary_exposes_running_policy_mismatch(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.settings.broker.get_values",
        lambda **_kwargs: ({"values": [{
            "effective_value": "require-lmstudio",
            "configured_value": "require-lmstudio",
            "pending_value": None,
            "apply_status": "effective",
            "revision": "value:5",
        }]}, []),
    )
    monkeypatch.setattr(
        "work_buddy.embedding.client.health_status",
        lambda **_kwargs: {
            "status": "ok",
            "models": [{
                "key": "leaf-ir",
                "name": "MongoDB/mdbr-leaf-ir-asym",
                "dims": 768,
                "status": "pending",
                "routing": {
                    "requested_provider": "local",
                    "provider_model": "leaf-ir",
                    "on_error": "fallback",
                    "last_route": None,
                },
            }],
        },
    )
    monkeypatch.setattr(
        "work_buddy.health.checks.check_lmstudio",
        lambda: (_ for _ in ()).throw(
            AssertionError("actual local runtime must not probe LM Studio")
        ),
    )

    out = api.get_embedding_runtime_summary()

    assert out["policy"]["effective"] == "require-lmstudio"
    assert out["policy"]["running"] == "local"
    assert out["policy"]["authority_matches_runtime"] is False
    assert out["lmstudio"] is None


def test_runtime_summary_keeps_service_truth_when_settings_authority_fails(
    monkeypatch,
):
    monkeypatch.setattr(
        "work_buddy.settings.broker.get_values",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("settings offline")),
    )
    monkeypatch.setattr(
        "work_buddy.embedding.client.health_status",
        lambda **_kwargs: {
            "status": "ok",
            "models": [{
                "key": "leaf-ir",
                "name": "MongoDB/mdbr-leaf-ir-asym",
                "dims": 768,
                "status": "pending",
                "routing": {
                    "requested_provider": "lmstudio",
                    "provider_model": "remote-leaf",
                    "on_error": "fail",
                    "last_route": None,
                },
            }],
        },
    )
    monkeypatch.setattr(
        "work_buddy.health.checks.check_lmstudio",
        lambda: {"ok": True, "model_ids": ["remote-leaf"]},
    )

    out = api.get_embedding_runtime_summary()

    assert out["settings_error"] == "settings offline"
    assert out["policy"]["effective"] is None
    assert out["policy"]["running"] == "require-lmstudio"
    assert out["policy"]["authority_matches_runtime"] is None
    assert out["lmstudio"]["ok"] is True
