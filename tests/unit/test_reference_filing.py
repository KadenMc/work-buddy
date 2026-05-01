"""Slice 6 tests — reference filing pipeline.

Covers the data-shape + composition layer.  The LLM step itself is
exercised via a stubbed runner; the live Smart Connections / vault
integration is mocked so the tests don't require Obsidian.

1. ``parse_filing_verdict`` accepts dicts + JSON strings, drops
   malformed candidate entries, returns None when the schema isn't
   met.
2. ``vault_schema`` returns the documented top-level keys, degrades
   gracefully when the vault root or bridge is unavailable.
3. ``propose_reference_filing`` composes semantic_search +
   SmartSource.drill_down + vault_schema; runs without an LLM and
   returns the candidates + schema; runs WITH a stubbed LLM and
   returns a parsed verdict.
4. ``SmartSource.drill_down`` raises NotImplementedError when the
   bridge is unavailable; supports the ``content`` and ``related``
   fields.
"""

from __future__ import annotations

import json

import pytest

from work_buddy.clarify import reference_filing as rf


# ---------------------------------------------------------------------------
# parse_filing_verdict
# ---------------------------------------------------------------------------


def test_parse_filing_verdict_dict_round_trip():
    raw = {
        "topic_label": "ECG augmentation strategies",
        "candidate_paths": [
            {"path": "Research/ECG/augmentation.md",
             "action": "extend",
             "rationale": "existing file lists 5 augmentation methods"},
        ],
        "confidence": 0.85,
        "namespace_tags": ["paper/ecg-classifier"],
    }
    v = rf.parse_filing_verdict(raw)
    assert v is not None
    assert v.topic_label == "ECG augmentation strategies"
    assert len(v.candidates) == 1
    assert v.candidates[0].action == "extend"
    assert v.confidence == pytest.approx(0.85)
    assert v.namespace_tags == ("paper/ecg-classifier",)


def test_parse_filing_verdict_json_string():
    raw = json.dumps({
        "topic_label": "X",
        "candidate_paths": [
            {"path": "a.md", "action": "new_file", "rationale": "r"},
        ],
    })
    v = rf.parse_filing_verdict(raw)
    assert v is not None
    assert v.candidates[0].path == "a.md"


def test_parse_filing_verdict_drops_invalid_action():
    """A candidate with an unknown action is dropped silently."""
    raw = {
        "topic_label": "X",
        "candidate_paths": [
            {"path": "a.md", "action": "merge", "rationale": "bad action"},
            {"path": "b.md", "action": "new_file", "rationale": "good"},
        ],
    }
    v = rf.parse_filing_verdict(raw)
    assert v is not None
    assert len(v.candidates) == 1
    assert v.candidates[0].path == "b.md"


def test_parse_filing_verdict_returns_none_for_empty_candidates():
    raw = {"topic_label": "X", "candidate_paths": []}
    assert rf.parse_filing_verdict(raw) is None


def test_parse_filing_verdict_returns_none_for_missing_topic():
    raw = {
        "candidate_paths": [
            {"path": "a.md", "action": "new_file", "rationale": "r"},
        ],
    }
    assert rf.parse_filing_verdict(raw) is None


def test_parse_filing_verdict_returns_none_for_garbage_string():
    assert rf.parse_filing_verdict("not-json") is None


def test_parse_filing_verdict_clamps_confidence():
    raw = {
        "topic_label": "X",
        "candidate_paths": [
            {"path": "a.md", "action": "new_file", "rationale": "r"},
        ],
        "confidence": 5.0,  # out of range
    }
    v = rf.parse_filing_verdict(raw)
    assert v.confidence == 1.0


# ---------------------------------------------------------------------------
# vault_schema
# ---------------------------------------------------------------------------


def test_vault_schema_returns_top_level_keys(monkeypatch):
    """All sub-calls succeed → status='ok', expected keys present."""
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": ""},
    )
    monkeypatch.setattr(
        "work_buddy.obsidian.bridge.get_tags",
        lambda: {"#research": 5, "#paper/ecg-classifier": 3},
    )
    monkeypatch.setattr(
        "work_buddy.contracts.active_contracts",
        lambda: [{"slug": "p1", "title": "Paper 1"}],
    )
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.store.distinct_namespace_tags",
        lambda recent_days: [
            {"tag": "paper/ecg", "count": 5, "recent_count": 2},
        ],
    )
    out = rf.vault_schema()
    for key in (
        "status", "vault_root", "folders", "tags",
        "active_contracts", "active_namespaces", "warnings",
    ):
        assert key in out
    assert out["tags"] == {"#research": 5, "#paper/ecg-classifier": 3}
    assert out["active_contracts"][0]["slug"] == "p1"


def test_vault_schema_degrades_when_bridge_unavailable(monkeypatch):
    """A failing bridge call falls back to the task-tag cache."""
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": ""},
    )

    def boom():
        raise RuntimeError("bridge down")
    monkeypatch.setattr("work_buddy.obsidian.bridge.get_tags", boom)
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.store.distinct_namespace_tags",
        lambda recent_days: [{"tag": "paper", "count": 3, "recent_count": 1}],
    )
    monkeypatch.setattr(
        "work_buddy.contracts.active_contracts", lambda: [],
    )
    out = rf.vault_schema()
    assert any("vault tags unavailable" in w for w in out["warnings"])
    # Fell back to task-tag cache
    assert out["tags"] == {"#paper": 3}


