"""Tests for index/encode.py — dense scoring, model resolution, provider routing.

No real embedding service: a FakeProvider records calls + returns deterministic vectors.
"""

from __future__ import annotations

import numpy as np

from work_buddy.index.encode import (
    BrokeredEncoder,
    ProviderRouter,
    resolve_model,
    score_dense,
)
from work_buddy.index.model import PoolStrategy, ProjectionKind
from work_buddy.inference.broker import Priority


class FakeProvider:
    name = "fake"

    def __init__(self, dim=4, name="fake"):
        self.name = name
        self.dim = dim
        self.calls = []  # (texts, model_id, prompt_name, priority)

    def encode(self, texts, *, model_id, prompt_name=None, priority=Priority.BACKGROUND):
        self.calls.append((list(texts), model_id, prompt_name, priority))
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.ones((len(texts), self.dim), dtype=np.float32)


class _NoneProvider:
    name = "down"

    def encode(self, texts, *, model_id, prompt_name=None, priority=Priority.BACKGROUND):
        return None  # simulates an unavailable backend


# ---------------------------------------------------------------------------
# score_dense
# ---------------------------------------------------------------------------

class TestScoreDense:
    def test_cosine_ranking(self):
        q = np.array([1.0, 0.0], dtype=np.float32)
        matrix = np.array([[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]], dtype=np.float32)
        ids = ["aligned", "orthogonal", "diag"]
        scores = score_dense(q, matrix, ids, pool=PoolStrategy.MAX)
        assert scores["aligned"] == 1.0          # best (normalized)
        assert "orthogonal" not in scores         # cosine 0 → dropped (not positive)
        assert 0 < scores["diag"] < 1.0

    def test_max_pool_over_repeated_doc(self):
        q = np.array([1.0, 0.0], dtype=np.float32)
        # doc 'x' has two sub-vectors: one orthogonal, one aligned → max should win
        matrix = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
        ids = ["x", "x"]
        scores = score_dense(q, matrix, ids, pool=PoolStrategy.MAX)
        assert scores["x"] == 1.0

    def test_mean_pool(self):
        q = np.array([1.0, 0.0], dtype=np.float32)
        matrix = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)  # both aligned
        ids = ["x", "x"]
        scores = score_dense(q, matrix, ids, pool=PoolStrategy.MEAN)
        assert scores["x"] == 1.0

    def test_empty(self):
        assert score_dense(np.array([1.0]), None, []) == {}
        assert score_dense(np.zeros(3), np.ones((2, 3)), ["a", "b"]) == {}  # zero query


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------

class TestResolveModel:
    def test_label_is_symmetric(self):
        assert resolve_model(ProjectionKind.LABEL, "query") == ("leaf-mt", None)
        assert resolve_model(ProjectionKind.LABEL, "document") == ("leaf-mt", None)

    def test_passage_is_asymmetric(self):
        assert resolve_model(ProjectionKind.PASSAGE, "query") == ("leaf-ir-query", "query")
        assert resolve_model(ProjectionKind.PASSAGE, "document") == ("leaf-ir", "document")

    def test_model_key_override(self):
        assert resolve_model(ProjectionKind.PASSAGE, "query", model_key="custom") == ("custom", None)
        # leaf-ir family override keeps the asymmetric query/doc split
        assert resolve_model(ProjectionKind.LABEL, "query", model_key="leaf-ir") == ("leaf-ir-query", "query")


# ---------------------------------------------------------------------------
# BrokeredEncoder + ProviderRouter
# ---------------------------------------------------------------------------

class TestBrokeredEncoder:
    def _encoder(self, provider):
        router = ProviderRouter(providers={"local": provider}, cfg={})
        return BrokeredEncoder(router=router), provider

    def test_query_uses_interactive_priority(self):
        enc, prov = self._encoder(FakeProvider())
        out = enc.encode_query(["hello"], ProjectionKind.PASSAGE)
        assert out.shape == (1, 4)
        texts, model_id, prompt, priority = prov.calls[-1]
        assert priority == Priority.INTERACTIVE
        assert model_id == "leaf-ir-query"
        assert prompt == "query"

    def test_documents_use_background_and_batch(self):
        enc, prov = self._encoder(FakeProvider())
        texts = [f"doc {i}" for i in range(70)]  # > 2 batches at 32
        out = enc.encode_documents(texts, ProjectionKind.PASSAGE, batch_size=32)
        assert out.shape == (70, 4)
        assert len(prov.calls) == 3  # 32 + 32 + 6
        assert all(c[3] == Priority.BACKGROUND for c in prov.calls)
        assert prov.calls[0][1] == "leaf-ir"  # document side

    def test_label_documents_use_leaf_mt(self):
        enc, prov = self._encoder(FakeProvider())
        enc.encode_documents(["alias one", "alias two"], ProjectionKind.LABEL)
        assert prov.calls[-1][1] == "leaf-mt"
        assert prov.calls[-1][2] is None

    def test_documents_degrade_to_none_when_provider_down(self):
        enc, _ = self._encoder(_NoneProvider())
        assert enc.encode_documents(["x"], ProjectionKind.PASSAGE) is None

    def test_router_falls_back_to_local_on_none(self):
        local = FakeProvider(name="local")
        down = _NoneProvider()
        # config routes leaf-ir to the (down) lmstudio provider
        router = ProviderRouter(
            providers={"local": local, "lmstudio": down},
            cfg={"embedding": {"models": {"leaf-ir": {"provider": "lmstudio"}}}},
        )
        out = router.encode(["x"], model_id="leaf-ir", priority=Priority.BACKGROUND)
        assert out is not None and out.shape == (1, 4)  # fell back to local
        assert local.calls  # local was invoked

    def test_router_routes_provider_st(self):
        from work_buddy.index.encode import SentenceTransformerProvider
        st = FakeProvider(name="st")
        router = ProviderRouter(
            providers={"local": FakeProvider(name="local"), "st": st},
            cfg={"embedding": {"models": {"nomic": {"provider": "st"}}}},
        )
        out = router.encode(["x"], model_id="nomic", priority=Priority.BACKGROUND)
        assert out is not None and st.calls  # routed to st, not local
        # the default router instantiates an st provider
        assert isinstance(ProviderRouter()._providers["st"], SentenceTransformerProvider)


