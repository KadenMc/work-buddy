"""Unit tests — websearch provider seam (factory + fake backend + models).

No network: everything runs against FakeSearchProvider. Mirrors
tests/unit/test_calendar_provider.py and test_fake_calendar_provider.py.
"""

from __future__ import annotations

import pytest

from work_buddy.websearch.errors import WebSearchProviderDisabled
from work_buddy.websearch.models import (
    ClassifyResult,
    EvidenceCard,
    FetchResult,
    SearchHit,
)
from work_buddy.websearch.provider import SearchProvider, get_search_provider
from work_buddy.websearch.providers.fake import FakeSearchProvider


def _patch_cfg(monkeypatch, websearch_cfg: dict) -> None:
    """Patch work_buddy.config.load_config (the factory imports it at call time)."""
    import work_buddy.config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {"websearch": websearch_cfg})


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_selects_fake(monkeypatch):
    _patch_cfg(monkeypatch, {"provider": "fake"})
    assert isinstance(get_search_provider("fake"), FakeSearchProvider)


def test_factory_disabled_raises(monkeypatch):
    _patch_cfg(monkeypatch, {"enabled": False})
    with pytest.raises(WebSearchProviderDisabled):
        get_search_provider("fake")


def test_factory_unknown_provider_raises(monkeypatch):
    _patch_cfg(monkeypatch, {})
    with pytest.raises(WebSearchProviderDisabled):
        get_search_provider("nope-not-a-backend")


def test_factory_enabled_default_true(monkeypatch):
    # No explicit enabled key → defaults to enabled.
    _patch_cfg(monkeypatch, {})
    assert isinstance(get_search_provider("fake"), FakeSearchProvider)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_fake_satisfies_protocol():
    assert isinstance(FakeSearchProvider(), SearchProvider)


# ---------------------------------------------------------------------------
# Fake backend behaviour
# ---------------------------------------------------------------------------


def test_fake_search_returns_seeded_fixtures():
    prov = FakeSearchProvider()
    hits = prov.search("work-buddy")
    assert hits
    assert all(isinstance(h, SearchHit) for h in hits)
    assert all(h.provider == "fake" for h in hits)


def test_fake_search_query_filters_then_falls_back():
    prov = FakeSearchProvider()
    # A token present in a fixture title/snippet narrows the set.
    only_wb = prov.search("work-buddy")
    assert any("work-buddy" in h.title.lower() or "work-buddy" in h.snippet.lower() for h in only_wb)
    # A token in nothing falls back to all seeded hits (deterministic, non-empty).
    fallback = prov.search("zzz-no-match-token")
    assert len(fallback) == len(prov.search(""))


def test_fake_search_respects_max_results():
    prov = FakeSearchProvider()
    assert len(prov.search("", max_results=1)) == 1
    assert len(prov.search("", max_results=0)) == 0


def test_fake_search_is_deterministic():
    prov = FakeSearchProvider()
    assert prov.search("provider") == prov.search("provider")


def test_fake_add_hit_and_health():
    prov = FakeSearchProvider(hits=[])
    assert prov.search("anything") == []
    prov.add_hit(SearchHit(title="t", url="https://x", snippet="s", provider="fake"))
    assert len(prov.search("")) == 1
    h = prov.health()
    assert h["ok"] is True and h["provider"] == "fake"


def test_fake_supports():
    prov = FakeSearchProvider()
    assert prov.supports("full_text") is True
    assert prov.supports("nonexistent-feature") is False


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_models_to_dict_roundtrip():
    hit = SearchHit(title="t", url="https://x", snippet="s", provider="jina", score=0.5)
    d = hit.to_dict()
    assert d["title"] == "t" and d["provider"] == "jina" and d["score"] == 0.5

    card = EvidenceCard(title="t", source="x.com", url="https://x", snippet="s",
                        matched_terms=["a"], why_retrieved="because")
    assert card.to_dict()["matched_terms"] == ["a"]

    fr = FetchResult(url="https://x", canonical_url="https://x", text="body",
                     fetched_at="2026-01-01T00:00:00Z", extractor="trafilatura")
    assert fr.to_dict()["extractor"] == "trafilatura"

    cr = ClassifyResult(relevant=True, confidence=0.9, reason="r", evidence_urls=["https://x"])
    assert cr.to_dict()["relevant"] is True


def test_evidence_card_defaults_independent():
    a = EvidenceCard(title="a", source="s", url="u", snippet="x")
    b = EvidenceCard(title="b", source="s", url="u", snippet="y")
    assert a.matched_terms == [] and b.matched_terms == []
    assert a.matched_terms is not b.matched_terms
