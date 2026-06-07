"""Unit tests for the native vault context collector.

The collector queries the native vault index per active contract. Both
dependencies (``active_contracts`` and ``vault_index.search.search``) are
patched so the test needs no real contracts dir or built index.
"""
from __future__ import annotations

from pathlib import Path

import work_buddy.contracts as contracts_mod
import work_buddy.vault_index.search as search_mod
from work_buddy.collectors import vault_collector


def _patch(monkeypatch, contracts, results):
    monkeypatch.setattr(contracts_mod, "active_contracts", lambda *_a, **_k: contracts)
    monkeypatch.setattr(contracts_mod, "get_contracts_dir", lambda *_a, **_k: Path("/x"))

    def _search(query, **_kw):
        return results(query) if callable(results) else results

    monkeypatch.setattr(search_mod, "search", _search)


def _contract(title=None, claim=None, path="c.md"):
    c = {"path": Path(path), "sections": {}}
    if title is not None:
        c["title"] = title
    if claim is not None:
        c["sections"]["Claim"] = claim
    return c


def test_renders_contract_hits(monkeypatch):
    _patch(
        monkeypatch,
        [_contract(title="ECG Foundation Models", claim="A literature review of foundation models for ECG")],
        [
            {"doc_id": "d1", "score": 0.912, "metadata": {"source_path": "vault/notes/ecg-review.md"}},
            {"doc_id": "d2", "score": 0.501, "metadata": {"source_path": "vault/notes/other.md"}},
        ],
    )
    out = vault_collector.collect({})
    assert "## Contract-Relevant Vault Content" in out
    assert "### ECG Foundation Models" in out
    assert "`vault/notes/ecg-review.md` (0.912)" in out
    assert "`vault/notes/other.md` (0.501)" in out
    assert "literature review of foundation models" in out  # query echoed


def test_empty_when_no_active_contracts(monkeypatch):
    _patch(monkeypatch, [], [{"doc_id": "d", "score": 1.0, "metadata": {}}])
    assert vault_collector.collect({}) == ""


def test_empty_when_no_search_results(monkeypatch):
    _patch(monkeypatch, [_contract(title="Some Contract Title")], [])
    assert vault_collector.collect({}) == ""


def test_falls_back_to_title_when_no_claim(monkeypatch):
    seen = {}

    def _results(query):
        seen["q"] = query
        return [{"doc_id": "d", "score": 0.7, "metadata": {"source_path": "vault/a.md"}}]

    _patch(monkeypatch, [_contract(title="Cardiac Signal Model")], _results)
    out = vault_collector.collect({})
    assert seen["q"] == "Cardiac Signal Model"  # title used as query
    assert "### Cardiac Signal Model" in out


def test_skips_too_short_query(monkeypatch):
    # title shorter than the min-query threshold and no claim → skipped
    _patch(monkeypatch, [_contract(title="ab")], [{"doc_id": "d", "score": 1.0, "metadata": {}}])
    assert vault_collector.collect({}) == ""


def test_caps_at_three_contracts(monkeypatch):
    calls = {"n": 0}

    def _results(query):
        calls["n"] += 1
        return [{"doc_id": "d", "score": 0.6, "metadata": {"source_path": "vault/a.md"}}]

    five = [_contract(title=f"Contract Number {i}") for i in range(5)]
    _patch(monkeypatch, five, _results)
    vault_collector.collect({})
    assert calls["n"] == 3  # only first 3 active contracts queried


def test_falls_back_to_doc_id_without_source_path(monkeypatch):
    _patch(
        monkeypatch,
        [_contract(title="Contract Title Here")],
        [{"doc_id": "deadbeef", "score": 0.8, "metadata": {}}],
    )
    out = vault_collector.collect({})
    assert "`deadbeef` (0.800)" in out
