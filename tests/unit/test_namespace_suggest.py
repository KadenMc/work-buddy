"""Unit tests for task_namespace_suggest (Phase 4c)."""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks import namespace_suggest as ns_mod
from work_buddy.obsidian.tasks.namespace_suggest import (
    _score_tag,
    _tokens,
    task_namespace_suggest,
)


@pytest.fixture(autouse=True)
def _force_tokens_path(monkeypatch):
    """Keep these legacy tests on the deterministic token scorer, regardless
    of whether the dev box happens to be running the embedding service."""
    monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: False)


@pytest.fixture
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "tasks.sqlite")


def _seed(ids_with_tags):
    for tid, tags in ids_with_tags.items():
        store.create(task_id=tid, state="inbox")
        store.set_task_tags(tid, tags)


def test_tokens_dedupe_and_lowercase():
    assert _tokens("ECG Classifier — ECG paper") == {"ecg", "classifier", "paper"}


def test_tokens_ignore_stopwords_and_short():
    assert "a" not in _tokens("a plan for the paper")
    assert "the" not in _tokens("the paper")


def test_score_tag_matches_deepest_segment_more_heavily():
    qt = {"ecg"}
    shallow = _score_tag("ecg", qt)
    nested = _score_tag("paper/ecg", qt)
    assert nested > shallow


def test_empty_universe_returns_empty(_isolated_store):
    result = task_namespace_suggest("draft outline")
    assert result["suggestions"] == []
    assert result["universe_size"] == 0
    assert result["service_used"] == "none"


def test_text_matches_tag_segment(_isolated_store):
    _seed({
        "t-a": [("paper/ecg-classifier", True)],
        "t-b": [("paper/ecg-classifier", True)],
        "t-c": [("admin/taxes", True)],
        "t-d": [("admin/taxes", True)],
    })
    result = task_namespace_suggest("prepare ECG augmentation experiment")
    tags = [s["tag"] for s in result["suggestions"]]
    assert "paper/ecg-classifier" in tags
    # admin/taxes shouldn't win against an ECG query.
    assert "admin/taxes" not in tags[:1]


def test_contract_boosts_related_namespace(_isolated_store):
    _seed({
        "t-a": [("paper/ecg-classifier", True)],
        "t-b": [("paper/ecg-classifier", True)],
        "t-c": [("paper/other", True)],
        "t-d": [("paper/other", True)],
    })
    # Text is generic; contract slug points at ecg-classifier.
    result = task_namespace_suggest("draft section", contract="ecg-classifier")
    top = result["suggestions"][0]["tag"]
    assert top == "paper/ecg-classifier"


def test_limit_respected(_isolated_store):
    _seed({
        "t-a": [("paper/ecg", True)],
        "t-b": [("paper/ecg", True)],
        "t-c": [("paper/other", True)],
        "t-d": [("paper/other", True)],
        "t-e": [("paper/third", True)],
        "t-f": [("paper/third", True)],
    })
    result = task_namespace_suggest("paper draft", limit=2)
    assert len(result["suggestions"]) <= 2


def test_empty_query_returns_no_suggestions(_isolated_store):
    _seed({"t-a": [("paper/ecg", True)], "t-b": [("paper/ecg", True)]})
    # Query is all stopwords + short tokens; no query tokens → no suggestions.
    result = task_namespace_suggest("a of the")
    assert result["suggestions"] == []
    assert result["universe_size"] == 1
