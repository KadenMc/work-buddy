"""Unit tests for `DurableSummaryStore` and `TtlCacheStore`."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from work_buddy.summarization import (
    Provenance,
    SummaryNode,
)
from work_buddy.summarization.stores import (
    DurableSummaryStore,
    TtlCacheStore,
)


# ---------------------------------------------------------------------------
# DurableSummaryStore — tree round-trip + staleness + namespacing
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Point both `_default_db_path` and the config-resolution path at a temp
    DB file so the store auto-discovers it via `get_connection()`."""
    from work_buddy.summarization import db as db_mod

    db_file = tmp_path / "summarization-test.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)
    return db_file


def _prov(prompt_v: int = 1, schema_v: int = 1) -> Provenance:
    return Provenance(
        model="m", backend="b", profile="p",
        generated_at=Provenance.now_iso(),
        prompt_version=prompt_v,
        summary_schema_version=schema_v,
        selection_version=1, cache_version=1,
    )


def test_durable_round_trip_depth2_tree(tmp_db):
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)

    root = SummaryNode(
        summary="root summary",
        source_ref=None,
        children=[
            SummaryNode(
                summary="child 1",
                source_ref={"span_start": 0, "span_end": 5},
                extra={"title": "T1", "keywords": ["k1"]},
            ),
            SummaryNode(
                summary="child 2",
                source_ref={"span_start": 5, "span_end": 9},
                extra={"title": "T2", "keywords": ["k2", "k3"]},
            ),
        ],
        extra={"meta": "v"},
    )

    store.save("item-1", root, _prov(), "tok-1")
    loaded = store.load("item-1")

    assert loaded is not None
    assert loaded.summary == "root summary"
    assert loaded.extra == {"meta": "v"}
    assert len(loaded.children) == 2
    assert loaded.children[0].summary == "child 1"
    assert loaded.children[0].source_ref == {"span_start": 0, "span_end": 5}
    assert loaded.children[0].extra["title"] == "T1"
    assert loaded.children[1].summary == "child 2"
    assert loaded.children[1].extra["keywords"] == ["k2", "k3"]


def test_durable_select_stale_missing_item(tmp_db):
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    candidates = [("alpha", "1"), ("beta", "1")]

    # Nothing saved yet — both are stale.
    assert store.select_stale(candidates) == candidates

    store.save("alpha", SummaryNode(summary="a"), _prov(), "1")
    stale = store.select_stale(candidates)
    assert stale == [("beta", "1")]


def test_durable_select_stale_token_changed(tmp_db):
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    store.save("alpha", SummaryNode(summary="a"), _prov(), "tok-old")

    # Same id, different token → stale.
    assert store.select_stale([("alpha", "tok-new")]) == [("alpha", "tok-new")]
    # Same token → fresh.
    assert store.select_stale([("alpha", "tok-old")]) == []


def test_durable_select_stale_version_bumped(tmp_db):
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    store.save("alpha", SummaryNode(summary="a"), _prov(prompt_v=1), "1")

    # Bump the strategy's prompt_version → stored row's prompt_version (1)
    # mismatches → stale.
    store.set_strategy_versions(2, 1)
    assert store.select_stale([("alpha", "1")]) == [("alpha", "1")]


def test_durable_is_fresh_matches_select_stale(tmp_db):
    """The two staleness APIs must agree (shared predicate)."""
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)

    store.save("a", SummaryNode(summary="x"), _prov(), "tok-1")

    for token in ["tok-1", "tok-2", ""]:
        is_fresh = store.is_fresh("a", token)
        is_in_stale = ("a", token) in store.select_stale([("a", token)])
        # is_fresh = True iff NOT in stale list.
        assert is_fresh == (not is_in_stale), (
            f"is_fresh/select_stale disagree for token={token!r}"
        )


def test_durable_namespaces_dont_collide(tmp_db):
    store_a = DurableSummaryStore("ns_a")
    store_a.set_strategy_versions(1, 1)
    store_b = DurableSummaryStore("ns_b")
    store_b.set_strategy_versions(1, 1)

    store_a.save("same-id", SummaryNode(summary="from A"), _prov(), "1")
    store_b.save("same-id", SummaryNode(summary="from B"), _prov(), "1")

    assert store_a.load("same-id").summary == "from A"
    assert store_b.load("same-id").summary == "from B"


