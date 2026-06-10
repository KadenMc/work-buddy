"""Encoding for the consolidated index — two orthogonal axes.

- **Backend (who computes vectors):** ``EmbeddingProvider`` — ``LocalProvider``
  (in-process sentence-transformers, or the HTTP client when out-of-service),
  ``LmStudioProvider`` (remote LM-Link peer). Selected per-model by ``ProviderRouter``
  (``embedding.models.<key>.provider`` + on_error fallback). Cross-device offload is a
  *provider*, never a kind of encoder.
- **Locus (where the index code runs):** handled INSIDE ``LocalProvider`` via the
  ``_IN_SERVICE``-awareness pattern (in-service → ``service._get_model`` + the
  ``local:embedding`` broker slot; else → the embedding HTTP client, which makes the
  running service do the compute). So one ``Encoder`` suffices — no separate HttpEncoder.

The **X1 broker admission is reused, not reinvented**: ``LocalProvider`` wraps each
in-service encode in ``inference.local_slot.local_embed_slot(priority)`` — query encode
at INTERACTIVE, bulk doc encode at BACKGROUND (per batch), so a rebuild yields to a
live search on the shared GPU.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from work_buddy.index.model import PoolStrategy, ProjectionKind
from work_buddy.inference.broker import Priority
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Cold-load tolerance for the lazy leaf-ir doc encoder over HTTP (mirrors
# knowledge/index.py::_CONTENT_COLD_LOAD_TIMEOUT_S).
_COLD_LOAD_TIMEOUT_S = 120
_BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# Dense scoring (cosine + per-doc pooling + max-normalize)
# ---------------------------------------------------------------------------

def score_dense(
    query_vec: "Any",
    matrix: "Any",
    doc_ids: list[str],
    *,
    pool: str = PoolStrategy.MAX,
) -> dict[str, float]:
    """Cosine-score a query vector against a (possibly pooled) doc matrix.

    ``doc_ids`` is parallel to ``matrix`` rows and may repeat (pooled projection);
    scores are aggregated per doc by ``pool`` (max default; mean), then max-normalized
    to [0, 1]. Returns ``{doc_id: score}``. Mirrors ``ir/dense.score_dense``.
    """
    import numpy as np

    if matrix is None or len(doc_ids) == 0:
        return {}
    q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
    qn = float(np.linalg.norm(q))
    if qn == 0:
        return {}
    q = q / qn
    m = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    sims = (m / norms) @ q  # (R,)

    if pool == PoolStrategy.MEAN:
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}
        for did, s in zip(doc_ids, sims):
            sums[did] = sums.get(did, 0.0) + float(s)
            counts[did] = counts.get(did, 0) + 1
        per_doc = {d: sums[d] / counts[d] for d in sums}
    else:  # MAX (and NONE — one vector per doc → the value itself)
        per_doc = {}
        for did, s in zip(doc_ids, sims):
            sv = float(s)
            if did not in per_doc or sv > per_doc[did]:
                per_doc[did] = sv

    if not per_doc:
        return {}
    hi = max(per_doc.values())
    if hi <= 0:
        # No positive similarity — return empty so this signal contributes nothing.
        return {}
    return {d: v / hi for d, v in per_doc.items() if v > 0}


# ---------------------------------------------------------------------------
# Model resolution (kind + role → model id + prompt)
# ---------------------------------------------------------------------------

def resolve_model(
    kind: str, role: str, model_key: str | None = None
) -> tuple[str, str | None]:
    """Map (projection kind, role) → (model_id, prompt_name).

    role ∈ {"query", "document"}. LABEL → symmetric ``leaf-mt`` (no prompt). PASSAGE →
    asymmetric ``leaf-ir-query`` (query) / ``leaf-ir`` (document). ``model_key`` overrides
    (fork F-RECENCY/MODEL — per-partition model choice).
    """
    if model_key:
        if model_key in ("leaf-ir", "leaf-ir-query"):
            return ("leaf-ir-query", "query") if role == "query" else ("leaf-ir", "document")
        return (model_key, None)
    if kind == ProjectionKind.LABEL:
        return ("leaf-mt", None)
    return ("leaf-ir-query", "query") if role == "query" else ("leaf-ir", "document")


# ---------------------------------------------------------------------------
# Provider seam (backend axis)
# ---------------------------------------------------------------------------

@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str

    def encode(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
    ) -> "Any | None":
        """Encode → ``(N, D)`` float32 ndarray, or ``None`` if unavailable."""
        ...


class LocalProvider:
    """In-process sentence-transformers (in-service) or HTTP client (out-of-service).

    Reuses the shipped X1 primitive: in-service encodes are admitted through the
    ``local:embedding`` broker profile via ``local_embed_slot(priority)``.
    """

    name = "local"

    def encode(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
    ) -> "Any | None":
        import numpy as np
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        try:
            from work_buddy.ir import dense as _ir_dense
            in_service = bool(getattr(_ir_dense, "_IN_SERVICE", False))
        except Exception:
            in_service = False

        if in_service:
            try:
                from work_buddy.embedding.service import _get_model
                from work_buddy.inference.local_slot import local_embed_slot
                model = _get_model(model_id)
                kwargs: dict[str, Any] = {"show_progress_bar": False}
                if prompt_name:
                    kwargs["prompt_name"] = prompt_name
                with local_embed_slot(priority):
                    vecs = model.encode(list(texts), **kwargs)
                return np.asarray(vecs, dtype=np.float32)
            except Exception as exc:  # in-service encode failed → signal unavailable
                logger.warning("LocalProvider in-service encode failed: %s", exc)
                return None

        # Out-of-service: round-trip to the running service (it does the compute).
        from work_buddy.embedding.client import embed
        timeout = max(_COLD_LOAD_TIMEOUT_S, len(texts) * 2)
        vecs = embed(list(texts), model=model_id, prompt_name=prompt_name, timeout_s=timeout)
        if vecs is None:
            return None
        return np.asarray(vecs, dtype=np.float32)


class LmStudioProvider:
    """Remote LM-Link peer via LM Studio's /v1/embeddings.

    Thin wrapper over the existing ``embedding/providers/lmstudio.encode``. Wired for
    completeness; not exercised by tonight's A/B (which uses local). Query offload is
    discouraged (latency) — the router only routes document encode here when configured.
    """

    name = "lmstudio"

    def encode(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
    ) -> "Any | None":
        import numpy as np
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        try:
            from work_buddy.embedding.providers.lmstudio import encode as lm_encode
            arr = lm_encode(list(texts), model_id=model_id, priority=priority)
            return np.asarray(arr, dtype=np.float32)
        except Exception as exc:
            logger.warning("LmStudioProvider encode failed: %s", exc)
            return None


class ProviderRouter:
    """(model_id) → provider, with on-error fallback to local."""

    def __init__(
        self,
        providers: dict[str, EmbeddingProvider] | None = None,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        self._providers: dict[str, EmbeddingProvider] = providers or {
            "local": LocalProvider(),
            "lmstudio": LmStudioProvider(),
        }
        if "local" not in self._providers:
            self._providers["local"] = LocalProvider()
        self._cfg = cfg

    def _provider_name_for(self, model_id: str) -> str:
        try:
            cfg = self._cfg
            if cfg is None:
                from work_buddy.config import load_config
                cfg = load_config()
            models = (cfg or {}).get("embedding", {}).get("models", {}) or {}
            return (models.get(model_id, {}) or {}).get("provider", "local")
        except Exception:
            return "local"

    def encode(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
    ) -> "Any | None":
        name = self._provider_name_for(model_id)
        provider = self._providers.get(name, self._providers["local"])
        out = provider.encode(
            texts, model_id=model_id, prompt_name=prompt_name, priority=priority,
        )
        if out is not None:
            return out
        if provider.name != "local":  # on_error fallback to local
            logger.info("provider %r unavailable for %s; falling back to local", name, model_id)
            return self._providers["local"].encode(
                texts, model_id=model_id, prompt_name=prompt_name, priority=priority,
            )
        return None


# ---------------------------------------------------------------------------
# Encoder (locus-agnostic; provider handles in-service vs HTTP)
# ---------------------------------------------------------------------------

@runtime_checkable
class Encoder(Protocol):
    def encode_query(
        self, texts: list[str], kind: str, model_key: str | None = None
    ) -> "Any | None": ...

    def encode_documents(
        self, texts: list[str], kind: str, model_key: str | None = None
    ) -> "Any | None": ...


class BrokeredEncoder:
    """The default encoder: resolves models, routes through the provider seam, and
    admits via the broker (INTERACTIVE for queries, BACKGROUND for builds)."""

    def __init__(self, router: ProviderRouter | None = None) -> None:
        self._router = router or ProviderRouter()

    def encode_query(
        self, texts: list[str], kind: str, model_key: str | None = None
    ) -> "Any | None":
        model_id, prompt = resolve_model(kind, "query", model_key)
        return self._router.encode(
            list(texts), model_id=model_id, prompt_name=prompt,
            priority=Priority.INTERACTIVE,
        )

    def encode_documents(
        self, texts: list[str], kind: str, model_key: str | None = None,
        *, batch_size: int = _BATCH_SIZE,
    ) -> "Any | None":
        import numpy as np
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        model_id, prompt = resolve_model(kind, "document", model_key)
        out = []
        for i in range(0, len(texts), batch_size):
            batch = list(texts[i:i + batch_size])
            v = self._router.encode(
                batch, model_id=model_id, prompt_name=prompt,
                priority=Priority.BACKGROUND,
            )
            if v is None:
                return None  # service/provider unavailable → caller degrades
            out.append(np.asarray(v, dtype=np.float32))
        return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)


def default_encoder() -> BrokeredEncoder:
    return BrokeredEncoder()
