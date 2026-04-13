"""Client for the shared embedding service.

Talks to the HTTP API on localhost. Falls back gracefully if the
service isn't running (returns empty results, no errors).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from pathlib import Path

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

from work_buddy.paths import resolve

_LOG_PATH = resolve("logs/search-debug")


def _debug(msg: str) -> None:
    import time
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _base_url() -> str:
    cfg = load_config()
    port = cfg.get("embedding", {}).get("service_port", 5124)
    return f"http://127.0.0.1:{port}"


def _request(method: str, path: str, data: dict | None = None, timeout: int = 30) -> dict | None:
    """Make a request to the embedding service."""
    url = f"{_base_url()}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError) as exc:
        logger.debug("Embedding service request failed: %s", exc)
        return None


def is_available() -> bool:
    """Check if the embedding service is running."""
    import time
    t = time.time()
    result = _request("GET", "/health", timeout=3)
    available = result is not None and result.get("status") == "ok"
    _debug(f"Embedding health check: {available} ({time.time()-t:.2f}s)")
    return available


def embed(
    texts: list[str],
    *,
    model: str | None = None,
    prompt_name: str | None = None,
) -> list[list[float]] | None:
    """Embed texts via the shared service. Returns None if unavailable.

    Args:
        texts: Texts to embed.
        model: Model key (e.g. "leaf-mt", "leaf-ir"). Defaults to service default.
        prompt_name: Prompt name for asymmetric models (e.g. "query", "document").
    """
    payload: dict[str, Any] = {"texts": texts}
    if model:
        payload["model"] = model
    if prompt_name:
        payload["prompt_name"] = prompt_name
    # Larger batches need more time (especially leaf-ir doc encoding on CPU/GPU)
    timeout = max(30, len(texts) * 2)
    result = _request("POST", "/embed", payload, timeout=timeout)
    if result is None:
        return None
    return result.get("vectors")


def embed_for_ir(
    texts: list[str],
    role: str = "document",
) -> list[list[float]] | None:
    """Convenience wrapper for IR encoding.

    Query encoding uses ``leaf-ir-query`` (MongoDB/mdbr-leaf-ir, 90 MB,
    eager-loaded at startup) so search is instant.  Document encoding uses
    ``leaf-ir`` (MongoDB/mdbr-leaf-ir-asym, 526 MB, lazy-loaded only when
    indexing).  Both produce compatible 768-d vectors — the asymmetric model
    card documents this split usage pattern.

    Args:
        texts: Texts to embed.
        role: "query" for search queries, "document" for indexing documents.
    """
    if role not in ("query", "document"):
        raise ValueError(f"role must be 'query' or 'document', got '{role}'")
    if role == "query":
        return embed(texts, model="leaf-ir-query", prompt_name="query")
    return embed(texts, model="leaf-ir", prompt_name="document")


def ir_search(
    query: str,
    *,
    source: str | None = None,
    scope: str | None = None,
    metadata_filter: dict[str, str] | None = None,
    top_k: int = 10,
    bm25_only: bool = False,
    dense_only: bool = False,
) -> list[dict] | None:
    """Search indexed documents via the IR engine on the embedding service.

    Returns list of result dicts, or None if service unavailable.
    """
    payload: dict[str, Any] = {"query": query, "top_k": top_k, "bm25_only": bm25_only}
    if dense_only:
        payload["dense_only"] = True
    if source:
        payload["source"] = source
    if scope:
        payload["scope"] = scope
    if metadata_filter:
        payload["metadata_filter"] = metadata_filter
    result = _request("POST", "/ir/search", payload, timeout=30)
    if result is None:
        return None
    return result.get("results")


def ir_index(
    action: str = "build",
    *,
    source: str = "conversation",
    days: int = 30,
    force: bool = False,
) -> dict | None:
    """Build or check the IR index via the embedding service.

    Returns stats/status dict, or None if service unavailable.
    """
    payload: dict[str, Any] = {
        "action": action,
        "source": source,
        "days": days,
        "force": force,
    }
    # Index building can be slow (bulk encoding)
    timeout = 30 if action == "status" else 300
    result = _request("POST", "/ir/index", payload, timeout=timeout)
    if result is None:
        return None
    return result.get("result")


def similarity_search(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Score a query against candidates via the shared service.

    Args:
        query: Search query text.
        candidates: List of {"name": str, "texts": [str, ...]} dicts.
        model: Model key. Defaults to service default.

    Returns:
        List of {"name": str, "score": float} sorted by score descending.
        Returns empty list if service is unavailable.
    """
    import time
    t = time.time()
    _debug(f"Calling embedding similarity ({len(candidates)} candidates)...")
    payload: dict[str, Any] = {"query": query, "candidates": candidates}
    if model:
        payload["model"] = model
    result = _request("POST", "/similarity", payload)
    _debug(f"Embedding similarity done in {time.time()-t:.2f}s")
    if result is None:
        return []
    return result.get("results", [])


def hybrid_search(
    query: str,
    candidates: list[dict[str, Any]],
    bm25_weight: float = 0.4,
    embed_weight: float = 0.6,
    *,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Combined BM25 + embedding search via the shared service.

    Args:
        query: Search query text.
        candidates: List of {"name": str, "texts": [str, ...]} dicts.
        bm25_weight: Weight for BM25 scores (default 0.4).
        embed_weight: Weight for embedding scores (default 0.6).
        model: Model key for embedding scoring. Defaults to service default.

    Returns:
        List of {"name": str, "score": float, "bm25_score": float, "embed_score": float}
        sorted by score descending. Returns empty list if service is unavailable.
    """
    _debug(f"Calling hybrid search ({len(candidates)} candidates)...")
    import time
    t = time.time()
    payload: dict[str, Any] = {
        "query": query,
        "candidates": candidates,
        "bm25_weight": bm25_weight,
        "embed_weight": embed_weight,
    }
    if model:
        payload["model"] = model
    result = _request("POST", "/search", payload)
    _debug(f"Hybrid search done in {time.time()-t:.2f}s")
    if result is None:
        return []
    return result.get("results", [])
