"""End-to-end provider policy coverage for the online /embed document path."""

from __future__ import annotations

import numpy as np
import pytest

from work_buddy.index.encode import LocalProvider, ProviderRouter, default_encoder
from work_buddy.inference import Priority


class _Provider:
    def __init__(self, name: str, *, available: bool = True) -> None:
        self.name = name
        self.available = available
        self.calls: list[dict] = []

    def encode(
        self,
        texts,
        *,
        model_id,
        prompt_name=None,
        priority=Priority.BACKGROUND,
        batch_size=None,
    ):
        self.calls.append({
            "texts": list(texts),
            "model_id": model_id,
            "prompt_name": prompt_name,
            "priority": priority,
            "batch_size": batch_size,
        })
        if not self.available:
            return None
        return np.ones((len(texts), 3), dtype=np.float32)


def _cfg(*, on_error: str = "fallback", alias: str | None = "remote-leaf-ir"):
    model = {
        "provider": "lmstudio",
        "on_error": on_error,
    }
    if alias is not None:
        model["lmstudio_model"] = alias
    return {"embedding": {"models": {"leaf-ir": model}}}


@pytest.fixture
def service(monkeypatch):
    import work_buddy.embedding.service as service_module
    from work_buddy.llm import provenance

    rows: list[dict] = []
    monkeypatch.setattr(
        provenance,
        "record_inference_call",
        lambda **kwargs: rows.append(kwargs),
    )
    monkeypatch.setattr(service_module, "_provider_router", None)
    monkeypatch.setattr(service_module, "_document_policy_authority_error", None)
    return service_module, rows


def test_document_embed_uses_remote_alias_without_loading_local(service, monkeypatch):
    svc, rows = service
    local = _Provider("local")
    remote = _Provider("lmstudio")
    router = ProviderRouter(
        providers={"local": local, "lmstudio": remote},
        cfg=_cfg(on_error="fail"),
    )
    monkeypatch.setattr(svc, "_provider_router", router)
    monkeypatch.setattr(
        svc,
        "_get_model",
        lambda _key: (_ for _ in ()).throw(
            AssertionError("remote document encode must not load a local model")
        ),
    )

    with svc.app.test_client() as client:
        response = client.post("/embed", json={
            "texts": ["one", "two"],
            "model": "leaf-ir",
            "prompt_name": "document",
            "call_id": "caller-batch-1",
        })

    assert response.status_code == 200
    body = response.get_json()
    assert body["vectors"] == [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]
    assert body["dims"] == 3 and body["count"] == 2 and body["model"] == "leaf-ir"
    assert body["route"]["provider"] == "lmstudio"
    assert body["route"]["fallback"] is False
    assert remote.calls == [{
        "texts": ["one", "two"],
        "model_id": "remote-leaf-ir",
        "prompt_name": "document",
        "priority": Priority.BACKGROUND,
        "batch_size": 32,
    }]
    assert not local.calls
    assert rows[-1]["provider"] == "lmstudio"
    assert rows[-1]["status"] == "ok"
    assert rows[-1]["call_id"] == "caller-batch-1"


def test_document_embed_can_suppress_per_request_provenance(service, monkeypatch):
    """An aggregate caller can prevent N service rows for its N chunks."""
    svc, rows = service
    remote = _Provider("lmstudio")
    monkeypatch.setattr(
        svc,
        "_provider_router",
        ProviderRouter(
            providers={"local": _Provider("local"), "lmstudio": remote},
            cfg=_cfg(on_error="fail"),
        ),
    )

    with svc.app.test_client() as client:
        response = client.post("/embed", json={
            "texts": ["one"],
            "model": "leaf-ir",
            "routing_role": "document",
            "record_provenance": False,
        })

    assert response.status_code == 200
    assert response.get_json()["route"]["provider"] == "lmstudio"
    assert rows == []


