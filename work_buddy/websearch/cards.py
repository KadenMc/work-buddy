"""Shape ``SearchHit``s into ``EvidenceCard``s — the compact, cited, LLM-facing
projection the classifier reasons over.

The load-bearing trick: the model judges *retrieved evidence*,
not the open web. So a card carries a short snippet (or a bounded slice of
extracted text), the source domain, the URL, and provenance (``matched_terms`` /
``why_retrieved``) — never a raw search dump. Snippets are truncated so the
classify prompt stays small and cheap.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlparse

from work_buddy.websearch.models import EvidenceCard, SearchHit

_SNIPPET_MAX = 500


def _domain(url: str, fallback: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc[4:] if netloc.startswith("www.") else (netloc or fallback)
    except ValueError:
        return fallback


def _snippet(hit: SearchHit) -> str:
    text = (hit.snippet or "").strip()
    if not text and hit.raw_text:
        text = " ".join(hit.raw_text.split())  # collapse whitespace from full text
    if len(text) > _SNIPPET_MAX:
        text = text[:_SNIPPET_MAX].rsplit(" ", 1)[0] + "…"
    return text


def to_evidence_cards(
    hits: Sequence[SearchHit],
    *,
    watch_label: str = "",
    matched_terms: Sequence[str] = (),
    why: str = "",
) -> list[EvidenceCard]:
    """Project hits into compact cited cards. ``matched_terms``/``why`` carry
    provenance through to the classifier; ``watch_label`` is folded into
    ``why_retrieved`` when present (e.g. a watcher's entity label)."""
    terms = list(matched_terms)
    why_base = why or (f"retrieved for: {watch_label}" if watch_label else "")
    cards: list[EvidenceCard] = []
    for hit in hits:
        cards.append(EvidenceCard(
            title=hit.title or "(untitled)",
            source=_domain(hit.url, hit.provider),
            url=hit.url,
            snippet=_snippet(hit),
            published=hit.published,
            matched_terms=list(terms),
            why_retrieved=why_base,
        ))
    return cards
