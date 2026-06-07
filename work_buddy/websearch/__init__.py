"""work-buddy WebSearch subsystem — a standalone, events-agnostic, provider-
neutral web-search + retrieval library.

Public surface (the frozen contract the future Events adapters call, spec §7):

- ``search`` / ``search_hits`` — routed multi-backend search (router layer)
- ``to_evidence_cards`` — shape hits into compact, cited LLM-facing cards
- ``classify_evidence`` — broker-admitted LOCAL_FAST relevance verdict
- ``extract_text`` — fetch + extract clean page text
- ``get_search_provider`` — resolve one concrete backend by name
- models: ``SearchHit``, ``EvidenceCard``, ``FetchResult``, ``ClassifyResult``

Backends shipped: ``jina`` (reliable default, full-text Markdown) + ``ddgs``
(no-key fallback) + ``fake`` (tests). Others are seam-ready, not built.

Imports are kept lazy where a submodule pulls an optional dependency, so
``import work_buddy.websearch`` stays cheap and a missing extra only bites the
backend that needs it.
"""

from __future__ import annotations

from work_buddy.websearch.errors import (
    WebSearchBadKey,
    WebSearchError,
    WebSearchProviderDisabled,
    WebSearchRateLimited,
    WebSearchTimeout,
    WebSearchUnavailable,
)
from work_buddy.websearch.models import (
    ClassifyResult,
    EvidenceCard,
    FetchResult,
    SearchHit,
)
from work_buddy.websearch.provider import SearchProvider, get_search_provider

__all__ = [
    # models
    "SearchHit",
    "EvidenceCard",
    "FetchResult",
    "ClassifyResult",
    # provider
    "SearchProvider",
    "get_search_provider",
    # errors
    "WebSearchError",
    "WebSearchProviderDisabled",
    "WebSearchUnavailable",
    "WebSearchRateLimited",
    "WebSearchTimeout",
    "WebSearchBadKey",
]


def __getattr__(name: str):
    """Lazily expose the higher layers (router/cards/classify/extract) so the
    package import stays light and free of optional-dep import cost until used.
    """
    if name in ("search", "search_hits"):
        from work_buddy.websearch import router
        return getattr(router, "search")
    if name == "to_evidence_cards":
        from work_buddy.websearch.cards import to_evidence_cards
        return to_evidence_cards
    if name == "classify_evidence":
        from work_buddy.websearch.classify import classify_evidence
        return classify_evidence
    if name == "extract_text":
        from work_buddy.websearch.extract import extract_text
        return extract_text
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