def test_document_embed_local_only_uses_service_registry_not_remote(
    service, monkeypatch,
):
    svc, rows = service
    remote = _Provider("lmstudio")
    cfg = {"embedding": {"models": {"leaf-ir": {
        "provider": "local",
        "on_error": "fail",
    }}}}
    router = ProviderRouter(
        providers={
            "local": LocalProvider(in_service=True),
            "lmstudio": remote,
        },
        cfg=cfg,
    )
    loaded: list[str] = []

    class _Model:
        def encode(self, texts, **_kwargs):
            return np.full((len(texts), 2), 4.0, dtype=np.float32)

    def _get_model(key, **_kwargs):
        loaded.append(key)
        return _Model()

    monkeypatch.setattr(svc, "_provider_router", router)
    monkeypatch.setattr(svc, "_get_model", _get_model)
    monkeypatch.setattr(
        svc,
        "_brokered_encode",
        lambda model, texts, **kwargs: model.encode(texts, **kwargs),
    )

    with svc.app.test_client() as client:
        response = client.post("/embed", json={
            "texts": ["one"], "model": "leaf-ir", "prompt_name": "document",
        })

    assert response.status_code == 200
    assert response.get_json()["route"]["reason"] == "configured_local"
    assert loaded == ["leaf-ir"]
    assert not remote.calls
    assert rows[-1]["provider"] == "local"


def test_document_embed_fallback_reports_actual_local_route(service, monkeypatch):
    svc, rows = service
    local = _Provider("local")
    remote = _Provider("lmstudio", available=False)
    monkeypatch.setattr(
        svc,
        "_provider_router",
        ProviderRouter(
            providers={"local": local, "lmstudio": remote},
            cfg=_cfg(on_error="fallback"),
        ),
    )

    with svc.app.test_client() as client:
        response = client.post("/embed", json={
            "texts": ["one"], "model": "leaf-ir", "prompt_name": "document",
        })

    assert response.status_code == 200
    route = response.get_json()["route"]
    assert route["requested_provider"] == "lmstudio"
    assert route["provider"] == "local"
    assert route["fallback"] is True
    assert route["reason"] == "provider_unavailable"
    assert len(remote.calls) == 1 and len(local.calls) == 1
    assert local.calls[0]["model_id"] == "leaf-ir"
    assert rows[-1]["provider"] == "local"
    assert "requested=lmstudio; actual=local" in rows[-1]["detail"]
    assert "provider_error=" in rows[-1]["detail"]
    assert "unavailable" in rows[-1]["error"]


def test_document_embed_required_remote_missing_alias_is_503_without_local_load(
    service, monkeypatch,
):
    svc, rows = service
    local = _Provider("local")
    remote = _Provider("lmstudio")
    monkeypatch.setattr(
        svc,
        "_provider_router",
        ProviderRouter(
            providers={"local": local, "lmstudio": remote},
            cfg=_cfg(on_error="fail", alias=None),
        ),
    )
    monkeypatch.setattr(
        svc,
        "_get_model",
        lambda _key: (_ for _ in ()).throw(
            AssertionError("required-remote failure must not load local weights")
        ),
    )

    with svc.app.test_client() as client:
        response = client.post("/embed", json={
            "texts": ["one"], "model": "leaf-ir", "prompt_name": "document",
        })

    assert response.status_code == 503
    body = response.get_json()
    assert body["code"] == "embedding_provider_misconfigured"
    assert body["route"]["reason"] == "missing_lmstudio_model"
    assert body["route"]["fallback"] is False
    assert not local.calls and not remote.calls
    assert rows[-1]["status"] == "error"
    assert rows[-1]["provider"] == "lmstudio"
    configured = svc._provider_router.describe("leaf-ir")
    assert configured["configuration_status"] == "error"
    assert "lmstudio_model is required" in configured["configuration_error"]