# ---------------------------------------------------------------------------
# Model registry (config-driven resolve_model — "test more embeddings")
# ---------------------------------------------------------------------------

class TestModelRegistry:
    def _cfg(self):
        return {"embedding": {"models": {
            "nomic": {"name": "nomic-ai/nomic-embed-text-v1.5", "encoding": {
                "query_model": "nomic", "document_model": "nomic",
                "query_prompt": "search_query", "document_prompt": "search_document"}},
            "leaf-mt": {"name": "MongoDB/mdbr-leaf-mt"},  # no encoding → not registered
            "bad": "not-a-dict",
        }}}

    def test_build_registry_only_keeps_encoding_blocks(self):
        from work_buddy.index.encode import _build_registry
        reg = _build_registry(self._cfg())
        assert set(reg) == {"nomic"}  # leaf-mt (no encoding) + bad (not dict) skipped
        assert reg["nomic"].query_prompt == "search_query"
        assert reg["nomic"].document_model == "nomic"

    def test_resolve_model_uses_registry(self, monkeypatch):
        import work_buddy.index.encode as enc
        monkeypatch.setattr(enc, "_REGISTRY_CACHE", enc._build_registry(self._cfg()))
        assert enc.resolve_model(ProjectionKind.PASSAGE, "query", "nomic") == ("nomic", "search_query")
        assert enc.resolve_model(ProjectionKind.PASSAGE, "document", "nomic") == ("nomic", "search_document")

    def test_resolve_model_defaults_unchanged_when_registry_empty(self, monkeypatch):
        import work_buddy.index.encode as enc
        monkeypatch.setattr(enc, "_REGISTRY_CACHE", {})
        # legacy behavior preserved
        assert enc.resolve_model(ProjectionKind.LABEL, "query") == ("leaf-mt", None)
        assert enc.resolve_model(ProjectionKind.PASSAGE, "document") == ("leaf-ir", "document")
        assert enc.resolve_model(ProjectionKind.PASSAGE, "query", "leaf-ir") == ("leaf-ir-query", "query")
        assert enc.resolve_model(ProjectionKind.PASSAGE, "query", "custom-sym") == ("custom-sym", None)


# ---------------------------------------------------------------------------
# SentenceTransformerProvider (direct in-process load — mocked, no download)
# ---------------------------------------------------------------------------

class _FakeST:
    construct_count = 0

    def __init__(self, name, device=None, trust_remote_code=False):
        _FakeST.construct_count += 1
        self.name = name
        self.trust = trust_remote_code
        self.prompts = {"search_query": "search_query: "}  # one registered prompt
        self.calls = []
        self.fail = False

    def encode(self, texts, prompt_name=None, show_progress_bar=False):
        if self.fail:
            raise RuntimeError("CUDA OOM")
        self.calls.append((list(texts), prompt_name))
        return np.ones((len(texts), 4), dtype=np.float32)


