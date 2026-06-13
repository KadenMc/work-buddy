"""Shared search & embedding service — loads models once, serves all agents.

Endpoints:
    POST /search     — hybrid BM25 + semantic search over candidates
    POST /embed      — embed one or more texts, return vectors
    POST /similarity — cosine similarity between a query and candidate texts
    GET  /health     — check if models are loaded and ready

Runs on localhost:5124 by default (configurable via config.yaml).
The MCP server subprocess calls this for search scoring so that numpy
and sentence-transformers never need to be imported in the MCP process.

Model registry:
    Models are defined in config.yaml under ``embedding.models``. Each entry
    specifies a HuggingFace model name, expected dimensions, and whether to
    load eagerly at startup or lazily on first use. All endpoints accept an
    optional ``model`` parameter to select which model to use (defaults to
    ``embedding.default_model``).
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from flask import Flask, Response, jsonify, request
from rank_bm25 import BM25Okapi

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

# Fallback config when config.yaml is unavailable or missing embedding.models
_DEFAULT_MODELS = {
    "leaf-mt": {
        "name": "MongoDB/mdbr-leaf-mt",
        "dims": 1024,
        "eager": True,
    },
    "leaf-ir-query": {
        "name": "MongoDB/mdbr-leaf-ir",
        "dims": 768,
        "eager": True,
    },
    "leaf-ir": {
        "name": "MongoDB/mdbr-leaf-ir-asym",
        "dims": 768,
        "eager": False,
    },
}
_DEFAULT_MODEL = "leaf-mt"


@dataclass
class ModelEntry:
    """Runtime state for a registered model.

    Each entry carries its own ``load_cond`` so a slow ``_load_model()``
    call on one model does NOT block ``_get_model()`` calls for a
    *different* model. The global ``_registry_lock`` is only used for
    dict-level operations (init, iteration during eviction) — never
    held across the actual SentenceTransformer instantiation, which
    can take 5+ seconds and would otherwise starve interactive
    queries that only need the small query-side model.

    Fields:
        _loading: True while some thread is actively running
            ``_load_model(self)``. Other threads entering
            ``_get_model`` wait on ``load_cond`` until this flips
            back to False.
        load_cond: Per-entry condition variable coordinating the
            double-checked load. Replaces the old global lock for
            model-loading concerns.
    """

    key: str  # short name, e.g. "leaf-mt"
    hf_name: str  # HuggingFace model ID
    dims: int
    eager: bool = True
    model: Any = field(default=None, repr=False)  # SentenceTransformer | None
    load_time_s: float | None = None
    status: str = "pending"  # "pending" | "loading" | "loaded" | "error"
    error: str | None = None
    last_used_at: float = 0.0  # monotonic timestamp of last _get_model hit
    # Guards against the "loaded model never released" leak. Models
    # like ``leaf-ir`` get lazy-loaded on first bulk encode (every 5
    # minutes via ir-index-rebuild) and, without eviction, stay in
    # RAM for the life of the service — hundreds of MB per model.
    # The idle-evictor thread below drops models whose
    # last_used_at is older than IDLE_EVICT_SECONDS.
    _loading: bool = field(default=False, repr=False)
    load_cond: threading.Condition = field(
        default_factory=threading.Condition, repr=False,
    )


_registry: dict[str, ModelEntry] = {}
_default_model_key: str = _DEFAULT_MODEL
_device: str | None = None  # resolved device string ("cpu", "cuda", etc.)
# Only protects the _registry dict itself (init, iteration for eviction).
# Loading is coordinated per-entry via ModelEntry.load_cond so a slow
# load of one model never blocks access to another.
_registry_lock = threading.RLock()

# Evict any non-eager model whose last use is older than this many seconds.
# Eager models (leaf-mt, leaf-ir-query by default) are never evicted — they
# serve hot paths where reload latency on every request would be worse than
# the persistent RSS. Tuning: shorter = tighter RAM, longer = fewer reloads.
IDLE_EVICT_SECONDS = 600  # 10 minutes
IDLE_EVICT_CHECK_SECONDS = 60  # how often the evictor wakes up


def _resolve_device(device_cfg: str = "auto") -> str:
    """Resolve device config to a concrete device string."""
    if device_cfg == "auto":
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    return device_cfg


def _init_registry(cfg: dict | None = None) -> None:
    """Build the model registry from config."""
    global _registry, _default_model_key, _device
    _registry.clear()

    embed_cfg = (cfg or {}).get("embedding", {})
    models_cfg = embed_cfg.get("models", _DEFAULT_MODELS)
    _default_model_key = embed_cfg.get("default_model", _DEFAULT_MODEL)
    _device = _resolve_device(embed_cfg.get("device", "auto"))
    print(f"Embedding device: {_device}", file=sys.stderr)

    for key, mcfg in models_cfg.items():
        _registry[key] = ModelEntry(
            key=key,
            hf_name=mcfg["name"],
            dims=mcfg.get("dims", 0),
            eager=mcfg.get("eager", False),
        )

    print(f"Model registry: {list(_registry.keys())} (default: {_default_model_key})",
          file=sys.stderr)


def _validate_lmstudio_providers(cfg: dict | None = None) -> None:
    """Log a loud WARN at startup when any model opts into LM Studio but
    LM Studio isn't reachable.

    Purely informational — dispatch at encode time handles the actual
    fallback based on each model's ``on_error`` config. This exists so
    the user gets a breadcrumb the moment the service boots, rather
    than having to wait for the next ir-index-rebuild to notice a
    silently-misconfigured provider.

    Does nothing when no model has ``provider: lmstudio`` configured —
    common case for users who never opted in. Never blocks startup.
    """
    embed_cfg = (cfg or {}).get("embedding", {})
    models_cfg = embed_cfg.get("models", {}) or {}
    opted_in = {
        key: mcfg for key, mcfg in models_cfg.items()
        if isinstance(mcfg, dict)
        and (mcfg.get("provider") or "").lower() == "lmstudio"
    }
    if not opted_in:
        return

    from work_buddy.embedding.providers.lmstudio import validate_reachable
    report = validate_reachable(cfg)
    if report["ok"]:
        # Also verify each opted-in model id appears in the
        # loaded-models list. Missing isn't fatal (LM Studio can
        # JIT-load cataloged models on first request) but it's worth
        # surfacing: if the id is wrong, every encode will fail.
        loaded_ids = set(report.get("model_ids") or [])
        for key, mcfg in opted_in.items():
            want = mcfg.get("lmstudio_model")
            if not want:
                print(
                    f"WARNING: embedding.models.{key}.provider is "
                    f"'lmstudio' but no lmstudio_model is set. Will "
                    f"fail every encode until configured.",
                    file=sys.stderr,
                )
                continue
            if want not in loaded_ids:
                print(
                    f"WARNING: embedding.models.{key}.lmstudio_model "
                    f"= {want!r} is NOT in LM Studio's loaded models "
                    f"({sorted(loaded_ids)}). LM Studio may JIT-load "
                    f"it on first request; if not, set on_error: "
                    f"fallback or correct the id.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"LM Studio provider for embedding.models.{key} "
                    f"verified — model {want!r} is loaded at "
                    f"{report['base_url']}.",
                    file=sys.stderr,
                )
    else:
        keys = ", ".join(sorted(opted_in))
        print(
            f"WARNING: models [{keys}] have provider: lmstudio but "
            f"LM Studio is not reachable — {report['detail']}. Bulk "
            f"document encoding will fall back to sentence_transformer "
            f"for any model with on_error: fallback, and fail hard for "
            f"any with on_error: fail.",
            file=sys.stderr,
        )


def _load_model(entry: ModelEntry) -> None:
    """Load a single model into memory."""
    from sentence_transformers import SentenceTransformer

    print(f"Loading model '{entry.key}': {entry.hf_name}...", file=sys.stderr)
    start = time.time()
    try:
        entry.model = SentenceTransformer(entry.hf_name, device=_device or "cpu")
        entry.load_time_s = time.time() - start
        entry.status = "loaded"
        actual_dims = entry.model.get_sentence_embedding_dimension()
        if entry.dims and actual_dims != entry.dims:
            print(f"  WARNING: expected {entry.dims}d, got {actual_dims}d",
                  file=sys.stderr)
            entry.dims = actual_dims
        print(f"  Loaded '{entry.key}' in {entry.load_time_s:.1f}s ({entry.dims}d)",
              file=sys.stderr)
    except Exception as exc:
        entry.load_time_s = time.time() - start
        entry.status = "error"
        entry.error = str(exc)
        print(f"  FAILED to load '{entry.key}': {exc}", file=sys.stderr)


def _get_model(key: str | None = None) -> Any:
    """Return a loaded model by key, loading lazily if needed.

    Concurrency contract:
      * Fast path (model already loaded): no lock acquired. Returns the
        model reference immediately.
      * Slow path (model needs loading): coordinates via a per-entry
        ``load_cond``. Only one thread actually runs ``_load_model``;
        other threads requesting the same model wait on the condition.
        Threads requesting a *different* model never block — each
        entry has its own condition.
      * The actual ``_load_model`` call runs OUTSIDE any held lock so
        concurrent access to other models isn't starved during the
        5-second SentenceTransformer instantiation.
    """
    key = key or _default_model_key
    entry = _registry.get(key)
    if entry is None:
        raise ValueError(
            f"Unknown model '{key}'. Available: {list(_registry.keys())}"
        )

    # Fast path: already loaded. No lock acquired.
    if entry.model is not None:
        entry.last_used_at = time.monotonic()
        return entry.model
    if entry.status == "error":
        raise RuntimeError(f"Model '{key}' failed to load: {entry.error}")

    # Slow path: coordinate via per-entry condition. This lock is only
    # ever contended among threads wanting THIS specific model.
    with entry.load_cond:
        # Re-check under lock — another thread may have loaded it or
        # marked it errored in the tiny window between the fast-path
        # check and this acquisition.
        if entry.model is not None:
            entry.last_used_at = time.monotonic()
            return entry.model
        if entry.status == "error":
            raise RuntimeError(f"Model '{key}' failed to load: {entry.error}")

        if entry._loading:
            # Another thread is already loading this entry. Wait for
            # completion and re-check. ``wait`` releases the cond
            # while blocked and re-acquires before returning.
            while entry._loading:
                entry.load_cond.wait()
            if entry.model is not None:
                entry.last_used_at = time.monotonic()
                return entry.model
            raise RuntimeError(
                f"Model '{key}' failed to load: {entry.error}"
            )

        # This thread claims the load.
        entry._loading = True
        entry.status = "loading"

    # Load OUTSIDE entry.load_cond — crucially, we hold no locks during
    # the slow SentenceTransformer instantiation. Other threads
    # requesting THIS model will enter the `wait` branch above and
    # park harmlessly on the condition. Threads requesting OTHER
    # models are unaffected.
    try:
        _load_model(entry)
    finally:
        with entry.load_cond:
            entry._loading = False
            entry.load_cond.notify_all()

    if entry.model is None:
        raise RuntimeError(f"Model '{key}' failed to load: {entry.error}")
    entry.last_used_at = time.monotonic()
    return entry.model


def _evict_model(entry: "ModelEntry") -> bool:
    """Drop a loaded model from memory. Returns True if anything was freed.

    Safe to call while other code may be about to read ``entry.model``
    because the caller holds ``_registry_lock``.
    """
    if entry.model is None:
        return False
    print(
        f"Evicting idle model '{entry.key}' "
        f"(idle for {time.monotonic() - entry.last_used_at:.0f}s)",
        file=sys.stderr,
    )
    entry.model = None
    entry.status = "pending"
    entry.load_time_s = None
    # Torch holds its own allocator caches and Python's GC is often
    # too lazy to return memory to the OS after a big object is
    # released. Explicit collect + empty_cache maximize the chance
    # that RSS actually drops.
    import gc
    gc.collect()
    try:
        import torch
        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    return True


def _idle_evictor_loop() -> None:
    """Background thread: periodically evict idle non-eager models.

    Eager models (typically ``leaf-mt``) are never evicted — they
    serve hot paths (wb_search on every call) and the reload
    latency on every request would hurt more than the RSS cost.
    Non-eager models (typically ``leaf-ir`` used by the 5-minute
    ir-index-rebuild cron) are released after ``IDLE_EVICT_SECONDS``
    of non-use.
    """
    while True:
        try:
            time.sleep(IDLE_EVICT_CHECK_SECONDS)
            now = time.monotonic()
            with _registry_lock:
                for entry in list(_registry.values()):
                    if entry.eager:
                        continue
                    if entry.model is None:
                        continue
                    if entry.last_used_at == 0.0:
                        # Loaded but never used? Mark it now and wait
                        # another cycle.
                        entry.last_used_at = now
                        continue
                    if now - entry.last_used_at >= IDLE_EVICT_SECONDS:
                        _evict_model(entry)
        except Exception as exc:
            print(f"idle_evictor error (non-fatal): {exc}", file=sys.stderr)


app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    """Check model registry status."""
    models_info = []
    any_loaded = False
    for key, entry in _registry.items():
        info: dict[str, Any] = {
            "key": key,
            "name": entry.hf_name,
            "dims": entry.dims,
            "status": entry.status,
            "eager": entry.eager,
        }
        if entry.load_time_s is not None:
            info["load_time_s"] = round(entry.load_time_s, 1)
        if entry.error:
            info["error"] = entry.error
        if entry.status == "loaded":
            any_loaded = True
        models_info.append(info)

    return jsonify({
        "status": "ok" if any_loaded else "loading",
        "default_model": _default_model_key,
        "models": models_info,
    })


# The broker exposes no HTTP state endpoint. Its completed-call metrics are
# flushed to the persistent store by the loop below; the dashboard reads that
# store for the inference-activity feed's latency join
# (dashboard/api.py::_build_inference_activity).


def _embed_priority(data: dict[str, Any], prompt_name: str | None) -> Any:
    """Pick a broker priority for an encode request.

    Explicit ``priority`` in the request body wins; otherwise derive from the
    asymmetric prompt role — a ``"query"`` encode serves a live search
    (INTERACTIVE), a ``"document"`` encode is part of a bulk index build
    (BACKGROUND). Symmetric encodes (no ``prompt_name``, e.g. leaf-mt aliases)
    default to WORKFLOW: they preempt background rebuilds but yield to explicit
    interactive queries.
    """
    from work_buddy.inference import Priority, parse_priority

    try:
        explicit = parse_priority(data.get("priority"))
    except ValueError:
        explicit = None
    if explicit is not None:
        return explicit
    if prompt_name == "query":
        return Priority.INTERACTIVE
    if prompt_name == "document":
        return Priority.BACKGROUND
    return Priority.WORKFLOW


def _brokered_encode(model: Any, texts: list[str], *, priority: Any, **encode_kwargs: Any):
    """Run ``model.encode`` under the shared local-device broker slot, so an
    INTERACTIVE query preempts a BACKGROUND rebuild on the one GPU. Degrades to
    a direct encode when the broker is unavailable (see ``local_embed_slot``).

    ``priority`` may be a ``Priority`` or a name string ("interactive" / …).
    """
    from work_buddy.inference import parse_priority
    from work_buddy.inference.local_slot import local_embed_slot

    prio = parse_priority(priority) if isinstance(priority, str) else priority
    with local_embed_slot(prio):
        return model.encode(texts, **encode_kwargs)


@app.route("/embed", methods=["POST"])
def embed():
    """Embed one or more texts.

    Request body: {
        "texts": ["text1", "text2", ...],
        "model": "leaf-mt",          // optional, defaults to default_model
        "prompt_name": "query"       // optional, for asymmetric models
    }
    Response: {"vectors": [[...], ...], "dims": 768, "count": 2, "model": "leaf-mt"}
    """
    data = request.get_json(silent=True) or {}
    texts = data.get("texts", [])
    if not texts:
        return jsonify({"error": "No texts provided"}), 400
    if isinstance(texts, str):
        texts = [texts]

    model_key = data.get("model") or _default_model_key
    prompt_name = data.get("prompt_name")

    try:
        model = _get_model(model_key)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400

    encode_kwargs: dict[str, Any] = {"batch_size": 32, "show_progress_bar": False}
    if prompt_name:
        encode_kwargs["prompt_name"] = prompt_name

    vectors = _brokered_encode(
        model, texts, priority=_embed_priority(data, prompt_name), **encode_kwargs,
    )

    return Response(
        json.dumps({
            "vectors": vectors.tolist(),
            "dims": int(vectors.shape[1]),
            "count": len(texts),
            "model": model_key,
        }),
        mimetype="application/json",
    )


@app.route("/similarity", methods=["POST"])
def similarity():
    """Score a query against candidate texts by cosine similarity.

    Request body: {
        "query": "search query",
        "candidates": [
            {"name": "id1", "texts": ["phrase1", "phrase2"]},
            {"name": "id2", "texts": ["phrase3"]},
        ],
        "model": "leaf-mt"          // optional
    }
    Response: {"results": [{"name": "id1", "score": 0.85}, ...]}

    Each candidate has multiple texts (search phrases). The score is
    the max cosine similarity across all phrases for that candidate.
    """
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    candidates = data.get("candidates", [])

    if not query or not candidates:
        return jsonify({"error": "Missing query or candidates"}), 400

    model_key = data.get("model") or _default_model_key
    try:
        model = _get_model(model_key)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400

    # Collect all texts for batch encoding
    all_texts = [query]
    text_to_candidate: list[tuple[str, int]] = []  # (candidate_name, index_in_all_texts)

    for cand in candidates:
        name = cand.get("name", "")
        for phrase in cand.get("texts", []):
            text_to_candidate.append((name, len(all_texts)))
            all_texts.append(phrase)

    # Serving a live similarity query → INTERACTIVE (preempts background rebuilds).
    vectors = _brokered_encode(
        model, all_texts, priority="interactive", batch_size=32, show_progress_bar=False,
    )
    query_vec = vectors[0]
    query_norm = np.linalg.norm(query_vec)

    if query_norm == 0:
        return jsonify({"results": []})

    # Score each candidate (max across its phrases)
    scores: dict[str, float] = {}
    for name, idx in text_to_candidate:
        vec = vectors[idx]
        norm = np.linalg.norm(vec)
        if norm == 0:
            continue
        sim = float(np.dot(query_vec, vec) / (query_norm * norm))
        scores[name] = max(scores.get(name, 0.0), sim)

    results = sorted(
        [{"name": name, "score": round(score, 4)} for name, score in scores.items()],
        key=lambda x: x["score"],
        reverse=True,
    )

    return jsonify({"results": results})


def _tokenize(text: str) -> list[str]:
    """Simple tokenizer for BM25: lowercase, split on non-alphanumeric."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1]


