"""Unit tests — extract / cards / classify. No network, no live LLM:
httpx is mocked, trafilatura.extract is stubbed, and LLMRunner.call is patched.
"""

from __future__ import annotations

import httpx
import pytest

import work_buddy.websearch.extract as extract_mod
from work_buddy.inference import Priority
from work_buddy.websearch.cards import to_evidence_cards
from work_buddy.websearch.classify import classify_evidence
from work_buddy.websearch.extract import extract_text
from work_buddy.websearch.models import EvidenceCard, SearchHit


def _mock_httpx(monkeypatch, handler):
    real = httpx.Client
    def factory(*a, **kw):
        kw.pop("transport", None)
        return real(*a, **kw, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(extract_mod.httpx, "Client", factory)


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------


def test_extract_raw_text_short_circuits(monkeypatch):
    # raw_text present → no network at all.
    def handler(req):  # pragma: no cover — must not be called
        raise AssertionError("network should not be touched")
    _mock_httpx(monkeypatch, handler)
    fr = extract_text("https://x", raw_text="already have it")
    assert fr.text == "already have it" and fr.extractor == "jina_reader"


def test_extract_trafilatura_path(monkeypatch):
    monkeypatch.setattr(extract_mod, "read_secret_env", lambda name: None)  # no jina key
    monkeypatch.setattr(extract_mod, "_websearch_cfg", lambda: {})
    def handler(req):
        return httpx.Response(200, text="<html><body><p>Hello world body</p></body></html>")
    _mock_httpx(monkeypatch, handler)
    import trafilatura
    monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: "EXTRACTED TEXT")
    fr = extract_text("https://example.com/page")
    assert fr.text == "EXTRACTED TEXT" and fr.extractor == "trafilatura"


def test_extract_jina_reader_path(monkeypatch):
    monkeypatch.setattr(extract_mod, "read_secret_env", lambda name: "jina-key")
    monkeypatch.setattr(extract_mod, "_websearch_cfg", lambda: {"jina": {}})
    def handler(req):
        assert "r.jina.ai" in str(req.url)
        assert req.headers.get("Authorization") == "Bearer jina-key"
        return httpx.Response(200, text="READER MARKDOWN")
    _mock_httpx(monkeypatch, handler)
    fr = extract_text("https://example.com/page")
    assert fr.text == "READER MARKDOWN" and fr.extractor == "jina_reader"


def test_extract_failure_degrades_to_empty(monkeypatch):
    monkeypatch.setattr(extract_mod, "read_secret_env", lambda name: None)
    monkeypatch.setattr(extract_mod, "_websearch_cfg", lambda: {})
    def handler(req):
        return httpx.Response(404, text="nope")
    _mock_httpx(monkeypatch, handler)
    fr = extract_text("https://dead.link")
    assert fr.text == "" and fr.extractor == "none"


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------


def test_cards_basic_mapping():
    hits = [SearchHit(title="T", url="https://www.example.com/a", snippet="snip",
                      provider="ddgs", published="2026-01-01")]
    cards = to_evidence_cards(hits, matched_terms=["x"], why="because")
    c = cards[0]
    assert isinstance(c, EvidenceCard)
    assert c.source == "example.com"  # www stripped
    assert c.matched_terms == ["x"] and c.why_retrieved == "because"
    assert c.published == "2026-01-01"


def test_cards_snippet_falls_back_to_raw_text_and_truncates():
    long_text = "word " * 400  # 2000 chars
    hit = SearchHit(title="T", url="https://x", snippet="", provider="jina", raw_text=long_text)
    card = to_evidence_cards([hit])[0]
    assert card.snippet  # not empty (fell back to raw_text)
    assert len(card.snippet) <= 520  # truncated near _SNIPPET_MAX
    assert card.snippet.endswith("…")


def test_cards_watch_label_in_why():
    hit = SearchHit(title="T", url="https://x", snippet="s", provider="ddgs")
    card = to_evidence_cards([hit], watch_label="NVDA")[0]
    assert "NVDA" in card.why_retrieved


def test_cards_source_fallback_to_provider_on_bad_url():
    hit = SearchHit(title="T", url="", snippet="s", provider="ddgs")
    assert to_evidence_cards([hit])[0].source == "ddgs"


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, structured=None, error=None):
        self.structured_output = structured
        self.error = error
        self.error_kind = None

    def is_error(self):
        return self.error is not None


def _patch_call(monkeypatch, resp, captured):
    import work_buddy.llm.runner_v2 as rv2
    def fake_call(self, **kwargs):
        captured.update(kwargs)
        return resp
    monkeypatch.setattr(rv2.LLMRunner, "call", fake_call)


def _cards():
    return [EvidenceCard(title="T", source="x.com", url="https://x", snippet="evidence")]


def test_classify_empty_cards_no_llm_call(monkeypatch):
    # If it tried to call the LLM this would explode (no patch) — assert it doesn't.
    import work_buddy.llm.runner_v2 as rv2
    def boom(self, **kw):  # pragma: no cover
        raise AssertionError("should not call LLM for empty cards")
    monkeypatch.setattr(rv2.LLMRunner, "call", boom)
    r = classify_evidence("q", [])
    assert r.relevant is False and r.confidence == 0.0


def test_classify_parses_verdict_and_passes_background(monkeypatch):
    captured = {}
    resp = _FakeResp(structured={"relevant": True, "confidence": 0.82, "reason": "matches",
                                 "evidence_urls": ["https://x"]})
    _patch_call(monkeypatch, resp, captured)
    r = classify_evidence("Is X true?", _cards())
    assert r.relevant is True and abs(r.confidence - 0.82) < 1e-9
    assert r.evidence_urls == ["https://x"]
    # The corrected design: BACKGROUND priority threaded straight to the call.
    assert captured["priority"] == Priority.BACKGROUND
    assert captured["tier"].value == "local_fast"
    assert captured["output_schema"]["required"] == ["relevant", "confidence", "reason"]


def test_classify_error_response_defaults_false(monkeypatch):
    captured = {}
    _patch_call(monkeypatch, _FakeResp(error="backend down"), captured)
    r = classify_evidence("q", _cards())
    assert r.relevant is False and "error" in r.reason


def test_classify_call_raises_defaults_false(monkeypatch):
    import work_buddy.llm.runner_v2 as rv2
    monkeypatch.setattr(rv2.LLMRunner, "call",
                        lambda self, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    r = classify_evidence("q", _cards())
    assert r.relevant is False and "failed" in r.reason
