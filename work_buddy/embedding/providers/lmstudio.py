"""LM Studio embedding provider — POSTs to ``/v1/embeddings``.

Used by ``work_buddy.ir.dense._encode_bulk_direct`` when a model's
``embedding.models.<key>.provider`` config is set to ``lmstudio``.
Query encoding is NOT routed here (see module docstring on
``work_buddy.embedding.providers``).

LM Studio exposes an OpenAI-compatible embeddings endpoint. The
request shape is the same shape Nomic and Snowflake ship in their
sentence-transformers-style docs — the user is expected to have
pre-loaded a GGUF that llama.cpp's BERT embedding path supports (see
``docs/handbook/features_lmstudio-offload-setup.md`` for the full
procedure, including the GGUF-metadata audit that prevents silently-
wrong vectors from pooling or normalization mismatches).

Error handling reuses ``interpret_httpx_exception`` from the LLM
backends so the same error_kind vocabulary (``server_unreachable``,
``lm_link_dropped``, etc.) applies here. Callers decide whether to
fall back to the local sentence-transformers path based on the
``on_error`` config and the ``LocalInferenceError`` they catch.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import numpy as np

from work_buddy.llm.backends._errors import (
    LocalInferenceError,
    interpret_httpx_exception,
)

logger = logging.getLogger(__name__)

# Default base URL when ``lmstudio.base_url`` isn't set in config.
# Matches LM Studio's out-of-box server binding.
_DEFAULT_BASE_URL = "http://localhost:1234"


def resolve_base_url(cfg: dict[str, Any] | None = None) -> str:
    """Return the LM Studio base URL from config, or the default.

    The ``lmstudio.base_url`` config key is the single source of truth
    for both the embedding provider and the ``work_buddy.health``
    reachable check. The URL is the bare server root — paths like
    ``/v1/embeddings`` and ``/v1/models`` are appended at use time.

    Args:
        cfg: Loaded config dict (from ``work_buddy.config.load_config``).
            If None, loads lazily so this helper can be called from
            contexts that don't want to pay the config-load cost.

    Returns:
        Base URL without trailing slash, e.g. ``http://localhost:1234``.
    """
    if cfg is None:
        from work_buddy.config import load_config
        cfg = load_config()
    url = cfg.get("lmstudio", {}).get("base_url", _DEFAULT_BASE_URL)
    if not isinstance(url, str) or not url:
        url = _DEFAULT_BASE_URL
    return url.rstrip("/")


def _profile_name(model_id: str) -> str:
    """Broker profile name for an LM Studio embedding call.

    Format: ``lmstudio:<model_id>`` — keeps profiles addressable from
    config (``inference.profiles.<name>``) and distinguishes embedding
    from LLM workloads when the LLM backends also adopt the broker.
    """
    return f"lmstudio:{model_id}"


def encode(
    texts: list[str],
    *,
    model_id: str,
    base_url: str | None = None,
    timeout: float = 20.0,
    api_key_env: str | None = None,
    batch_size: int = 64,
    priority: "Priority | None" = None,
    queue_wait_s: float = 15.0,
) -> np.ndarray:
    """Encode a batch of texts via LM Studio's ``/v1/embeddings`` endpoint.

    Args:
        texts: Texts to encode. Passed through as-is — LM Studio /
            llama.cpp does not honor the sentence-transformers
            ``prompt_name`` convention, so any query-side prefixes
            would have to be prepended by the caller. (Moot here:
            this provider is only wired into bulk DOCUMENT encoding,
            and asymmetric models like ``snowflake-arctic-embed-m-
            v1.5`` have no document-side prefix.)
        model_id: Model id as exposed by LM Studio's ``/v1/models``
            (e.g. ``text-embedding-snowflake-arctic-embed-m-v1.5``).
            Must match a model reachable from this LM Studio instance —
            either loaded already or cataloged so LM Studio can JIT-load
            on first request. If the model isn't downloaded locally and
            no LM Link device is surfacing it, the call errors with
            ``model_not_available`` via the error interpreter.
        base_url: LM Studio base URL without ``/v1`` suffix. Falls back
            to ``resolve_base_url()`` when None.
        timeout: HTTP timeout per batch. Default 20s (down from the
            prior 60s): most embedding calls return in 1-3s, and a
            tighter ceiling keeps the blast radius small when LM
            Studio stalls (so the caller's fallback path kicks in
            before upstream callers trip their own 30s deadlines).
        api_key_env: Optional env var holding a bearer token. LM
            Studio is unauth by default — leave None unless you've
            enabled auth.
        batch_size: Texts per request. LM Studio accepts arrays
            natively; chunking keeps individual payloads bounded so
            a single mis-sized text doesn't tank the whole call.
        priority: Broker priority class. Defaults to ``BACKGROUND``
            because this function is only wired into bulk document
            encoding (ir-index-rebuild cron) — which MUST yield to
            interactive dashboard searches that also hit LM Studio.
            Callers that invoke this from user-facing paths should
            pass a higher priority explicitly.
        queue_wait_s: Maximum time to wait for a broker slot before
            giving up. On timeout, raises ``QueueWaitTimeout`` —
            which the caller's fallback logic can catch same as any
            other ``LocalInferenceError``. 15s matches the tighter
            inference budget.

    Returns:
        ``(len(texts), D)`` float32 ``np.ndarray``. Vectors are
        server-side L2-normalized by llama.cpp for any non-``none``
        pooling mode, which is what asymmetric retrieval embeddings
        use.

    Raises:
        LocalInferenceError on any HTTP or transport failure.
        QueueWaitTimeout if the broker couldn't admit within
            ``queue_wait_s``. QueueFull if the broker's queue at
            this priority is at capacity.
        The caller (``_encode_bulk_direct`` in ``ir/dense.py``)
        decides whether to fall back based on ``on_error`` config.
    """
    # Deferred imports: the broker module imports work_buddy.config,
    # which works in the sidecar but sometimes not in bare test rigs.
    # Keep the import lazy so this module stays cheap to import.
    from work_buddy.inference import get_broker, Priority as _Priority

    prio = priority if priority is not None else _Priority.BACKGROUND
    broker = get_broker()
    profile = _profile_name(model_id)

    if base_url is None:
        base_url = resolve_base_url()

    # Admit under the broker; httpx does the inference-timeout
    # enforcement (20s default) inside the slot.
    with broker.slot(
        profile=profile,
        priority=prio,
        queue_wait_s=queue_wait_s,
        inference_s=timeout,
    ) as ticket:
        return _encode_inside_slot(
            texts,
            model_id=model_id,
            base_url=base_url,
            timeout=timeout,
            api_key_env=api_key_env,
            batch_size=batch_size,
            ticket=ticket,
        )


def _encode_inside_slot(
    texts: list[str],
    *,
    model_id: str,
    base_url: str,
    timeout: float,
    api_key_env: str | None,
    batch_size: int,
    ticket: "Ticket",
) -> np.ndarray:
    """Actual HTTP work. Extracted so the broker context is obvious in
    the public ``encode`` function's shape."""
    headers = {"Content-Type": "application/json"}
    if api_key_env:
        token = os.environ.get(api_key_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    url = base_url.rstrip("/") + "/v1/embeddings"
    all_vectors: list[list[float]] = []

    try:
        ticket.mark_started_http()
        with httpx.Client(timeout=timeout) as client:
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                response = client.post(
                    url,
                    headers=headers,
                    json={"input": batch, "model": model_id},
                )
                response.raise_for_status()
                body = response.json()
                data = body.get("data")
                if not isinstance(data, list):
                    raise LocalInferenceError(
                        f"LM Studio /v1/embeddings returned no 'data' "
                        f"field for model {model_id!r}",
                        kind="malformed_response",
                        hint=(
                            "Verify the loaded model is an embedding "
                            "model (not a chat model). Chat models "
                            "return a different response shape and "
                            "will fail this check."
                        ),
                        raw=body,
                    )
                # Sort by index — OpenAI-compat is supposed to return
                # in request order but defensive sort is cheap.
                data.sort(key=lambda d: d.get("index", 0))
                if len(data) != len(batch):
                    raise LocalInferenceError(
                        f"LM Studio /v1/embeddings returned "
                        f"{len(data)} vectors for a batch of "
                        f"{len(batch)} texts — refusing to proceed "
                        "with mismatched arity.",
                        kind="malformed_response",
                        hint=(
                            "This usually means the model silently "
                            "failed on some inputs (too long for the "
                            "context window, or rejected tokens). "
                            "Check LM Studio's server log."
                        ),
                        raw=body,
                    )
                for item in data:
                    emb = item.get("embedding")
                    if not isinstance(emb, list):
                        raise LocalInferenceError(
                            "LM Studio /v1/embeddings returned a "
                            "non-list 'embedding' entry.",
                            kind="malformed_response",
                            hint="",
                            raw=body,
                        )
                    all_vectors.append(emb)
    except httpx.HTTPError as exc:
        raise interpret_httpx_exception(
            exc, model=model_id, endpoint="/v1/embeddings",
        ) from exc

    arr = np.asarray(all_vectors, dtype=np.float32)
    # Defensive re-normalization: llama.cpp L2-normalizes server-side
    # for pooling != none, but some older builds / configurations skip
    # it. We rely on unit-norm vectors downstream (cosine == dot).
    norms = np.linalg.norm(arr, axis=1)
    # Guard against zero-norm rows (empty strings, bad inputs) — leave
    # them zero rather than dividing by zero.
    safe = np.where(norms > 0, norms, 1.0)
    if not np.allclose(norms[norms > 0], 1.0, atol=1e-3):
        logger.debug(
            "LM Studio embeddings were not unit-normalized "
            "(norm range %.4f..%.4f); re-normalizing in-place",
            float(norms[norms > 0].min()) if np.any(norms > 0) else 0.0,
            float(norms.max()),
        )
        arr = arr / safe[:, None]
    return arr


def validate_reachable(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe LM Studio for a liveness report suitable for startup logging.

    Returns ``{ok, base_url, detail}`` — purely informational, never
    raises. Used by the embedding service's startup validator so it
    can emit a loud WARN when a model is configured with
    ``provider: lmstudio`` but the server isn't up. Doesn't block
    service startup — the dispatch path handles the fall-through.
    """
    base_url = resolve_base_url(cfg)
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(base_url.rstrip("/") + "/v1/models")
            response.raise_for_status()
            body = response.json()
    except httpx.ConnectError:
        return {
            "ok": False,
            "base_url": base_url,
            "detail": (
                f"LM Studio not reachable at {base_url} "
                "(connection refused)"
            ),
        }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "base_url": base_url,
            "detail": f"LM Studio probe timed out on {base_url}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "base_url": base_url,
            "detail": f"LM Studio probe failed: {type(exc).__name__}: {exc}",
        }

    models = body.get("data") or []
    model_ids = [m.get("id") for m in models if isinstance(m, dict)]
    return {
        "ok": True,
        "base_url": base_url,
        "detail": (
            f"LM Studio at {base_url} reports "
            f"{len(model_ids)} loaded models"
        ),
        "model_ids": model_ids,
    }