# ---------------------------------------------------------------------------
# Candidate embedding cache for /search
# ---------------------------------------------------------------------------
# The MCP registry is static within a session — ~61 entries with ~200 texts.
# Encoding them on every wb_search call wastes ~5s on CPU. Instead, we cache
# the encoded vectors keyed by a fingerprint of the candidate texts. Only the
# query (1 text) needs encoding per search call.

import hashlib

_candidate_cache: dict[str, tuple[str, np.ndarray, list[tuple[str, int]]]] = {}
# key: model_key -> (fingerprint, vectors_matrix, text_to_name_mapping)


def _candidate_fingerprint(candidates: list[dict]) -> str:
    """Fast fingerprint of candidate texts for cache keying."""
    h = hashlib.md5()
    for cand in candidates:
        h.update(cand.get("name", "").encode())
        for t in cand.get("texts", []):
            h.update(t.encode())
    return h.hexdigest()


def _prewarm_search_cache() -> None:
    """Pre-encode MCP registry candidates at startup.

    Builds the same candidate list that ``wb_search`` sends to ``/search``,
    encodes all texts with the default model, and populates the cache.
    Subsequent ``/search`` calls with the same candidates skip encoding
    entirely — only the 1-text query needs encoding per call.
    """
    try:
        from work_buddy.mcp_server.registry import get_registry, Capability

        registry = get_registry()
        candidates = []
        for name, entry in registry.items():
            phrases = [name.replace("-", " ").replace("_", " "), entry.description]
            if isinstance(entry, Capability) and entry.search_aliases:
                phrases.extend(entry.search_aliases)
            phrases.append(f"{name} {entry.description}")
            candidates.append({"name": name, "texts": phrases})

        if not candidates:
            return

        model = _get_model(_default_model_key)
        cand_texts = []
        text_to_name: list[tuple[str, int]] = []
        for cand in candidates:
            for phrase in cand.get("texts", []):
                text_to_name.append((cand["name"], len(cand_texts)))
                cand_texts.append(phrase)

        t = time.time()
        cand_vectors = model.encode(cand_texts, batch_size=32, show_progress_bar=False)
        fp = _candidate_fingerprint(candidates)
        _candidate_cache[_default_model_key] = (fp, cand_vectors, text_to_name)

        print(
            f"Search cache pre-warmed: {len(candidates)} candidates, "
            f"{len(cand_texts)} texts encoded in {time.time() - t:.1f}s",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"Search cache pre-warm failed (non-fatal): {exc}", file=sys.stderr)


