"""General-purpose information retrieval — hybrid BM25 + dense over structured text.

Public API:
    search()          — query the index (BM25 + optional dense + RRF fusion)
    search_against()  — ad-hoc hybrid search over a list of strings (no index needed)
    build_index()     — index documents from one or all sources
    index_status()    — report index health and stats
"""

from work_buddy.ir.engine import search, search_against, top_k_weighted_score  # noqa: F401
from work_buddy.ir.store import build_index, index_status  # noqa: F401