def test_document_embed_required_remote_honors_cooldown_without_local_load(
    service, monkeypatch,
):
    svc, _rows = service
    local = _Provider("local")
    remote = _Provider("lmstudio", available=False)
    router = ProviderRouter(
        providers={"local": local, "lmstudio": remote},
        cfg=_cfg(on_error="fail"),
    )
    monkeypatch.setattr(svc, "_provider_router", router)

    with svc.app.test_client() as client:
        first = client.post("/embed", json={
            "texts": ["one"], "model": "leaf-ir", "prompt_name": "document",
        })
        second = client.post("/embed", json={
            "texts": ["two"], "model": "leaf-ir", "prompt_name": "document",
        })

    assert first.status_code == 503 and second.status_code == 503
    assert first.get_json()["route"]["reason"] == "provider_unavailable"
    assert second.get_json()["code"] == "embedding_provider_cooldown"
    assert second.get_json()["route"]["reason"] == "provider_cooldown"
    assert len(remote.calls) == 1
    assert not local.calls
    assert router.describe("leaf-ir")["cooldown_remaining_s"] > 0


def test_query_model_keeps_existing_local_endpoint_path(service, monkeypatch):
    svc, rows = service
    seen: list[str] = []

    class _Model:
        def encode(self, texts, **_kwargs):
            return np.full((len(texts), 2), 2.0, dtype=np.float32)

    monkeypatch.setattr(
        svc,
        "_get_provider_router",
        lambda: (_ for _ in ()).throw(
            AssertionError("query models must not enter document provider routing")
        ),
    )

    def _get_model(key):
        seen.append(key)
        return _Model()

    monkeypatch.setattr(svc, "_get_model", _get_model)
    monkeypatch.setattr(
        svc,
        "_brokered_encode",
        lambda model, texts, **kwargs: model.encode(texts, **kwargs),
    )

    with svc.app.test_client() as client:
        response = client.post("/embed", json={
            "texts": ["query"],
            "model": "leaf-ir-query",
            "prompt_name": "query",
        })

    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "vectors": [[2.0, 2.0]],
        "dims": 2,
        "count": 1,
        "model": "leaf-ir-query",
    }
    assert seen == ["leaf-ir-query"]
    assert rows == []


def test_explicit_document_role_routes_custom_prompt_through_provider_policy(
    service, monkeypatch,
):
    """Model-specific prompt names must not hide the semantic document role."""
    svc, _rows = service
    local = _Provider("local")
    remote = _Provider("lmstudio")
    monkeypatch.setattr(
        svc,
        "_provider_router",
        ProviderRouter(
            providers={"local": local, "lmstudio": remote},
            cfg={"embedding": {"models": {"nomic": {
                "provider": "lmstudio",
                "lmstudio_model": "remote-nomic",
                "on_error": "fail",
            }}}},
        ),
    )
    monkeypatch.setattr(
        svc,
        "_get_model",
        lambda _key: (_ for _ in ()).throw(
            AssertionError("custom document route must not bypass provider policy")
        ),
    )

    with svc.app.test_client() as client:
        response = client.post("/embed", json={
            "texts": ["one"],
            "model": "nomic",
            "prompt_name": "search_document",
            "routing_role": "document",
        })

    assert response.status_code == 200
    assert response.get_json()["route"]["provider"] == "lmstudio"
    assert remote.calls[0]["model_id"] == "remote-nomic"
    assert not local.calls


def test_explicit_query_role_overrides_legacy_leaf_ir_document_heuristic(
    service, monkeypatch,
):
    """A semantic query remains local even when a legacy model-key heuristic matches."""
    svc, rows = service
    seen: list[str] = []

    class _Model:
        def encode(self, texts, **_kwargs):
            return np.full((len(texts), 2), 3.0, dtype=np.float32)

    monkeypatch.setattr(svc, "_get_model", lambda key: seen.append(key) or _Model())
    monkeypatch.setattr(
        svc,
        "_brokered_encode",
        lambda model, texts, **kwargs: model.encode(texts, **kwargs),
    )
    monkeypatch.setattr(
        svc,
        "_get_provider_router",
        lambda: (_ for _ in ()).throw(
            AssertionError("explicit query role must not enter document routing")
        ),
    )

    with svc.app.test_client() as client:
        response = client.post("/embed", json={
            "texts": ["query"],
            "model": "leaf-ir",
            "prompt_name": "search_query",
            "routing_role": "query",
        })

    assert response.status_code == 200
    assert seen == ["leaf-ir"]
    assert rows == []


def test_health_exposes_policy_breaker_and_last_route(service, monkeypatch):
    svc, _rows = service
    local = _Provider("local")
    remote = _Provider("lmstudio")
    router = ProviderRouter(
        providers={"local": local, "lmstudio": remote},
        cfg=_cfg(on_error="fallback"),
    )
    monkeypatch.setattr(svc, "_provider_router", router)
    monkeypatch.setattr(svc, "_registry", {
        "leaf-ir": svc.ModelEntry(
            key="leaf-ir",
            hf_name="MongoDB/mdbr-leaf-ir-asym",
            dims=768,
            eager=False,
        ),
    })

    router.encode(["one"], model_id="leaf-ir", prompt_name="document")
    with svc.app.test_client() as client:
        response = client.get("/health")

    assert response.status_code == 200
    [model] = response.get_json()["models"]
    routing = model["routing"]
    assert routing["requested_provider"] == "lmstudio"
    assert routing["provider_model"] == "remote-leaf-ir"
    assert routing["on_error"] == "fallback"
    assert routing["configuration_status"] == "ok"
    assert routing["configuration_error"] is None
    assert routing["authority_error"] is None
    assert routing["last_route"]["provider"] == "lmstudio"
    assert routing["last_route"]["reason"] == "provider_success"


def test_in_service_bulk_index_uses_same_router_and_bounded_batches(
    service, monkeypatch,
):
    from work_buddy.ir import dense

    svc, rows = service
    local = _Provider("local")
    remote = _Provider("lmstudio")
    router = ProviderRouter(
        providers={"local": local, "lmstudio": remote},
        cfg=_cfg(on_error="fail"),
    )
    monkeypatch.setattr(svc, "_provider_router", router)
    monkeypatch.setattr(dense, "_IN_SERVICE", True)
    texts = [f"document {i}" for i in range(65)]

    vectors = dense._encode_bulk_direct(texts, batch_size=32, kind="passage")

    assert vectors.shape == (65, 3)
    assert [len(call["texts"]) for call in remote.calls] == [32, 32, 1]
    assert all(call["model_id"] == "remote-leaf-ir" for call in remote.calls)
    assert not local.calls
    assert rows[-1]["provider"] == "lmstudio"
    assert "route=provider_success" in rows[-1]["detail"]


def test_sidecar_default_encoder_delegates_document_policy_to_service(
    monkeypatch,
):
    """A dashboard override must not race a second YAML router in the sidecar."""
    from work_buddy.embedding import client
    from work_buddy.index import encode as encode_module
    from work_buddy.ir import dense

    monkeypatch.setattr(dense, "_IN_SERVICE", False)
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"embedding": {"models": {"leaf-ir": {"provider": "st"}}}},
    )
    monkeypatch.setattr(
        encode_module.LmStudioProvider,
        "encode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sidecar must not route documents around /embed")
        ),
    )
    calls: list[dict] = []

    def _embed(
        texts,
        *,
        model=None,
        prompt_name=None,
        timeout_s=None,
        routing_role=None,
    ):
        calls.append({
            "texts": list(texts),
            "model": model,
            "prompt_name": prompt_name,
            "timeout_s": timeout_s,
            "routing_role": routing_role,
        })
        return [[7.0, 8.0]] * len(texts)

    monkeypatch.setattr(client, "embed", _embed)

    vectors = default_encoder().encode_documents(
        ["one", "two"], "passage", batch_size=32,
    )

    assert vectors.tolist() == [[7.0, 8.0], [7.0, 8.0]]
    assert calls == [{
        "texts": ["one", "two"],
        "model": "leaf-ir",
        "prompt_name": "document",
        "timeout_s": 120,
        "routing_role": "document",
    }]


