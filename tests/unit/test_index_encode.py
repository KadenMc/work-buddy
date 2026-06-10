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
