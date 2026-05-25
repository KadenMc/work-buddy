"""Tests for the Chrome composition of the summarization framework.

Covers: `ChromeSource` discovery + batch render; `summarize_tabs` end-to-end
through the framework with a stub LLM; URL normalization fidelity; cache-hit
reuse behavior.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from work_buddy.collectors.chrome_summarizer_binding import (
    ChromeSource,
    build_chrome_summarizer,
    normalize_url_for_cache,
    summarize_tabs,
)
from work_buddy.summarization import as_caller


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_llm_cache(monkeypatch, tmp_path):
    """Point `llm.cache._CACHE_PATH` at a tmp file so the TtlCacheStore writes
    to a sandbox rather than the user's real cache."""
    from work_buddy.llm import cache as cache_mod
    cache_file = tmp_path / "llm_cache.json"
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)
    return cache_file


def _stub_batch_response(item_ids: list[str]) -> dict[str, Any]:
    return {
        "summaries": [
            {
                "item_index": i,
                "content_summary": f"summary for {iid}",
                "entities": [
                    {"name": "X", "type": "concept", "context": "appears here"},
                ],
                "key_claims": [f"{iid} is interesting"],
                "user_intent_speculation": "they wanted to know",
                "user_posture": "researching",
            }
            for i, iid in enumerate(item_ids)
        ]
    }


# ---------------------------------------------------------------------------
# URL normalization fidelity
# ---------------------------------------------------------------------------


def test_normalize_url_strips_query_for_normal_sites():
    assert normalize_url_for_cache(
        "https://example.com/foo/bar/?utm=x",
    ) == "example.com/foo/bar"


def test_normalize_url_preserves_query_for_google():
    assert normalize_url_for_cache(
        "https://www.google.com/search?q=hello",
    ) == "www.google.com/search?q=hello"


def test_normalize_url_preserves_query_for_chatgpt():
    assert normalize_url_for_cache(
        "https://chatgpt.com/c/abc?xyz=1",
    ) == "chatgpt.com/c/abc?xyz=1"


# ---------------------------------------------------------------------------
# ChromeSource discovery + render
# ---------------------------------------------------------------------------


def test_chrome_source_discover_emits_per_tab_hash_token():
    tabs = [
        {"url": "https://a.com/x", "title": "A"},
        {"url": "https://b.com/y", "title": "B"},
    ]
    tab_contents = {
        "https://a.com/x": {"text": "alpha"},
        "https://b.com/y": {"text": "beta"},
    }
    src = ChromeSource(tabs, tab_contents)
    disc = src.discover(None)

    assert sorted(iid for iid, _ in disc) == ["a.com/x", "b.com/y"]
    for item_id, token in disc:
        assert "hash" in token and "text" in token


def test_chrome_source_render_batch_builds_per_tab_prompt():
    tabs = [{"url": "https://a.com/", "title": "Title A"}]
    src = ChromeSource(tabs, {"https://a.com/": {"text": "the page"}})
    out = src.render_batch(["a.com"])
    assert out[0].startswith("Title A [a.com]")
    assert "the page" in out[0]


def test_chrome_source_render_handles_missing_item_id():
    src = ChromeSource([], {})
    assert src.render("nope") is None
    assert src.render_batch(["nope"]) == [None]


# ---------------------------------------------------------------------------
# build_chrome_summarizer construction
# ---------------------------------------------------------------------------


def test_chrome_summarizer_constructs_with_expected_caps():
    summ = build_chrome_summarizer([], {})
    caps = sorted(c.value for c in summ.capabilities)
    # Flat strategy, batched, TTL-evicted, tree-or-flat-capable store.
    assert "batched" in caps
    assert "flat" in caps
    assert "ttl_evicted" in caps


# ---------------------------------------------------------------------------
# summarize_tabs end-to-end
# ---------------------------------------------------------------------------


