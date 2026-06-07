"""Deterministic in-memory search backend — drives unit tests and dry-runs
with zero network. Mirrors the FakeEmailProvider / FakeCalendarProvider shape:
a static ``name``, a no-arg constructor seeded with fixtures, ``add_hit`` /
``add_many`` seeders for tests, and protocol methods that never touch the wire.

``search`` is deterministic: it returns the seeded hits (optionally filtered to
those whose title/snippet contains a query token), sliced to ``max_results``.
The result is identical across calls for the same query, so router cache tests
("hit on 2nd identical query") and fallback tests are reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable

from work_buddy.websearch.models import SearchHit

_DEFAULT_FIXTURES: tuple[SearchHit, ...] = (
    SearchHit(
        title="work-buddy — personal agent framework",
        url="https://example.com/work-buddy",
        snippet="work-buddy orchestrates tasks and workflows over Claude Code and MCP.",
        provider="fake",
        published="2026-01-01T00:00:00Z",
        score=0.99,
        raw_text="work-buddy is a personal agent framework. It exposes capabilities "
        "and workflows through an MCP gateway. This is fixture full text.",
    ),
    SearchHit(
        title="Provider seam pattern",
        url="https://example.com/provider-seam",
        snippet="A runtime-checkable Protocol plus a config-driven factory.",
        provider="fake",
        published="2026-02-02T00:00:00Z",
        score=0.80,
    ),
    SearchHit(
        title="Evidence cards for LLM classification",
        url="https://example.org/evidence-cards",
        snippet="Judge retrieved evidence, not the open web.",
        provider="fake",
        published=None,
        score=0.55,
    ),
)


class FakeSearchProvider:
    """In-memory, deterministic, no-network search provider."""

    name = "fake"

    def __init__(self, hits: Iterable[SearchHit] | None = None) -> None:
        self._hits: list[SearchHit] = list(hits) if hits is not None else list(_DEFAULT_FIXTURES)

    # --- test seeders ------------------------------------------------------

    def add_hit(self, hit: SearchHit) -> None:
        self._hits.append(hit)

    def add_many(self, hits: Iterable[SearchHit]) -> None:
        self._hits.extend(hits)

    # --- protocol ----------------------------------------------------------

    def health(self) -> dict:
        return {"ok": True, "provider": "fake", "backend": "fake", "hits_seeded": len(self._hits)}

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        topic: str | None = None,
        time_range: str | None = None,
        since: str | None = None,
    ) -> list[SearchHit]:
        q = (query or "").lower().strip()
        if q:
            tokens = [t for t in q.split() if t]
            matched = [
                h for h in self._hits
                if any(t in h.title.lower() or t in h.snippet.lower() for t in tokens)
            ]
            # Deterministic fallback: if nothing matched, return all seeded hits
            # so callers always get a usable, reproducible result set.
            hits = matched or list(self._hits)
        else:
            hits = list(self._hits)
        return hits[: max(0, int(max_results))]

    def supports(self, feature: str) -> bool:
        # The fake advertises full_text so extraction short-circuit paths are
        # exercisable without a live Jina backend.
        return feature in {"full_text", "time_filter", "news"}