@app.route("/search", methods=["POST"])
def search():
    """Hybrid BM25 + semantic search over candidates.

    Request body: {
        "query": "search text",
        "candidates": [
            {"name": "id1", "texts": ["phrase1", "phrase2"]},
            ...
        ],
        "bm25_weight": 0.4,    // optional, default 0.4
        "embed_weight": 0.6    // optional, default 0.6
    }
    Response: {"results": [{"name": "id1", "score": 0.95, "bm25_score": 1.2, "embed_score": 0.85}, ...]}

    Candidate embeddings are cached — only the query is encoded on each call.
    """
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    candidates = data.get("candidates", [])
    bm25_weight = data.get("bm25_weight", 0.4)
    embed_weight = data.get("embed_weight", 0.6)
    model_key = data.get("model") or _default_model_key

    if not query or not candidates:
        return jsonify({"error": "Missing query or candidates"}), 400

    # ── BM25 scoring ──
    # Build corpus from all candidate phrases
    corpus = []
    doc_to_name = []
    for cand in candidates:
        name = cand.get("name", "")
        for phrase in cand.get("texts", []):
            tokens = _tokenize(phrase)
            if tokens:
                corpus.append(tokens)
                doc_to_name.append(name)

    bm25_scores: dict[str, float] = {}
    if corpus:
        bm25 = BM25Okapi(corpus)
        query_tokens = _tokenize(query)
        if query_tokens:
            raw_scores = bm25.get_scores(query_tokens)
            for i, score in enumerate(raw_scores):
                name = doc_to_name[i]
                bm25_scores[name] = max(bm25_scores.get(name, 0.0), float(score))

    # ── Embedding scoring (with candidate cache) ──
    try:
        model = _get_model(model_key)
    except (ValueError, RuntimeError):
        model = None

    embed_scores: dict[str, float] = {}
    if model is not None:
        fp = _candidate_fingerprint(candidates)
        cached = _candidate_cache.get(model_key)

        if cached and cached[0] == fp:
            # Cache hit — only encode the query
            cand_vectors = cached[1]
            text_to_name = cached[2]
            query_vec = _brokered_encode(
                model, [query], priority="interactive", batch_size=1, show_progress_bar=False,
            )[0]
        else:
            # Cache miss — encode all candidates, cache them, then encode query
            cand_texts = []
            text_to_name = []
            for cand in candidates:
                name = cand.get("name", "")
                for phrase in cand.get("texts", []):
                    text_to_name.append((name, len(cand_texts)))
                    cand_texts.append(phrase)

            cand_vectors = _brokered_encode(
                model, cand_texts, priority="interactive", batch_size=32, show_progress_bar=False,
            )
            _candidate_cache[model_key] = (fp, cand_vectors, text_to_name)
            query_vec = _brokered_encode(
                model, [query], priority="interactive", batch_size=1, show_progress_bar=False,
            )[0]

        query_norm = np.linalg.norm(query_vec)
        if query_norm > 0:
            for name, idx in text_to_name:
                vec = cand_vectors[idx]
                norm = np.linalg.norm(vec)
                if norm > 0:
                    sim = float(np.dot(query_vec, vec) / (query_norm * norm))
                    embed_scores[name] = max(embed_scores.get(name, 0.0), sim)

    # ── Fuse scores ──
    all_names = set(bm25_scores.keys()) | set(embed_scores.keys())
    bm25_max = max(bm25_scores.values()) if bm25_scores else 1.0
    embed_max = max(embed_scores.values()) if embed_scores else 1.0

    if not embed_scores:
        bm25_weight = 1.0
        embed_weight = 0.0

    fused = []
    for name in all_names:
        bm25_norm = bm25_scores.get(name, 0.0) / bm25_max if bm25_max else 0
        embed_norm = embed_scores.get(name, 0.0) / embed_max if embed_max else 0
        combined = bm25_weight * bm25_norm + embed_weight * embed_norm

        if combined > 0:
            fused.append({
                "name": name,
                "score": round(combined, 4),
                "bm25_score": round(bm25_scores.get(name, 0.0), 4),
                "embed_score": round(embed_scores.get(name, 0.0), 4),
            })

    fused.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({"results": fused})