def test_summarize_tabs_returns_aligned_pagesummaries(tmp_llm_cache):
    tabs = [
        {"url": "https://a.com/", "title": "A"},
        {"url": "https://b.com/", "title": "B"},
    ]
    tab_contents = {
        "https://a.com/": {"text": "content A"},
        "https://b.com/": {"text": "content B"},
    }

    call_count = {"n": 0}

    def stub(*, system, user, output_schema=None, profile=None):
        call_count["n"] += 1
        # The batched user prompt contains both items, so produce summaries
        # for both at indices 0 and 1.
        return _stub_batch_response(["a.com", "b.com"])

    summaries, cached_count = summarize_tabs(
        tabs, tab_contents, llm_caller=as_caller(stub),
    )

    assert len(summaries) == 2
    assert call_count["n"] == 1   # one batched LLM call
    assert cached_count == 0
    # PageSummary shape preserved.
    assert summaries[0].content_summary
    assert summaries[0].source_label == "A [a.com]"
    assert summaries[0].user_posture == "researching"
    assert summaries[0].entities[0].name == "X"


def test_summarize_tabs_reuses_cached_entries(tmp_llm_cache):
    """First call summarizes; second call finds them in cache → 0 LLM calls."""
    tabs = [{"url": "https://a.com/", "title": "A"}]
    tab_contents = {"https://a.com/": {"text": "stable content"}}

    n_calls = {"n": 0}

    def stub(*, system, user, output_schema=None, profile=None):
        n_calls["n"] += 1
        return _stub_batch_response(["a.com"])

    # Pass 1: cold cache.
    summaries1, cached1 = summarize_tabs(
        tabs, tab_contents, llm_caller=as_caller(stub),
    )
    assert n_calls["n"] == 1
    assert cached1 == 0
    assert summaries1[0].content_summary == "summary for a.com"

    # Pass 2: same input — cache hit; no LLM call.
    summaries2, cached2 = summarize_tabs(
        tabs, tab_contents, llm_caller=as_caller(stub),
    )
    assert n_calls["n"] == 1   # unchanged
    assert cached2 == 1
    assert summaries2[0].content_summary == "summary for a.com"
    assert summaries2[0].cached is True


def test_summarize_tabs_invalidates_when_content_changes(tmp_llm_cache):
    tabs = [{"url": "https://a.com/", "title": "A"}]

    n_calls = {"n": 0}

    def stub(*, system, user, output_schema=None, profile=None):
        n_calls["n"] += 1
        return _stub_batch_response(["a.com"])

    summarize_tabs(
        tabs, {"https://a.com/": {"text": "v1"}},
        llm_caller=as_caller(stub),
    )
    summarize_tabs(
        tabs, {"https://a.com/": {"text": "v2"}},
        llm_caller=as_caller(stub),
    )
    # Content changed → cache miss → second LLM call.
    assert n_calls["n"] == 2


def test_summarize_tabs_returns_fallback_for_unsummarizable_tab(tmp_llm_cache):
    """When the LLM omits an item from the batch, the tab gets a fallback
    PageSummary so the consumer index stays aligned."""
    tabs = [
        {"url": "https://a.com/", "title": "A"},
        {"url": "https://b.com/", "title": "B"},
    ]
    tab_contents = {
        "https://a.com/": {"text": "alpha"},
        "https://b.com/": {"text": "beta"},
    }

    def stub(*, system, user, output_schema=None, profile=None):
        # Return only the first item — second is missing from response.
        return {"summaries": [_stub_batch_response(["a.com"])["summaries"][0]]}

    summaries, _cached = summarize_tabs(
        tabs, tab_contents, llm_caller=as_caller(stub),
    )

    assert len(summaries) == 2
    assert summaries[0].content_summary == "summary for a.com"
    assert summaries[1].content_summary == "Content unavailable"
    assert summaries[1].source_label == "B [b.com]"


def test_summarize_tabs_empty_selection_returns_empty(tmp_llm_cache):
    summaries, cached = summarize_tabs([], {})
    assert summaries == []
    assert cached == 0