def test_durable_record_error_preserves_prior_good_nodes(tmp_db):
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    good = SummaryNode(
        summary="good root",
        children=[SummaryNode(summary="good child")],
    )
    store.save("item-1", good, _prov(), "tok-1")

    # Now record an error. Prior nodes should remain loadable; load() should
    # still return the prior tree.
    store.record_error("item-1", "llm went boom", _prov())
    loaded = store.load("item-1")
    assert loaded is not None
    assert loaded.summary == "good root"
    assert len(loaded.children) == 1


def test_durable_record_error_no_prior_inserts_status_only(tmp_db):
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    store.record_error("item-1", "boom", _prov())
    # No nodes were ever saved → load returns None.
    assert store.load("item-1") is None
    # But the item's meta row exists with status='error'.
    meta = store.load_item_meta("item-1")
    assert meta is not None
    assert meta["status"] == "error"
    assert meta["error"] == "boom"


def test_durable_overwrite_existing_replaces_children(tmp_db):
    store = DurableSummaryStore("ns_a")
    store.set_strategy_versions(1, 1)
    store.save(
        "item-1",
        SummaryNode(summary="v1", children=[
            SummaryNode(summary="old child 1"),
            SummaryNode(summary="old child 2"),
        ]),
        _prov(), "1",
    )
    store.save(
        "item-1",
        SummaryNode(summary="v2", children=[SummaryNode(summary="new child")]),
        _prov(), "2",
    )

    loaded = store.load("item-1")
    assert loaded.summary == "v2"
    assert len(loaded.children) == 1
    assert loaded.children[0].summary == "new child"


# ---------------------------------------------------------------------------
# TtlCacheStore — wraps llm.cache
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_llm_cache(monkeypatch, tmp_path):
    """Point `llm.cache._CACHE_PATH` at a temp file."""
    from work_buddy.llm import cache as cache_mod
    cache_file = tmp_path / "llm_cache.json"
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)
    return cache_file


def test_ttl_round_trip_flat_summary(tmp_llm_cache):
    store = TtlCacheStore(
        "chrome_page", strategy_version_tag="chrome_page:v1",
        ttl_minutes=30,
    )

    content = "the page content"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    token = {"hash": content_hash, "text": content}

    node = SummaryNode(
        summary="A page about X",
        extra={"entities": [{"name": "X", "type": "concept"}],
               "key_claims": ["X is Y"]},
    )

    assert store.is_fresh("http://example.com/p", token) is False
    store.save("http://example.com/p", node, _prov(), token)
    assert store.is_fresh("http://example.com/p", token) is True

    loaded = store.load("http://example.com/p")
    assert loaded is not None
    assert loaded.summary == "A page about X"
    assert loaded.extra["entities"][0]["name"] == "X"


def test_ttl_content_hash_change_invalidates(tmp_llm_cache):
    store = TtlCacheStore(
        "chrome_page", strategy_version_tag="chrome_page:v1",
    )

    old_content = "old"
    new_content = "new"
    old_token = {
        "hash": hashlib.sha256(old_content.encode()).hexdigest(),
        "text": old_content,
    }
    new_token = {
        "hash": hashlib.sha256(new_content.encode()).hexdigest(),
        "text": new_content,
    }

    store.save("k", SummaryNode(summary="s"), _prov(), old_token)
    # Same key, different content → cache miss (via cache.get's hash check).
    assert store.is_fresh("k", new_token) is False


def test_ttl_strategy_version_bump_invalidates(tmp_llm_cache):
    content = "x"
    token = {
        "hash": hashlib.sha256(content.encode()).hexdigest(),
        "text": content,
    }

    s1 = TtlCacheStore(
        "chrome_page", strategy_version_tag="chrome_page:v1",
    )
    s1.save("k", SummaryNode(summary="s"), _prov(), token)
    assert s1.is_fresh("k", token) is True

    # Different version tag → different system_hash → cache miss.
    s2 = TtlCacheStore(
        "chrome_page", strategy_version_tag="chrome_page:v2",
    )
    assert s2.is_fresh("k", token) is False


def test_ttl_select_stale_consistent_with_is_fresh(tmp_llm_cache):
    store = TtlCacheStore(
        "chrome_page", strategy_version_tag="chrome_page:v1",
    )
    content = "c"
    token = {
        "hash": hashlib.sha256(content.encode()).hexdigest(),
        "text": content,
    }
    store.save("a", SummaryNode(summary="s"), _prov(), token)

    # 'a' is fresh; 'b' is missing.
    stale = store.select_stale([("a", token), ("b", token)])
    assert stale == [("b", token)]