@app.route("/ir/search", methods=["POST"])
def ir_search_endpoint():
    """Search indexed documents via the IR engine (BM25 + optional dense).

    Request body: {
        "query": "search text",
        "source": "conversation",       // optional
        "scope": "session_id_prefix",   // optional
        "top_k": 10,                    // optional, default 10
        "bm25_only": false,             // optional, default false
        "dense_only": false             // optional, default false
    }
    Response: {"results": [{"doc_id": ..., "score": ..., ...}, ...]}
    """
    data = request.get_json(silent=True) or {}
    query = data.get("query", "")
    if not query:
        return jsonify({"error": "Missing query"}), 400

    from work_buddy.ir.engine import search as ir_search

    try:
        results = ir_search(
            query,
            source=data.get("source"),
            scope=data.get("scope"),
            metadata_filter=data.get("metadata_filter"),
            top_k=data.get("top_k", 10),
            bm25_only=data.get("bm25_only", False),
            dense_only=data.get("dense_only", False),
        )
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/ir/index", methods=["POST"])
def ir_index_endpoint():
    """Build or check the IR search index.

    Request body: {
        "action": "build" | "status",   // default "build"
        "source": "conversation",       // default "conversation"
        "days": 30,                     // default 30
        "force": false                  // default false
    }
    Response: {"result": {...}}
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action", "build")
    source = data.get("source", "conversation")

    from work_buddy.ir.store import build_index, index_status

    try:
        if action == "status":
            result = index_status(source=source)
        else:
            result = build_index(
                source=source,
                days=data.get("days", 30),
                force=data.get("force", False),
            )
            # Best-effort dense vector build so hybrid retrieval actually
            # has vectors to score against. Runs in-service (no HTTP
            # self-call) because _IN_SERVICE is set in main(). Failures
            # here degrade the index to BM25-only rather than failing the
            # whole build — callers can check result["dense"] for status.
            if data.get("include_dense", True):
                try:
                    from work_buddy.ir.dense import build_vectors

                    result["dense"] = build_vectors(
                        source=source,
                        force=data.get("force", False),
                    )
                except Exception as dense_exc:
                    result["dense"] = {
                        "status": "error",
                        "error": f"{type(dense_exc).__name__}: {dense_exc}",
                    }
        return jsonify({"result": result})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/vault/search", methods=["POST"])
def vault_search_endpoint():
    """Hybrid search over the vault semantic index, in-process.

    Running here (not in the caller's process) keeps the dense vector matrix
    resident across queries via ``vault_index.dense_cache`` — the query encoder is
    ``_IN_SERVICE``-aware, so no HTTP self-call.

    Request body: {"query", "top_k"?, "method"?, "vault_id"?, "recency"?}
    Response: {"results": [...]}
    """
    data = request.get_json(silent=True) or {}
    try:
        from work_buddy.vault_index.search import search as vault_search

        results = vault_search(
            data.get("query", ""),
            top_k=data.get("top_k", 10),
            method=data.get("method", "hybrid"),
            vault_id=data.get("vault_id"),
            recency=data.get("recency", False),
        )
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/vault/index", methods=["POST"])
def vault_index_endpoint():
    """Build or check the vault semantic index.

    ``build`` runs ``build_all`` in-process with ``_IN_SERVICE=True``, so the encode
    uses the in-service model and the **one** ``LocalInferenceBroker`` — yielding to
    interactive searches at BACKGROUND priority (a standalone CLI build has its own
    broker, so cross-process priority would only be LM Studio's FIFO).

    Request body: {"action": "build"|"status", "force"?}
    Response: {"result": {...}}
    """
    data = request.get_json(silent=True) or {}
    action = data.get("action", "build")
    try:
        if action == "status":
            from work_buddy.vault_index.status import index_status

            result = index_status()
        else:
            from work_buddy.vault_index.indexer import build_all

            result = build_all(force=data.get("force", False), encode=True)
        return jsonify({"result": result})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/index/search", methods=["POST"])
def index_search_endpoint():
    """Hybrid search over the consolidated index, in-process (resident matrices).

    Flag-gated infrastructure: inert unless something queries it; the live
    knowledge/vault/IR paths are unaffected. Runs here so the query encoder is
    ``_IN_SERVICE``-aware and the per-(partition,projection) resident matrices stay warm.

    Request body: {"query", "top_k"?, "method"?, "partitions"?, "filters"?, "scope"?, "recency"?, "rrf_k"?}
    Response: {"results": [{"doc_id","score","signals","display_text","metadata"}, ...]}
    """
    data = request.get_json(silent=True) or {}
    try:
        from work_buddy.index.model import Query
        from work_buddy.index.partitioned import UnifiedIndex

        q = Query(
            text=data.get("query", ""),
            top_k=data.get("top_k", 10),
            method=data.get("method", "hybrid"),
            filters=data.get("filters") or {},
            scope=data.get("scope"),
            recency=data.get("recency", False),
            rrf_k=data.get("rrf_k"),
        )
        hits = UnifiedIndex().search(q, partitions=data.get("partitions"))
        results = [
            {"doc_id": h.doc_id, "score": h.score, "signals": h.signals,
             "display_text": h.display_text, "metadata": h.metadata}
            for h in hits
        ]
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/index/search_many", methods=["POST"])
def index_search_many_endpoint():
    """Batched hybrid search over the consolidated index (resident matrices).

    Mirrors ``/index/search`` but takes a LIST of queries and runs ONE query-encode
    round-trip per projection for all of them — the budget-preserving path the
    dev-document scan uses (collapses N round-trips to 1, per the #178 rationale).

    Request body: {"queries":[...], "top_k"?, "method"?, "partitions"?, "filters"?, "scope"?, "recency"?, "rrf_k"?}
    Response: {"results": [[{doc_id,score,signals,display_text,metadata}, ...], ...]}  (one list per query, order preserved)
    """
    data = request.get_json(silent=True) or {}
    try:
        from work_buddy.index.partitioned import UnifiedIndex

        per_query = UnifiedIndex().search_many(
            data.get("queries") or [],
            partitions=data.get("partitions"),
            top_k=data.get("top_k", 10),
            method=data.get("method", "hybrid"),
            filters=data.get("filters") or {},
            scope=data.get("scope"),
            recency=data.get("recency", False),
            rrf_k=data.get("rrf_k"),
        )
        results = [
            [{"doc_id": h.doc_id, "score": h.score, "signals": h.signals,
              "display_text": h.display_text, "metadata": h.metadata}
             for h in hits]
            for hits in per_query
        ]
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/index/build", methods=["POST"])
def index_build_endpoint():
    """Build the consolidated index (a partition, or all) into its SEPARATE DB.

    Explicit build endpoint — builds into ``db/index-consolidated`` regardless of the
    ``index.enabled`` flag (a separate DB; building it does not change live behavior —
    only flipping the flag + re-pointing callers would). The cron-driven seam adapter,
    by contrast, respects the flag.

    Request body: {"partition"?: str, "force"?: bool}  (omit partition → build_all)
    Response: {"result": {...}}
    """
    data = request.get_json(silent=True) or {}
    try:
        from work_buddy.index.config import load_index_config
        from work_buddy.index.partitioned import UnifiedIndex

        ui = UnifiedIndex(config=load_index_config())
        name = data.get("partition")
        force = bool(data.get("force", False))
        result = ui.build(name, force=force) if name else ui.build_all(force=force)
        return jsonify({"result": result})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


def _vault_matrix_evictor_loop() -> None:
    """Background thread: release the resident vault search matrix after an idle TTL.

    The matrix (~hundreds of MB at vault scale) is lazy-loaded on the first search
    and would otherwise stay pinned in this long-lived process. Mirrors
    ``_idle_evictor_loop`` (which evicts idle models).
    """
    from work_buddy.vault_index import dense_cache

    while True:
        try:
            time.sleep(60)
            if dense_cache.release_if_idle(ttl_s=600):
                print("Released idle vault search matrix", file=sys.stderr)
        except Exception as exc:
            print(f"vault matrix evictor error (non-fatal): {exc}", file=sys.stderr)


_BROKER_PERSIST_INTERVAL_S = 15  # how often completed broker calls flush to disk


def _broker_metrics_persist_loop() -> None:
    """Background thread: flush completed broker calls to the persistent store.

    The broker's metrics ring is in-memory and wiped on every process restart.
    This drains it periodically into ``inference.metrics_store`` — out-of-band,
    so the broker itself stays a pure in-memory object — giving the dashboard's
    Inference panel history that survives restarts. ``INSERT OR IGNORE`` on the
    immutable call id makes re-flushing the same ring rows a cheap no-op.
    """
    from work_buddy.inference import get_broker, metrics_store

    while True:
        try:
            time.sleep(_BROKER_PERSIST_INTERVAL_S)
            metrics_store.persist_terminal_rows(
                get_broker().snapshot_metrics(),
                time.monotonic(),
                time.time(),
            )
        except Exception as exc:
            print(f"broker metrics persist error (non-fatal): {exc}", file=sys.stderr)


def main():
    """Entry point — init registry, start serving, load models in background.

    Flask comes up first so ``/health`` is reachable during the (slow)
    model-load phase; it returns ``{"status": "loading"}`` until at
    least one model finishes loading. The sidecar's health-checker
    accepts ``loading`` as healthy, so the service is not falsely
    declared dead while models warm up.
    """
    import threading

    from work_buddy.config import load_config

    cfg = load_config()
    port = cfg.get("embedding", {}).get("service_port", 5124)

    # Build model registry from config (cheap — just metadata)
    _init_registry(cfg)

    # Recover the IR vector store before serving: quarantine any crash-corrupted
    # .npz and clear orphaned write temps, so the first read after boot is clean
    # instead of raising on a 0-byte file and taking a search source dark.
    # Non-fatal — a sweep failure must not stop the service from coming up.
    try:
        from work_buddy.ir.store import recover_vector_store

        summary = recover_vector_store(cfg)
        if summary.get("quarantined") or summary.get("temps_removed"):
            print(
                "IR vector store recovery: "
                f"quarantined={len(summary['quarantined'])} "
                f"temps_removed={len(summary['temps_removed'])}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(
            f"IR vector store recovery raised (non-fatal): {exc}",
            file=sys.stderr,
        )

    # Surface LM Studio configuration drift at startup (loud but
    # non-fatal). Keeps the user informed when they've opted into
    # offloading but LM Studio isn't actually up, without blocking
    # the service — fallback paths kick in at encode time.
    try:
        _validate_lmstudio_providers(cfg)
    except Exception as exc:
        print(
            f"LM Studio provider validation raised (non-fatal): {exc}",
            file=sys.stderr,
        )

    # Enable in-service mode for dense retrieval so it calls models
    # directly instead of HTTP round-tripping to itself. Set before
    # the warmup thread runs so any early-arriving call sees it.
    import work_buddy.ir.dense
    work_buddy.ir.dense._IN_SERVICE = True

    def _warmup() -> None:
        try:
            for _key, entry in _registry.items():
                if entry.eager:
                    _load_model(entry)
            _prewarm_search_cache()
        except Exception:
            # Warmup failures are logged inside _load_model /
            # _prewarm_search_cache; don't kill the thread silently.
            import traceback
            traceback.print_exc()

    threading.Thread(target=_warmup, name="embedding-warmup", daemon=True).start()
    # Idle-evict non-eager models so a one-off bulk-encode doesn't
    # permanently pin their RAM footprint.
    threading.Thread(
        target=_idle_evictor_loop,
        name="embedding-idle-evictor",
        daemon=True,
    ).start()
    # Release the resident vault search matrix when search goes idle, so an
    # occasional vault query doesn't pin hundreds of MB in this long-lived process.
    threading.Thread(
        target=_vault_matrix_evictor_loop,
        name="vault-matrix-evictor",
        daemon=True,
    ).start()
    # Release idle resident matrices of the consolidated index (flag-gated; additive —
    # does NOT replace the model/vault evictors above). One sweep for all its partitions.
    try:
        from work_buddy.index.resident import start_idle_evictor as _start_index_evictor
        _start_index_evictor()
    except Exception as exc:  # never block service startup
        print(f"consolidated-index evictor start failed (non-fatal): {exc}", file=sys.stderr)
    # Prewarm the consolidated index's resident matrices at startup (flag-gated; runs in
    # a background daemon so it never blocks /health or query serving). Without it the
    # FIRST post-restart search of a large partition pays the full cold matrix-load and
    # can time out to None; warming up front removes that first-query penalty. Pairs with
    # the idle evictor above (warm → serve → release when idle → re-warm on next query).
    try:
        from work_buddy.index.partitioned import start_prewarm as _start_index_prewarm
        _start_index_prewarm()
    except Exception as exc:  # never block service startup
        print(f"consolidated-index prewarm start failed (non-fatal): {exc}", file=sys.stderr)
    # Persist completed broker calls so the dashboard Inference panel keeps
    # history across restarts (the broker's metrics ring is in-memory only).
    threading.Thread(
        target=_broker_metrics_persist_loop,
        name="broker-metrics-persist",
        daemon=True,
    ).start()

    from work_buddy.web.access_log_filter import install_probe_log_filter
    install_probe_log_filter(["/health"])
    print(f"Embedding service running on http://127.0.0.1:{port}", file=sys.stderr)
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