def test_sidecar_default_encoder_preserves_explicit_direct_st_evaluation(
    monkeypatch,
):
    """Custom provider:st A/B models remain independent of the live service."""
    from work_buddy.index import encode as encode_module
    from work_buddy.ir import dense

    cfg = {"embedding": {"models": {"nomic": {
        "name": "nomic-ai/nomic-embed-text-v1.5",
        "provider": "st",
        "encoding": {
            "query_model": "nomic",
            "document_model": "nomic",
            "query_prompt": "search_query",
            "document_prompt": "search_document",
        },
    }}}}
    monkeypatch.setattr(dense, "_IN_SERVICE", False)
    monkeypatch.setattr("work_buddy.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        encode_module,
        "_REGISTRY_CACHE",
        encode_module._build_registry(cfg),
    )
    calls: list[dict] = []

    def _direct(self, texts, **kwargs):
        calls.append({"texts": list(texts), **kwargs})
        return np.ones((len(texts), 2), dtype=np.float32)

    monkeypatch.setattr(encode_module.SentenceTransformerProvider, "encode", _direct)
    monkeypatch.setattr(
        "work_buddy.embedding.client.embed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider:st evaluation must not call the live service")
        ),
    )

    vectors = default_encoder().encode_documents(
        ["one", "two"], "passage", model_key="nomic",
    )

    assert vectors.tolist() == [[1.0, 1.0], [1.0, 1.0]]
    assert calls == [{
        "texts": ["one", "two"],
        "model_id": "nomic",
        "prompt_name": "search_document",
        "priority": Priority.BACKGROUND,
    }]


def test_failed_direct_st_evaluation_never_falls_through_to_live_service(
    monkeypatch,
):
    """Missing deps/OOM degrade without mutating the service's model residency."""
    from work_buddy.index import encode as encode_module
    from work_buddy.ir import dense

    cfg = {"embedding": {"models": {"nomic": {
        "name": "nomic-ai/nomic-embed-text-v1.5",
        "provider": "st",
        "encoding": {
            "query_model": "nomic",
            "document_model": "nomic",
            "document_prompt": "search_document",
        },
    }}}}
    monkeypatch.setattr(dense, "_IN_SERVICE", False)
    monkeypatch.setattr("work_buddy.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        encode_module,
        "_REGISTRY_CACHE",
        encode_module._build_registry(cfg),
    )
    monkeypatch.setattr(
        encode_module.SentenceTransformerProvider,
        "encode",
        lambda *_args, **_kwargs: None,
    )
    service_calls: list[dict] = []
    monkeypatch.setattr(
        "work_buddy.embedding.client.embed",
        lambda *_args, **kwargs: service_calls.append(kwargs) or [[9.0, 9.0]],
    )

    vectors = default_encoder().encode_documents(
        ["one"], "passage", model_key="nomic",
    )

    assert vectors is None
    assert service_calls == []


def test_in_service_default_encoder_reuses_authoritative_router(
    service, monkeypatch,
):
    svc, _rows = service
    from work_buddy.ir import dense

    local = _Provider("local")
    remote = _Provider("lmstudio")
    router = ProviderRouter(
        providers={"local": local, "lmstudio": remote},
        cfg=_cfg(on_error="fail"),
    )
    monkeypatch.setattr(dense, "_IN_SERVICE", True)
    monkeypatch.setattr(svc, "_provider_router", router)

    vectors = default_encoder().encode_documents(["one"], "passage")

    assert vectors.tolist() == [[1.0, 1.0, 1.0]]
    assert len(remote.calls) == 1
    assert not local.calls


@pytest.mark.parametrize(
    ("mode", "provider", "on_error"),
    [
        ("local", "local", "fallback"),
        ("prefer-lmstudio", "lmstudio", "fallback"),
        ("require-lmstudio", "lmstudio", "fail"),
    ],
)
def test_restart_setting_authoritatively_overlays_document_route(
    service, monkeypatch, mode, provider, on_error,
):
    svc, _rows = service
    from work_buddy.settings import broker

    original = {
        "embedding": {
            "service_port": 5999,
            "models": {
                "leaf-ir": {
                    "name": "local-leaf",
                    "dims": 768,
                    "eager": False,
                    "provider": "lmstudio" if mode == "local" else "local",
                    "lmstudio_model": "remote-leaf",
                    "on_error": "fail" if mode == "local" else "fallback",
                }
            },
        }
    }
    monkeypatch.setattr(
        broker,
        "get_embedding_document_execution_mode",
        lambda **_kwargs: (
            mode,
            {"revision": "value:3"},
            {"reason": "restart-value-applied"},
        ),
    )

    effective = svc._activate_document_execution_setting(original)

    leaf = effective["embedding"]["models"]["leaf-ir"]
    assert leaf["provider"] == provider
    assert leaf["on_error"] == on_error
    assert leaf["lmstudio_model"] == "remote-leaf"
    assert effective["embedding"]["service_port"] == 5999
    assert original["embedding"]["models"]["leaf-ir"]["provider"] != provider


