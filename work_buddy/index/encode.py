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

from dataclasses import dataclass
from threading import Condition, Lock
from time import monotonic, time
from typing import Any, Protocol, runtime_checkable

from work_buddy.index.model import PoolStrategy, ProjectionKind
from work_buddy.inference.broker import Priority
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Cold-load tolerance for the lazy leaf-ir doc encoder over HTTP (mirrors
# knowledge/index.py::_CONTENT_COLD_LOAD_TIMEOUT_S).
_COLD_LOAD_TIMEOUT_S = 120
_BATCH_SIZE = 32
# A failed LM Studio request should not be retried for every document batch.  Keep
# the breaker router-local so a new build gets a fresh attempt, while a long-running
# router will probe again after a short cooldown in case the peer recovers.
_LMSTUDIO_RETRY_COOLDOWN_S = 300.0


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
# Model registry (config-driven model resolution — "test more embeddings")
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelSpec:
    """How a registered ``model_key`` encodes each side of a comparison.

    ``query_model``/``document_model`` are encoder ids (equal ⇒ symmetric). The prompts
    are passed to the provider, which treats a value as a registered prompt_name if the
    model declares it, else as a literal prefix to prepend (covers nomic ``search_query:``,
    e5 ``query:``, leaf prompt_names, …).
    """

    query_model: str
    document_model: str
    query_prompt: str | None = None
    document_prompt: str | None = None


_REGISTRY_CACHE: "dict[str, ModelSpec] | None" = None


def _build_registry(cfg: dict[str, Any] | None = None) -> "dict[str, ModelSpec]":
    """Build the model registry from ``embedding.models.<key>.encoding`` blocks. Defensive:
    any malformed entry is skipped, and a model with no ``encoding`` block is simply not
    registered (so it falls through to the legacy defaults — behavior unchanged)."""
    try:
        if cfg is None:
            from work_buddy.config import load_config
            cfg = load_config()
        models = (cfg or {}).get("embedding", {}).get("models", {}) or {}
    except Exception:
        return {}
    reg: dict[str, ModelSpec] = {}
    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        enc = entry.get("encoding")
        if not isinstance(enc, dict):
            continue
        reg[key] = ModelSpec(
            query_model=str(enc.get("query_model", key)),
            document_model=str(enc.get("document_model", key)),
            query_prompt=enc.get("query_prompt"),
            document_prompt=enc.get("document_prompt"),
        )
    return reg


def get_model_registry(*, reload: bool = False) -> "dict[str, ModelSpec]":
    """Process-cached model registry. ``reload=True`` rebuilds (e.g. tests / config change)."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None or reload:
        _REGISTRY_CACHE = _build_registry()
    return _REGISTRY_CACHE


# ---------------------------------------------------------------------------
# Model resolution (kind + role → model id + prompt)
# ---------------------------------------------------------------------------

def resolve_model(
    kind: str, role: str, model_key: str | None = None
) -> tuple[str, str | None]:
    """Map (projection kind, role) → (model_id, prompt_name). role ∈ {"query","document"}.

    Precedence: (1) a config-registered ``model_key`` → its per-role (model, prompt); (2) the
    legacy ``leaf-ir`` family special-case; (3) any other ``model_key`` → that model, no prompt
    (symmetric); (4) no ``model_key`` → LABEL → ``leaf-mt``, PASSAGE → asymmetric ``leaf-ir``
    pair. With no ``encoding`` config the registry is empty and (4)/(2) preserve today's behavior.
    """
    if model_key:
        spec = get_model_registry().get(model_key)
        if spec is not None:
            return (
                (spec.query_model, spec.query_prompt) if role == "query"
                else (spec.document_model, spec.document_prompt)
            )
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
        batch_size: int | None = None,
        routing_role: str | None = None,
    ) -> "Any | None":
        """Encode to an ``(N, D)`` array; return ``None`` or raise if unavailable."""
        ...


class LocalProvider:
    """In-process sentence-transformers (in-service) or HTTP client (out-of-service).

    Reuses the embedding service's brokered persistent-worker path in-service;
    this preserves priority admission without creating native OpenMP worker teams
    on transient request threads.
    """

    name = "local"

    def __init__(self, *, in_service: bool | None = None) -> None:
        # The embedding HTTP service explicitly sets this to True. Other callers
        # retain the historical auto-detection through ``ir.dense._IN_SERVICE``.
        self._in_service = in_service

    def encode(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
        batch_size: int | None = None,
        routing_role: str | None = None,
    ) -> "Any | None":
        import numpy as np
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        in_service = self._in_service
        if in_service is None:
            try:
                from work_buddy.ir import dense as _ir_dense
                in_service = bool(getattr(_ir_dense, "_IN_SERVICE", False))
            except Exception:
                in_service = False

        if in_service:
            try:
                from work_buddy.embedding.service import _brokered_encode, _get_model
                model = _get_model(model_id, provider_authorized=True)
                kwargs: dict[str, Any] = {"show_progress_bar": False}
                if batch_size is not None:
                    kwargs["batch_size"] = batch_size
                if prompt_name:
                    kwargs["prompt_name"] = prompt_name
                vecs = _brokered_encode(
                    model, list(texts), priority=priority, **kwargs,
                )
                return np.asarray(vecs, dtype=np.float32)
            except Exception as exc:  # in-service encode failed → signal unavailable
                logger.warning("LocalProvider in-service encode failed: %s", exc)
                return None

        # Out-of-service: round-trip to the running service (it does the compute).
        from work_buddy.embedding.client import embed
        timeout = max(_COLD_LOAD_TIMEOUT_S, len(texts) * 2)
        request_kwargs: dict[str, Any] = {
            "model": model_id,
            "prompt_name": prompt_name,
            "timeout_s": timeout,
        }
        if routing_role is not None:
            request_kwargs["routing_role"] = routing_role
        vecs = embed(list(texts), **request_kwargs)
        if vecs is None:
            return None
        return np.asarray(vecs, dtype=np.float32)


class _SidecarServiceProvider(LocalProvider):
    """HTTP service transport that never absorbs direct-evaluation failures.

    ``provider: st`` is explicitly an isolated, in-process A/B path. If that
    candidate cannot load (missing dependency, OOM, and so on), its normal
    router fallback must degrade to ``None`` rather than POSTing the same model
    to the live service and changing that process's memory/runtime state.
    """

    def __init__(self, *, direct_evaluation_models: set[str]) -> None:
        super().__init__(in_service=False)
        self._direct_evaluation_models = direct_evaluation_models

    def encode(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
        batch_size: int | None = None,
        routing_role: str | None = None,
    ) -> "Any | None":
        if model_id in self._direct_evaluation_models:
            logger.warning(
                "Direct sentence-transformers evaluation unavailable for %s; "
                "not falling through to the live embedding service",
                model_id,
            )
            return None
        return super().encode(
            texts,
            model_id=model_id,
            prompt_name=prompt_name,
            priority=priority,
            batch_size=batch_size,
            routing_role=routing_role,
        )


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
        batch_size: int | None = None,
        routing_role: str | None = None,
    ) -> "Any | None":
        import numpy as np
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        from work_buddy.embedding.providers.lmstudio import encode as lm_encode

        arr = lm_encode(
            list(texts), model_id=model_id, priority=priority,
            batch_size=batch_size or 64,
        )
        return np.asarray(arr, dtype=np.float32)


class SentenceTransformerProvider:
    """Loads a sentence-transformers model DIRECTLY in the current process.

    Decoupled from the embedding service: lets an offline A/B or CI compare embedding
    models with no running/restarted service. Routed only when a model declares
    ``provider: st``; production paths (local/lmstudio) are untouched. The HF repo is
    ``embedding.models.<id>.name`` (defaulting to the id itself), ``trust_remote_code``
    is read from the same block. Broker-admitted; returns ``None`` on any failure (OOM,
    missing model) so the router/build degrades gracefully.
    """

    name = "st"

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def _spec(self, model_id: str) -> tuple[str, bool]:
        try:
            from work_buddy.config import load_config
            entry = (
                (load_config().get("embedding", {}).get("models", {}) or {}).get(model_id, {})
                or {}
            )
            return str(entry.get("name", model_id)), bool(entry.get("trust_remote_code", False))
        except Exception:
            return model_id, False

    def _model(self, model_id: str) -> Any:
        if model_id not in self._cache:
            from sentence_transformers import SentenceTransformer
            hf_name, trust = self._spec(model_id)
            # device=None → sentence-transformers auto-selects CUDA if present, else CPU.
            self._cache[model_id] = SentenceTransformer(
                hf_name, device=None, trust_remote_code=trust,
            )
        return self._cache[model_id]

    def encode(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
        batch_size: int | None = None,
        routing_role: str | None = None,
    ) -> "Any | None":
        import numpy as np
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        try:
            from work_buddy.inference.local_slot import local_embed_slot
            model = self._model(model_id)
            registered = set(getattr(model, "prompts", {}) or {})
            with local_embed_slot(priority):
                if prompt_name and prompt_name in registered:
                    kwargs: dict[str, Any] = {
                        "prompt_name": prompt_name,
                        "show_progress_bar": False,
                    }
                    if batch_size is not None:
                        kwargs["batch_size"] = batch_size
                    vecs = model.encode(list(texts), **kwargs)
                elif prompt_name:
                    # literal prefix (e.g. "search_query:" / "query:"); add a space if bare.
                    pre = prompt_name if prompt_name.endswith((" ", ":")) else f"{prompt_name}: "
                    kwargs = {"show_progress_bar": False}
                    if batch_size is not None:
                        kwargs["batch_size"] = batch_size
                    vecs = model.encode([f"{pre}{t}" for t in texts], **kwargs)
                else:
                    kwargs = {"show_progress_bar": False}
                    if batch_size is not None:
                        kwargs["batch_size"] = batch_size
                    vecs = model.encode(list(texts), **kwargs)
            return np.asarray(vecs, dtype=np.float32)
        except Exception as exc:
            logger.warning("SentenceTransformerProvider encode failed for %s: %s", model_id, exc)
            return None


@dataclass(frozen=True)
class RouteInfo:
    """One resolved embedding route, safe to expose through health APIs."""

    model: str
    requested_provider: str
    provider: str | None
    provider_model: str | None
    on_error: str
    status: str
    fallback: bool
    reason: str
    at: float
    error: str | None = None
    provider_error: str | None = None
    provider_error_kind: str | None = None
    provider_error_hint: str | None = None

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, default_model: str,
    ) -> "RouteInfo":
        """Rehydrate route metadata returned across the local HTTP boundary."""
        at = payload.get("at")
        return cls(
            model=str(payload.get("model") or default_model),
            requested_provider=str(payload.get("requested_provider") or "unknown"),
            provider=(
                str(payload["provider"])
                if payload.get("provider") is not None
                else None
            ),
            provider_model=(
                str(payload["provider_model"])
                if payload.get("provider_model") is not None
                else None
            ),
            on_error=str(payload.get("on_error") or "fallback"),
            status=str(payload.get("status") or "unknown"),
            fallback=bool(payload.get("fallback", False)),
            reason=str(payload.get("reason") or "unknown"),
            at=float(at) if isinstance(at, (int, float)) else time(),
            error=(str(payload["error"]) if payload.get("error") else None),
            provider_error=(
                str(payload["provider_error"])
                if payload.get("provider_error")
                else None
            ),
            provider_error_kind=(
                str(payload["provider_error_kind"])
                if payload.get("provider_error_kind")
                else None
            ),
            provider_error_hint=(
                str(payload["provider_error_hint"])
                if payload.get("provider_error_hint")
                else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "requested_provider": self.requested_provider,
            "provider": self.provider,
            "provider_model": self.provider_model,
            "on_error": self.on_error,
            "status": self.status,
            "fallback": self.fallback,
            "reason": self.reason,
            "at": self.at,
            "error": self.error,
            "provider_error": self.provider_error,
            "provider_error_kind": self.provider_error_kind,
            "provider_error_hint": self.provider_error_hint,
        }


@dataclass(frozen=True)
class RoutedEmbedding:
    """Vectors plus the route that produced (or failed to produce) them."""

    vectors: Any | None
    route: RouteInfo


class ProviderRoutingError(RuntimeError):
    """A configured fail-closed provider could not serve an encode."""

    def __init__(self, message: str, *, code: str, route: RouteInfo) -> None:
        super().__init__(message)
        self.code = code
        self.route = route


class ProviderRouter:
    """Authoritatively route a registry model to its configured backend.

    LM Studio receives its configured external model alias, while the local
    fallback always receives the original registry key. ``on_error=fallback``
    permits that local fallback; ``on_error=fail`` never touches the local
    provider. A failed LM Studio route is cooled down briefly so bulk batches
    do not hammer an unavailable peer. The latest decision per model is retained
    for the embedding health/settings surface.
    """

    def __init__(
        self,
        providers: dict[str, EmbeddingProvider] | None = None,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        self._providers: dict[str, EmbeddingProvider] = providers or {
            "local": LocalProvider(),
            "lmstudio": LmStudioProvider(),
            "st": SentenceTransformerProvider(),
        }
        if "local" not in self._providers:
            self._providers["local"] = LocalProvider()
        self._cfg = cfg
        self._unavailable_until: dict[tuple[str, str], float] = {}
        self._provider_inflight: set[tuple[str, str]] = set()
        self._breaker_condition = Condition()
        self._warned_missing_lmstudio_aliases: set[str] = set()
        self._route_lock = Lock()
        self._last_routes: dict[str, RouteInfo] = {}

    def _model_config_for(self, model_id: str) -> dict[str, Any]:
        try:
            cfg = self._cfg
            if cfg is None:
                from work_buddy.config import load_config
                cfg = load_config()
            models = (cfg or {}).get("embedding", {}).get("models", {}) or {}
            entry = models.get(model_id, {}) or {}
            return entry if isinstance(entry, dict) else {}
        except Exception:
            return {}

    def _encode_local(
        self, texts: list[str], *, model_id: str, prompt_name: str | None,
        priority: Priority, batch_size: int | None, routing_role: str | None,
    ) -> "Any | None":
        kwargs: dict[str, Any] = {
            "model_id": model_id,
            "prompt_name": prompt_name,
            "priority": priority,
        }
        if batch_size is not None:
            kwargs["batch_size"] = batch_size
        local = self._providers["local"]
        if routing_role is not None and isinstance(local, LocalProvider):
            # The semantic role is transport metadata for /embed. Keep it out
            # of the provider protocol so existing injected providers remain
            # source-compatible.
            kwargs["routing_role"] = routing_role
        return local.encode(texts, **kwargs)

    @staticmethod
    def _on_error(model_cfg: dict[str, Any]) -> str:
        raw = model_cfg.get("on_error", "fallback")
        return (
            raw.strip().lower()
            if isinstance(raw, str) and raw.strip().lower() == "fail"
            else "fallback"
        )

    @staticmethod
    def _route(
        *, model_id: str, requested_provider: str, provider: str | None,
        provider_model: str | None, on_error: str, status: str,
        fallback: bool, reason: str, error: str | None = None,
        provider_error: str | None = None,
        provider_error_kind: str | None = None,
        provider_error_hint: str | None = None,
    ) -> RouteInfo:
        return RouteInfo(
            model=model_id,
            requested_provider=requested_provider,
            provider=provider,
            provider_model=provider_model,
            on_error=on_error,
            status=status,
            fallback=fallback,
            reason=reason,
            at=time(),
            error=error,
            provider_error=provider_error,
            provider_error_kind=provider_error_kind,
            provider_error_hint=provider_error_hint,
        )

    def _remember(self, route: RouteInfo) -> RouteInfo:
        with self._route_lock:
            self._last_routes[route.model] = route
        return route

    def _claim_provider_attempt(self, key: tuple[str, str]) -> bool:
        """Serialize one provider attempt and re-check cooldown after waiting."""
        with self._breaker_condition:
            while key in self._provider_inflight:
                self._breaker_condition.wait()
            if monotonic() < self._unavailable_until.get(key, 0.0):
                return False
            self._provider_inflight.add(key)
            return True

    def _finish_provider_attempt(
        self,
        key: tuple[str, str],
        *,
        available: bool,
    ) -> None:
        with self._breaker_condition:
            if available:
                self._unavailable_until.pop(key, None)
            else:
                self._unavailable_until[key] = (
                    monotonic() + _LMSTUDIO_RETRY_COOLDOWN_S
                )
            self._provider_inflight.discard(key)
            self._breaker_condition.notify_all()

    def _fallback_or_fail(
        self, texts: list[str], *, model_id: str, prompt_name: str | None,
        priority: Priority, batch_size: int | None, routing_role: str | None,
        requested_provider: str,
        provider_model: str | None, on_error: str, reason: str,
        message: str, code: str, attempted_provider: str | None = None,
        provider_error_kind: str | None = None,
        provider_error_hint: str | None = None,
    ) -> RoutedEmbedding:
        if on_error == "fail":
            route = self._remember(self._route(
                model_id=model_id,
                requested_provider=requested_provider,
                provider=attempted_provider,
                provider_model=provider_model,
                on_error=on_error,
                status="error",
                fallback=False,
                reason=reason,
                error=message,
                provider_error=message,
                provider_error_kind=provider_error_kind,
                provider_error_hint=provider_error_hint,
            ))
            raise ProviderRoutingError(message, code=code, route=route)

        local = self._encode_local(
            texts, model_id=model_id, prompt_name=prompt_name,
            priority=priority, batch_size=batch_size, routing_role=routing_role,
        )
        local_ok = local is not None
        route = self._remember(self._route(
            model_id=model_id,
            requested_provider=requested_provider,
            provider="local",
            provider_model=model_id,
            on_error=on_error,
            status="ok" if local_ok else "unavailable",
            fallback=True,
            reason=reason if local_ok else "local_fallback_unavailable",
            error=None if local_ok else message,
            provider_error=message,
            provider_error_kind=provider_error_kind,
            provider_error_hint=provider_error_hint,
        ))
        return RoutedEmbedding(local, route)

    def describe(self, model_id: str) -> dict[str, Any]:
        """Return configured policy, breaker state, and the last actual route."""
        model_cfg = self._model_config_for(model_id)
        requested = model_cfg.get("provider", "local")
        if not isinstance(requested, str):
            requested = "local"
        requested = requested.strip().lower()
        if requested == "sentence_transformer":
            requested = "local"
        alias = model_cfg.get("lmstudio_model") if requested == "lmstudio" else None
        alias = alias.strip() if isinstance(alias, str) and alias.strip() else None
        configuration_error = None
        if requested not in self._providers:
            configuration_error = f"Embedding provider {requested!r} is not configured"
        elif requested == "lmstudio" and alias is None:
            configuration_error = (
                f"embedding.models.{model_id}.lmstudio_model is required"
            )
        breaker_key = (requested, alias) if alias else None
        remaining = 0.0
        inflight = False
        if breaker_key is not None:
            with self._breaker_condition:
                remaining = max(
                    0.0,
                    self._unavailable_until.get(breaker_key, 0.0) - monotonic(),
                )
                inflight = breaker_key in self._provider_inflight
        with self._route_lock:
            last = self._last_routes.get(model_id)
        return {
            "requested_provider": requested,
            "provider_model": alias if requested == "lmstudio" else model_id,
            "on_error": self._on_error(model_cfg),
            "configuration_status": "error" if configuration_error else "ok",
            "configuration_error": configuration_error,
            "cooldown_remaining_s": round(remaining, 1),
            "provider_inflight": inflight,
            "last_route": last.as_dict() if last is not None else None,
        }

    def encode(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
        batch_size: int | None = None,
        routing_role: str | None = None,
    ) -> "Any | None":
        return self.encode_with_route(
            texts, model_id=model_id, prompt_name=prompt_name,
            priority=priority, batch_size=batch_size, routing_role=routing_role,
        ).vectors

    def encode_with_route(
        self, texts: list[str], *, model_id: str, prompt_name: str | None = None,
        priority: Priority = Priority.BACKGROUND,
        batch_size: int | None = None,
        routing_role: str | None = None,
    ) -> RoutedEmbedding:
        model_cfg = self._model_config_for(model_id)
        name = model_cfg.get("provider", "local")
        if not isinstance(name, str):
            name = "local"
        name = name.strip().lower()
        if name == "sentence_transformer":
            name = "local"
        on_error = self._on_error(model_cfg)
        provider = self._providers.get(name)

        if provider is None:
            return self._fallback_or_fail(
                texts, model_id=model_id, prompt_name=prompt_name,
                priority=priority, batch_size=batch_size, routing_role=routing_role,
                requested_provider=name, provider_model=None, on_error=on_error,
                reason="provider_not_configured",
                message=f"Embedding provider {name!r} is not configured for model {model_id!r}",
                code="embedding_provider_not_configured",
            )

        # The registry key identifies the local sentence-transformers model; LM
        # Studio exposes its own model id.  Never send the registry key to the
        # remote endpoint as an implicit fallback: a missing alias is a local-
        # fallback condition, and the local provider must still receive model_id.
        provider_model_id = model_id
        breaker_key: tuple[str, str] | None = None
        if name == "lmstudio":
            alias = model_cfg.get("lmstudio_model")
            if not isinstance(alias, str) or not alias.strip():
                if model_id not in self._warned_missing_lmstudio_aliases:
                    logger.warning(
                        "embedding.models.%s.provider is 'lmstudio' but "
                        "lmstudio_model is not set",
                        model_id,
                    )
                    self._warned_missing_lmstudio_aliases.add(model_id)
                return self._fallback_or_fail(
                    texts, model_id=model_id, prompt_name=prompt_name,
                    priority=priority, batch_size=batch_size,
                    routing_role=routing_role,
                    requested_provider=name, provider_model=None, on_error=on_error,
                    reason="missing_lmstudio_model",
                    message=(
                        f"embedding.models.{model_id}.provider is 'lmstudio' but "
                        "lmstudio_model is not set"
                    ),
                    code="embedding_provider_misconfigured",
                )
            provider_model_id = alias.strip()
            breaker_key = (name, provider_model_id)
            if not self._claim_provider_attempt(breaker_key):
                return self._fallback_or_fail(
                    texts, model_id=model_id, prompt_name=prompt_name,
                    priority=priority, batch_size=batch_size,
                    routing_role=routing_role,
                    requested_provider=name, provider_model=provider_model_id,
                    on_error=on_error, reason="provider_cooldown",
                    message=(
                        f"Embedding provider {name!r} for model {model_id!r} "
                        "is in retry cooldown after an earlier failure"
                    ),
                    code="embedding_provider_cooldown",
                )

        provider_kwargs: dict[str, Any] = {
            "model_id": provider_model_id,
            "prompt_name": prompt_name,
            "priority": priority,
        }
        if batch_size is not None:
            provider_kwargs["batch_size"] = batch_size
        if routing_role is not None and isinstance(provider, LocalProvider):
            provider_kwargs["routing_role"] = routing_role
        out = None
        provider_error: Exception | None = None
        try:
            try:
                out = provider.encode(texts, **provider_kwargs)
            except Exception as exc:
                provider_error = exc
                logger.warning("provider %r failed for %s: %s", name, model_id, exc)
        finally:
            if breaker_key is not None:
                self._finish_provider_attempt(
                    breaker_key,
                    available=out is not None,
                )
        if out is not None:
            route = self._remember(self._route(
                model_id=model_id,
                requested_provider=name,
                provider=name,
                provider_model=provider_model_id,
                on_error=on_error,
                status="ok",
                fallback=False,
                reason="configured_local" if name == "local" else "provider_success",
            ))
            return RoutedEmbedding(out, route)
        if name != "local":
            logger.info("provider %r unavailable for %s", name, model_id)
            provider_error_kind = None
            provider_error_hint = None
            provider_error_message = None
            if provider_error is not None:
                kind = getattr(provider_error, "kind", None)
                hint = getattr(provider_error, "hint", None)
                provider_error_kind = kind if isinstance(kind, str) else None
                provider_error_hint = hint if isinstance(hint, str) and hint else None
                formatter = getattr(provider_error, "format_caller_message", None)
                if callable(formatter):
                    provider_error_message = str(formatter())
                else:
                    provider_error_message = str(provider_error)
            failure_detail = (
                f": {provider_error_message}" if provider_error_message else ""
            )
            return self._fallback_or_fail(
                texts, model_id=model_id, prompt_name=prompt_name,
                priority=priority, batch_size=batch_size,
                routing_role=routing_role,
                requested_provider=name, provider_model=provider_model_id,
                on_error=on_error, reason="provider_unavailable",
                message=(
                    f"Embedding provider {name!r} is unavailable for model "
                    f"{model_id!r}{failure_detail}"
                ),
                code="embedding_provider_unavailable",
                attempted_provider=name,
                provider_error_kind=provider_error_kind,
                provider_error_hint=provider_error_hint,
            )
        route = self._remember(self._route(
            model_id=model_id,
            requested_provider=name,
            provider="local",
            provider_model=model_id,
            on_error=on_error,
            status="unavailable",
            fallback=False,
            reason="local_unavailable",
            error=f"Local embedding provider is unavailable for model {model_id!r}",
        ))
        return RoutedEmbedding(None, route)


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
            routing_role="query",
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
                routing_role="document",
            )
            if v is None:
                return None  # service/provider unavailable → caller degrades
            out.append(np.asarray(v, dtype=np.float32))
        return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)


def default_encoder() -> BrokeredEncoder:
    """Return the production encoder with one authoritative document route.

    Consolidated-index refreshes normally run in the sidecar process.  Letting that
    process construct its own config-backed provider router would split policy from
    the embedding component: a restart-gated dashboard override could say ``local``
    while the sidecar still read ``provider: lmstudio`` from YAML and called the
    remote endpoint directly.  It would also make those calls invisible to the
    embedding service's breaker and route diagnostics.

    Outside the embedding service, production models cross its ``/embed`` endpoint;
    that endpoint owns provider/model/on-error resolution. Explicit ``provider: st``
    evaluation models retain their documented direct, in-process path so an offline
    A/B neither depends on nor reconfigures the live service. Inside the service,
    reuse the same singleton router directly to avoid an HTTP self-call.
    """
    try:
        from work_buddy.ir import dense as _ir_dense

        in_service = bool(getattr(_ir_dense, "_IN_SERVICE", False))
    except Exception:
        in_service = False

    if in_service:
        from work_buddy.embedding.service import _get_provider_router

        return BrokeredEncoder(router=_get_provider_router())

    # Preserve only the explicit direct-evaluation backend. Every production
    # route, including leaf-ir even if stale YAML says ``st``, resolves to the
    # service transport so restart-gated runtime authority cannot be bypassed.
    direct_models: dict[str, Any] = {}
    try:
        from work_buddy.config import load_config

        models = (load_config().get("embedding", {}).get("models", {}) or {})
        direct_models = {
            str(key): value
            for key, value in models.items()
            if key != "leaf-ir"
            and isinstance(value, dict)
            and str(value.get("provider") or "").strip().lower() == "st"
        }
    except Exception:
        direct_models = {}

    service_transport = ProviderRouter(
        providers={
            "local": _SidecarServiceProvider(
                direct_evaluation_models=set(direct_models),
            ),
            "st": SentenceTransformerProvider(),
        },
        # Unlisted models deliberately resolve to ``local``; here "local"
        # means the shared service transport, not a local model load.
        cfg={"embedding": {"models": direct_models}},
    )
    return BrokeredEncoder(router=service_transport)
