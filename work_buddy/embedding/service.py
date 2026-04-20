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
    """Runtime state for a registered model."""

    key: str  # short name, e.g. "leaf-mt"
    hf_name: str  # HuggingFace model ID
    dims: int
    eager: bool = True
    model: Any = field(default=None, repr=False)  # SentenceTransformer | None
    load_time_s: float | None = None
    status: str = "pending"  # "pending" | "loaded" | "error"
    error: str | None = None
    last_used_at: float = 0.0  # monotonic timestamp of last _get_model hit
    # Guards against the "loaded model never released" leak. Models
    # like ``leaf-ir`` get lazy-loaded on first bulk encode (every 5
    # minutes via ir-index-rebuild) and, without eviction, stay in
    # RAM for the life of the service — hundreds of MB per model.
    # The idle-evictor thread below drops models whose
    # last_used_at is older than IDLE_EVICT_SECONDS.


_registry: dict[str, ModelEntry] = {}
_default_model_key: str = _DEFAULT_MODEL
_device: str | None = None  # resolved device string ("cpu", "cuda", etc.)
_registry_lock = __import__("threading").RLock()

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
    """Return a loaded model by key, loading lazily if needed."""
    key = key or _default_model_key
    entry = _registry.get(key)
    if entry is None:
        raise ValueError(f"Unknown model '{key}'. Available: {list(_registry.keys())}")
    with _registry_lock:
        if entry.model is None and entry.status != "error":
            _load_model(entry)
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

    vectors = model.encode(texts, **encode_kwargs)

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

    vectors = model.encode(all_texts, batch_size=32, show_progress_bar=False)
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
            query_vec = model.encode([query], batch_size=1, show_progress_bar=False)[0]
        else:
            # Cache miss — encode all candidates, cache them, then encode query
            cand_texts = []
            text_to_name = []
            for cand in candidates:
                name = cand.get("name", "")
                for phrase in cand.get("texts", []):
                    text_to_name.append((name, len(cand_texts)))
                    cand_texts.append(phrase)

            cand_vectors = model.encode(cand_texts, batch_size=32, show_progress_bar=False)
            _candidate_cache[model_key] = (fp, cand_vectors, text_to_name)
            query_vec = model.encode([query], batch_size=1, show_progress_bar=False)[0]

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

    print(f"Embedding service running on http://127.0.0.1:{port}", file=sys.stderr)
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
