"""Client for the shared embedding service.

Talks to the HTTP API on localhost. Falls back gracefully if the
service isn't running (returns empty results, no errors).
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pathlib import Path

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

from work_buddy.paths import resolve

_LOG_PATH = resolve("logs/search-debug")


def _debug(msg: str) -> None:
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


def _request(
    method: str,
    path: str,
    data: dict | None = None,
    timeout: int = 30,
    *,
    return_http_error: bool = False,
) -> dict | None:
    """Make a request to the embedding service.

    Returns the parsed JSON response on success. On failure:

    - A genuine connection failure (service down / refused / timeout) returns
      ``None`` — the universal "service unavailable" signal every caller already
      degrades on.
    - An HTTP error status (the service was reached but the endpoint failed,
      e.g. ``/ir/index`` 500) is always logged with its body. By default it also
      returns ``None`` (preserving the historical contract for all callers);
      when ``return_http_error=True`` it instead returns
      ``{"error": <message>, "status": <code>}`` so the caller can surface the
      real error instead of masking it as "service unavailable".
    """
    url = f"{_base_url()}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        # HTTPError subclasses URLError, so it MUST be caught first — otherwise a
        # 500's body (carrying the real error) is swallowed as a generic
        # connection failure.
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        logger.warning(
            "Embedding %s %s -> HTTP %s: %s", method, path, exc.code, raw[:500]
        )
        if not return_http_error:
            return None
        message = raw or f"HTTP {exc.code}"
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("error"):
                    message = parsed["error"]
            except ValueError:
                pass
        return {"error": message, "status": exc.code}
    except (URLError, TimeoutError) as exc:
        logger.debug("Embedding service request failed: %s", exc)
        return None


def is_available() -> bool:
    """Check if the embedding service is running."""
    t = time.time()
    result = _request("GET", "/health", timeout=3)
    available = result is not None and result.get("status") == "ok"
    _debug(f"Embedding health check: {available} ({time.time()-t:.2f}s)")
    return available


def wait_until_available(
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
) -> bool:
    """Block until the embedding service reports ``status: ok``, or
    return False on timeout.

    Use this from background warmup paths (e.g. the knowledge-dense
    warmup thread) so they don't fire embed batches against a service
    that's still cold-loading models. The service's ``/health``
    endpoint returns ``{"status": "loading"}`` while at least one
    model is in pending-load state — :func:`is_available` already
    treats that as "not ready" — so the wait simply polls
    ``is_available`` until True.

    Bounded by ``timeout_s`` (default 30s — enough headroom for the
    eager ``leaf-mt`` to finish its initial load on a typical
    machine; the asymmetric ``leaf-ir`` is lazy-loaded on first doc
    embed so it doesn't gate the health check).

    Returns True if the service became ready within the budget; False
    if the timeout elapsed first. Caller decides whether a False
    return is logged as a soft skip (warmup) or a hard error
    (synchronous user call).
    """
    deadline = time.monotonic() + timeout_s
    # First check is free — many callsites will be lucky and not
    # have to sleep at all.
    if is_available():
        return True
    while time.monotonic() < deadline:
        time.sleep(interval_s)
        if is_available():
            return True
    return False


def embed(
    texts: list[str],
    *,
    model: str | None = None,
    prompt_name: str | None = None,
    timeout_s: int | None = None,
) -> list[list[float]] | None:
    """Embed texts via the shared service. Returns None if unavailable.

    Args:
        texts: Texts to embed.
        model: Model key (e.g. "leaf-mt", "leaf-ir"). Defaults to service default.
        prompt_name: Prompt name for asymmetric models (e.g. "query", "document").
        timeout_s: Override the per-request timeout in seconds. When None,
            uses ``max(30, len(texts) * 2)``. Indexing callers should pass a
            larger value when targeting a lazy-loaded model — the first
            request triggers a SentenceTransformer instantiation that for
            large passage encoders runs into the tens of seconds, well past
            the default 30s floor for small cache-miss batches.
    """
    payload: dict[str, Any] = {"texts": texts}
    if model:
        payload["model"] = model
    if prompt_name:
        payload["prompt_name"] = prompt_name
    if timeout_s is None:
        # Larger batches need more time (especially leaf-ir doc encoding on CPU/GPU)
        timeout = max(30, len(texts) * 2)
    else:
        timeout = timeout_s
    result = _request("POST", "/embed", payload, timeout=timeout)
    if result is None:
        return None
    return result.get("vectors")


def embed_for_ir(
    texts: list[str],
    role: str = "document",
    *,
    timeout_s: int | None = None,
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
        timeout_s: Override the per-request timeout. Indexing callers using
            ``role="document"`` should pass a value large enough to absorb a
            cold ``leaf-ir`` SentenceTransformer load. See ``embed()`` for
            the underlying rationale; see
            ``work_buddy.knowledge.index._CONTENT_COLD_LOAD_TIMEOUT_S`` for a
            tuned reference value.
    """
    if role not in ("query", "document"):
        raise ValueError(f"role must be 'query' or 'document', got '{role}'")
    if role == "query":
        return embed(texts, model="leaf-ir-query", prompt_name="query", timeout_s=timeout_s)
    return embed(texts, model="leaf-ir", prompt_name="document", timeout_s=timeout_s)


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

    Returns:
        - the stats/status dict on success;
        - ``{"error": <message>, "status": <code>}`` if the service was reachable
          but the request failed (e.g. ``/ir/index`` returned 500 because a
          vector file was corrupt) — so the caller surfaces the real error;
        - ``None`` if the service is unreachable (connection refused / timeout).
    """
    payload: dict[str, Any] = {
        "action": action,
        "source": source,
        "days": days,
        "force": force,
    }
    # Index building can be slow (bulk encoding)
    timeout = 30 if action == "status" else 300
    result = _request(
        "POST", "/ir/index", payload, timeout=timeout, return_http_error=True
    )
    if result is None:
        return None
    if "result" in result:
        return result["result"]
    return result  # HTTP-error envelope: {"error": ..., "status": ...}


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
