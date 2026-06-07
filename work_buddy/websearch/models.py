"""Canonical data shapes for the websearch subsystem.

All models are frozen dataclasses with ``to_dict()`` helpers so capability
wrappers can emit JSON-serialisable payloads without re-deriving the shape.

``SearchHit`` is the backend-neutral search result. ``EvidenceCard`` is the
compact, cited, LLM-facing projection (the load-bearing trick: the classifier
judges *retrieved evidence*, not the open web). ``FetchResult`` is one extracted
page. ``ClassifyResult`` is the structured verdict from the LOCAL_FAST classify.

These four shapes are the frozen public contract the future Events
``Processor``/``Condition`` adapters call (spec §7) — keep them stable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchHit:
    """One backend-neutral search result.

    ``raw_text`` carries full page text only when the backend returns it
    (Jina's reader does; ddgs does not). When present, downstream extraction
    short-circuits instead of re-fetching the URL.
    """

    title: str
    url: str
    snippet: str
    provider: str                 # which backend produced it ("jina"|"ddgs"|"fake")
    published: str | None = None  # RFC3339 if known
    score: float | None = None
    raw_text: str | None = None   # full page text if the backend returns it; else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceCard:
    """The compact, cited, LLM-facing shape fed to the classifier.

    Never carries raw search dumps — only a snippet or extracted text — so the
    classify prompt stays small and the model reasons over *evidence*, not the
    web at large.
    """

    title: str
    source: str                   # domain / provider
    url: str
    snippet: str
    published: str | None = None
    matched_terms: list[str] = field(default_factory=list)
    why_retrieved: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FetchResult:
    """One extracted page. ``extractor`` records which path produced ``text``
    (``"jina_reader"`` for Jina's r.jina.ai, ``"trafilatura"`` otherwise)."""

    url: str
    canonical_url: str
    text: str
    fetched_at: str               # RFC3339
    extractor: str                # "jina_reader" | "trafilatura"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClassifyResult:
    """Structured verdict from :func:`work_buddy.websearch.classify.classify_evidence`.

    ``relevant`` defaults to ``False`` at the call site on any classify error —
    a watcher must not fire on an inconclusive judgment.
    """

    relevant: bool
    confidence: float
    reason: str
    evidence_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