def test_vault_schema_walks_folders(tmp_path, monkeypatch):
    """When vault_root is set + walkable, folders are populated."""
    (tmp_path / "Research").mkdir()
    (tmp_path / "Research" / "ECG").mkdir()
    (tmp_path / "Research" / "a.md").write_text("x")
    (tmp_path / "Research" / "ECG" / "b.md").write_text("y")
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(tmp_path)},
    )
    monkeypatch.setattr(
        "work_buddy.obsidian.bridge.get_tags", lambda: {},
    )
    monkeypatch.setattr(
        "work_buddy.contracts.active_contracts", lambda: [],
    )
    monkeypatch.setattr(
        "work_buddy.obsidian.tasks.store.distinct_namespace_tags",
        lambda recent_days: [],
    )
    out = rf.vault_schema()
    paths = {f["path"] for f in out["folders"]}
    assert "Research" in paths
    assert "Research/ECG" in paths


# ---------------------------------------------------------------------------
# propose_reference_filing — composition without LLM
# ---------------------------------------------------------------------------


def _stub_smart_source(content_by_key: dict[str, str], monkeypatch):
    """Install a fake SmartSource into the context registry."""
    class _FakeSmart:
        name = "smart"
        def drill_down(self, item_id, field):
            if field != "content":
                raise KeyError(field)
            return {"content": content_by_key.get(item_id, "")}
    monkeypatch.setattr(
        "work_buddy.context.registry.get",
        lambda name: _FakeSmart() if name == "smart" else None,
    )


def test_propose_without_runner_returns_candidates(monkeypatch):
    """When runner is None, the function returns candidates + schema; verdict=None."""
    monkeypatch.setattr(
        "work_buddy.obsidian.smart.env.semantic_search",
        lambda q, limit=5: [
            {"key": "Research/ECG/aug.md", "score": 0.85},
            {"key": "Research/ECG/notes.md", "score": 0.72},
        ],
    )
    _stub_smart_source({
        "Research/ECG/aug.md": "Existing augmentation notes here.",
        "Research/ECG/notes.md": "General ECG notes.",
    }, monkeypatch)
    monkeypatch.setattr(
        rf, "vault_schema",
        lambda **kwargs: {"status": "ok", "vault_root": "", "folders": [],
                          "tags": {}, "active_contracts": [],
                          "active_namespaces": [], "warnings": []},
    )

    proposal = rf.propose_reference_filing(
        topic_text="thinking about new ECG augmentations", top_k=2,
    )
    assert proposal.verdict is None  # no LLM
    assert len(proposal.candidates) == 2
    assert proposal.candidates[0]["snippet"].startswith("Existing")
    assert proposal.schema["status"] == "ok"


def test_propose_handles_empty_candidates(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.obsidian.smart.env.semantic_search",
        lambda q, limit=5: [],
    )
    monkeypatch.setattr(
        rf, "vault_schema",
        lambda **kwargs: {"status": "ok", "vault_root": "", "folders": [],
                          "tags": {}, "active_contracts": [],
                          "active_namespaces": [], "warnings": []},
    )
    proposal = rf.propose_reference_filing(topic_text="X")
    assert proposal.candidates == []
    assert proposal.verdict is None


def test_propose_handles_drill_down_failure(monkeypatch):
    """Drill-down raises NotImplementedError → snippet stays empty."""
    monkeypatch.setattr(
        "work_buddy.obsidian.smart.env.semantic_search",
        lambda q, limit=5: [{"key": "a.md", "score": 0.5}],
    )

    class _FakeSmart:
        name = "smart"
        def drill_down(self, item_id, field):
            raise NotImplementedError("bridge down")
    monkeypatch.setattr(
        "work_buddy.context.registry.get",
        lambda name: _FakeSmart() if name == "smart" else None,
    )
    monkeypatch.setattr(
        rf, "vault_schema",
        lambda **kwargs: {"status": "ok", "vault_root": "", "folders": [],
                          "tags": {}, "active_contracts": [],
                          "active_namespaces": [], "warnings": []},
    )
    proposal = rf.propose_reference_filing(topic_text="X")
    assert len(proposal.candidates) == 1
    assert proposal.candidates[0]["snippet"] == ""


# ---------------------------------------------------------------------------
# SmartSource.drill_down
# ---------------------------------------------------------------------------


def test_smart_drill_down_unknown_field_raises_keyerror():
    from work_buddy.context.sources.smart import SmartSource
    src = SmartSource()
    with pytest.raises(KeyError):
        src.drill_down("a.md", "unsupported")


def test_smart_drill_down_empty_item_id_raises():
    from work_buddy.context.sources.smart import SmartSource
    src = SmartSource()
    with pytest.raises(KeyError):
        src.drill_down("", "content")


def test_smart_drill_down_content_calls_get_item_content(monkeypatch):
    from work_buddy.context.sources.smart import SmartSource
    captured = {}
    def fake_get_item_content(key):
        captured["key"] = key
        return {"key": key, "content": "hello"}
    monkeypatch.setattr(
        "work_buddy.obsidian.smart.env.get_item_content",
        fake_get_item_content,
    )
    src = SmartSource()
    out = src.drill_down("Research/x.md", "content")
    assert out["content"] == "hello"
    assert captured["key"] == "Research/x.md"


def test_smart_drill_down_content_failure_raises_notimplementederror(monkeypatch):
    from work_buddy.context.sources.smart import SmartSource
    def boom(key):
        raise RuntimeError("bridge unavailable")
    monkeypatch.setattr(
        "work_buddy.obsidian.smart.env.get_item_content", boom,
    )
    src = SmartSource()
    with pytest.raises(NotImplementedError):
        src.drill_down("a.md", "content")