class TestSentenceTransformerProvider:
    def _provider(self, monkeypatch, *, cfg=None, fail=False):
        import contextlib
        import sentence_transformers
        from work_buddy.index.encode import SentenceTransformerProvider
        _FakeST.construct_count = 0
        created = []

        def _factory(name, device=None, trust_remote_code=False):
            m = _FakeST(name, device=device, trust_remote_code=trust_remote_code)
            m.fail = fail
            created.append(m)
            return m

        monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _factory)
        monkeypatch.setattr(
            "work_buddy.inference.local_slot.local_embed_slot",
            lambda *a, **k: contextlib.nullcontext(),
        )
        monkeypatch.setattr(
            "work_buddy.config.load_config",
            lambda *a, **k: cfg or {"embedding": {"models": {}}},
        )
        return SentenceTransformerProvider(), created

    def test_registered_prompt_uses_prompt_name(self, monkeypatch):
        p, created = self._provider(monkeypatch)
        out = p.encode(["hello"], model_id="m", prompt_name="search_query")
        assert out.shape == (1, 4)
        assert created[-1].calls[-1] == (["hello"], "search_query")  # prompt_name path

    def test_unregistered_prompt_is_prefixed(self, monkeypatch):
        p, created = self._provider(monkeypatch)
        p.encode(["hello"], model_id="m", prompt_name="query")  # not in model.prompts
        texts, prompt_name = created[-1].calls[-1]
        assert prompt_name is None and texts == ["query: hello"]  # literal prefix applied

    def test_no_prompt(self, monkeypatch):
        p, created = self._provider(monkeypatch)
        p.encode(["hello"], model_id="m")
        assert created[-1].calls[-1] == (["hello"], None)

    def test_model_cached_across_calls(self, monkeypatch):
        p, _ = self._provider(monkeypatch)
        p.encode(["a"], model_id="m")
        p.encode(["b"], model_id="m")
        assert _FakeST.construct_count == 1  # loaded once, reused

    def test_hf_name_and_trust_from_config(self, monkeypatch):
        cfg = {"embedding": {"models": {"nomic": {
            "name": "nomic-ai/nomic-embed-text-v1.5", "trust_remote_code": True}}}}
        p, created = self._provider(monkeypatch, cfg=cfg)
        p.encode(["a"], model_id="nomic")
        assert created[-1].name == "nomic-ai/nomic-embed-text-v1.5"
        assert created[-1].trust is True

    def test_returns_none_on_failure(self, monkeypatch):
        p, _ = self._provider(monkeypatch, fail=True)
        assert p.encode(["a"], model_id="m") is None  # OOM → degrade


# ---------------------------------------------------------------------------
# Provider bodies that previously had no direct unit coverage (test-gap closure)
# ---------------------------------------------------------------------------

class _FakeServiceModel:
    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs.get("prompt_name")))
        return np.ones((len(texts), 4), dtype=np.float32)


class TestLocalProvider:
    def test_in_service_path_uses_get_model_and_slot(self, monkeypatch):
        import contextlib
        import work_buddy.ir.dense as dense
        import work_buddy.embedding.service as svc
        from work_buddy.index.encode import LocalProvider

        fake_model = _FakeServiceModel()
        monkeypatch.setattr(dense, "_IN_SERVICE", True, raising=False)
        monkeypatch.setattr(svc, "_get_model", lambda key: fake_model, raising=False)
        monkeypatch.setattr(
            "work_buddy.inference.local_slot.local_embed_slot",
            lambda *a, **k: contextlib.nullcontext(),
        )
        out = LocalProvider().encode(["x"], model_id="leaf-ir", prompt_name="document")
        assert out.shape == (1, 4)
        assert fake_model.calls[-1] == (["x"], "document")  # prompt_name forwarded

    def test_in_service_failure_returns_none(self, monkeypatch):
        import work_buddy.ir.dense as dense
        import work_buddy.embedding.service as svc
        from work_buddy.index.encode import LocalProvider

        def _boom(key):
            raise RuntimeError("model load failed")

        monkeypatch.setattr(dense, "_IN_SERVICE", True, raising=False)
        monkeypatch.setattr(svc, "_get_model", _boom, raising=False)
        assert LocalProvider().encode(["x"], model_id="leaf-ir") is None

    def test_out_of_service_uses_http_client(self, monkeypatch):
        import work_buddy.ir.dense as dense
        import work_buddy.embedding.client as client
        from work_buddy.index.encode import LocalProvider

        monkeypatch.setattr(dense, "_IN_SERVICE", False, raising=False)
        seen = {}

        def _embed(texts, model=None, prompt_name=None, timeout_s=None):
            seen.update(model=model, prompt_name=prompt_name)
            return [[1.0, 2.0]] * len(texts)

        monkeypatch.setattr(client, "embed", _embed)
        out = LocalProvider().encode(["x", "y"], model_id="leaf-mt")
        assert out.shape == (2, 2) and seen["model"] == "leaf-mt"

    def test_out_of_service_none_when_client_unavailable(self, monkeypatch):
        import work_buddy.ir.dense as dense
        import work_buddy.embedding.client as client
        from work_buddy.index.encode import LocalProvider

        monkeypatch.setattr(dense, "_IN_SERVICE", False, raising=False)
        monkeypatch.setattr(client, "embed", lambda *a, **k: None)
        assert LocalProvider().encode(["x"], model_id="leaf-mt") is None


class TestLmStudioProvider:
    def test_delegates_to_lmstudio_encode(self, monkeypatch):
        import work_buddy.embedding.providers.lmstudio as lm
        from work_buddy.index.encode import LmStudioProvider

        monkeypatch.setattr(lm, "encode", lambda texts, **k: np.ones((len(texts), 3)))
        out = LmStudioProvider().encode(["a", "b"], model_id="arctic")
        assert out.shape == (2, 3)

    def test_returns_none_on_error(self, monkeypatch):
        import work_buddy.embedding.providers.lmstudio as lm
        from work_buddy.index.encode import LmStudioProvider

        def _boom(texts, **k):
            raise RuntimeError("peer down")

        monkeypatch.setattr(lm, "encode", _boom)
        assert LmStudioProvider().encode(["a"], model_id="arctic") is None
