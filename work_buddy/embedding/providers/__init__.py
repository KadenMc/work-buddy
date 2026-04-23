"""Embedding provider backends.

Each provider implements ``encode(texts, *, model_id, base_url, ...)``
returning an ``(N, D)`` float32 ``np.ndarray`` for a batch of texts.
Provider dispatch happens in ``work_buddy.ir.dense._encode_bulk_direct``
based on per-model ``embedding.models.<key>.provider`` config.

The default provider is the in-process sentence-transformers path —
this package is for *optional alternative* providers. Today that
means LM Studio's ``/v1/embeddings`` endpoint, which lets the user
offload the passage encoder to a remote GPU via LM Link.

Provider abstractions intentionally stay narrow: bulk document
encoding only. Query encoding never offloads (see
``work_buddy/ir/dense.py::encode_query``) because query latency is
user-facing and a network hop hurts.
"""