def test_restart_setting_activation_failure_fails_document_route_safe(
    service, monkeypatch,
):
    svc, _rows = service
    from work_buddy.settings import broker

    config = {
        "embedding": {
            "models": {
                "leaf-ir": {
                    "provider": "local",
                    "on_error": "fallback",
                    "eager": True,
                },
            }
        }
    }
    monkeypatch.setattr(
        broker,
        "get_embedding_document_execution_mode",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )

    effective = svc._activate_document_execution_setting(config)

    assert effective["embedding"]["models"]["leaf-ir"] == {
        "provider": "lmstudio",
        "on_error": "fail",
        "eager": False,
    }
    assert config == {
        "embedding": {
            "models": {
                "leaf-ir": {
                    "provider": "local",
                    "on_error": "fallback",
                    "eager": True,
                },
            }
        }
    }
    assert svc._document_policy_authority_error == "RuntimeError: settings unavailable"


@pytest.mark.parametrize("on_error", ["fallback", "fail"])
def test_remote_document_policy_suppresses_stale_eager_local_warmup(
    service, monkeypatch, on_error,
):
    """Remote-preferred and remote-required startup never prewarm leaf-ir locally."""
    svc, _rows = service
    monkeypatch.setattr(svc, "_registry", {})
    monkeypatch.setattr(svc, "_resolve_device", lambda _device="auto": "cpu")

    svc._init_registry({
        "embedding": {
            "device": "cpu",
            "models": {
                "leaf-ir": {
                    "name": "MongoDB/mdbr-leaf-ir-asym",
                    "dims": 768,
                    # Hostile historical config: routing policy must override this
                    # preload hint before main's warmup loop sees the entry.
                    "eager": True,
                    "provider": "lmstudio",
                    "lmstudio_model": "remote-leaf",
                    "on_error": on_error,
                },
            },
        },
    })

    assert svc._registry["leaf-ir"].eager is False


def test_local_document_policy_preserves_explicit_eager_warmup(service, monkeypatch):
    svc, _rows = service
    monkeypatch.setattr(svc, "_registry", {})
    monkeypatch.setattr(svc, "_resolve_device", lambda _device="auto": "cpu")

    svc._init_registry({
        "embedding": {
            "device": "cpu",
            "models": {
                "leaf-ir": {
                    "name": "MongoDB/mdbr-leaf-ir-asym",
                    "dims": 768,
                    "eager": True,
                    "provider": "local",
                },
            },
        },
    })

    assert svc._registry["leaf-ir"].eager is True


@pytest.mark.parametrize("on_error", ["fallback", "fail"])
def test_direct_model_lookup_cannot_bypass_remote_leaf_ir_policy(
    service, monkeypatch, on_error,
):
    """Prewarm/search helpers cannot load leaf-ir around the provider router."""
    svc, _rows = service
    entry = svc.ModelEntry(
        key="leaf-ir",
        hf_name="MongoDB/mdbr-leaf-ir-asym",
        dims=768,
        eager=False,
    )
    monkeypatch.setattr(svc, "_registry", {"leaf-ir": entry})
    monkeypatch.setattr(
        svc,
        "_provider_router",
        ProviderRouter(
            providers={
                "local": _Provider("local"),
                "lmstudio": _Provider("lmstudio"),
            },
            cfg=_cfg(on_error=on_error),
        ),
    )
    monkeypatch.setattr(
        svc,
        "_load_model",
        lambda _entry: (_ for _ in ()).throw(
            AssertionError("direct lookup must not instantiate remote-routed leaf-ir")
        ),
    )

    with pytest.raises(RuntimeError, match="document-provider routing"):
        svc._get_model("leaf-ir")
    assert entry.model is None
