"""Reciprocal Rank Fusion — the single source for the consolidated index.

A verbatim lift of ``ir/store.py::rrf_fuse`` (parity-tested) so the consolidated index
and the IR engine fuse identically. ``RRF_K`` is the algorithm's neutral default (60,
Cormack/Clarke/Buettcher 2009); per-partition ``rrf_k`` overrides live in config
(fork F-RRFK — the smaller per-partition default that the searcher passes in).

Pure Python — safe to import anywhere (no numpy, no sqlite3).
"""

from __future__ import annotations

RRF_K = 60


def rrf_fuse(rankings: list[dict[str, float]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion over multiple ``{doc_id: score}`` ranking lists.

    Each ranking is ranked by score desc; a doc's fused score is the sum of
    ``1 / (k + rank)`` (1-based) across every ranking it appears in. Empty
    rankings contribute nothing.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        if not ranking:
            continue
        sorted_ids = sorted(ranking, key=ranking.get, reverse=True)
        for rank, doc_id in enumerate(sorted_ids, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused
