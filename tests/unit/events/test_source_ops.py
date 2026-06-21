"""Unit tests for the source authoring helpers + ops (create / dry-run)."""

from __future__ import annotations

import work_buddy.events.sources.loader as loader_mod
from work_buddy.events.sources.definition import build_source_fm, from_frontmatter, validate_source_fm
from work_buddy.events.sources.loader import load_event_sources, write_event_source


def test_build_source_fm_validates():
    fm = build_source_fm(
        source_type="fake",
        interval="6h",
        extract_mode="json_path",
        extract_path="$.ceo",
        condition="event.data != prev.data",
        action="notify",
        allowed_actions=["notify"],
    )
    assert validate_source_fm("nvda", fm) == []


def test_write_event_source_roundtrips(tmp_path):
    fm = build_source_fm(
        source_type="http_poll",
        url="https://example.test/quote.json",
        interval="6h",
        extract_mode="json_path",
        extract_path="$.price",
    )
    res = write_event_source(tmp_path, "nvda", fm)
    assert res["success"] is True

    defs, errors = load_event_sources(tmp_path)
    assert errors == []
    assert [d.name for d in defs] == ["nvda"]
    assert defs[0].extract_path == "$.price"
    assert defs[0].url == "https://example.test/quote.json"


def test_write_refuses_overwrite(tmp_path):
    fm = build_source_fm(source_type="fake", interval="5m", extract_mode="hash")
    assert write_event_source(tmp_path, "s", fm)["success"] is True
    again = write_event_source(tmp_path, "s", fm)
    assert again["success"] is False and "exists" in again["error"]
    assert write_event_source(tmp_path, "s", fm, overwrite=True)["success"] is True


def test_write_rejects_unknown_type(tmp_path):
    res = write_event_source(tmp_path, "s", build_source_fm(source_type="bogus", interval="6h"))
    assert res["success"] is False
    assert any("unknown" in e for e in res["errors"])


def test_write_rejects_bad_condition(tmp_path):
    fm = build_source_fm(source_type="fake", interval="5m", extract_mode="hash", condition="a +")
    res = write_event_source(tmp_path, "s", fm)
    assert res["success"] is False
    assert any("CEL" in e for e in res["errors"])


# --- event_source_dry_run op (condition eval over the would-emit event) -------

def _dry_run_src():
    return from_frontmatter(
        "nvda",
        {
            "kind": "event_source",
            "source": {"type": "fake", "url": "x", "interval": "5m"},
            "extract": {"mode": "json_path", "path": "$.ceo"},
            "condition": "event.data != prev.data",
            "action": {"name": "notify"},
            "allowed_actions": ["notify"],
            "enabled": True,
        },
    )


def test_dry_run_would_fire_when_condition_passes(monkeypatch):
    import work_buddy.events.sources.poller as P
    import work_buddy.mcp_server.ops.events_ops as ops

    src = _dry_run_src()
    monkeypatch.setattr(loader_mod, "load_event_sources", lambda directory=None: ([src], []))
    monkeypatch.setattr(
        P, "dry_run",
        lambda s, **kw: {"changed": True, "is_first": False, "value": "B", "prev": "A",
                         "would_emit": {"type": s.event_type}},
    )
    out = ops.event_source_dry_run("nvda")
    assert out["ok"] is True
    assert out["condition_passed"] is True
    assert out["would_fire"] is True


def test_dry_run_no_fire_when_condition_fails(monkeypatch):
    import work_buddy.events.sources.poller as P
    import work_buddy.mcp_server.ops.events_ops as ops

    src = _dry_run_src()
    monkeypatch.setattr(loader_mod, "load_event_sources", lambda directory=None: ([src], []))
    monkeypatch.setattr(
        P, "dry_run",
        lambda s, **kw: {"changed": True, "is_first": False, "value": "A", "prev": "A",
                         "would_emit": {"type": s.event_type}},
    )
    out = ops.event_source_dry_run("nvda")
    assert out["condition_passed"] is False
    assert out["would_fire"] is False


def test_dry_run_unknown_source(monkeypatch):
    import work_buddy.mcp_server.ops.events_ops as ops

    monkeypatch.setattr(loader_mod, "load_event_sources", lambda directory=None: ([], []))
    out = ops.event_source_dry_run("nope")
    assert out["ok"] is False


def test_dry_run_from_unsaved_proposal(monkeypatch):
    import work_buddy.events.sources.poller as P
    import work_buddy.mcp_server.ops.events_ops as ops

    monkeypatch.setattr(
        P, "dry_run",
        lambda s, **kw: {"changed": True, "is_first": False, "value": "B", "prev": "A",
                         "would_emit": {"type": s.event_type}},
    )
    proposal = {
        "name": "nvda",
        "source_type": "http_poll",
        "url": "https://example.test/quote.json",
        "interval": "6h",
        "extract_mode": "json_path",
        "extract_path": "$.ceo",
        "condition": "event.data != prev.data",
        "action": "notify",
        "allowed_actions": ["notify"],
    }
    out = ops.event_source_dry_run(proposal=proposal)
    assert out["ok"] is True
    assert out["condition_passed"] is True
    assert out["would_fire"] is True


def test_dry_run_proposal_rejects_invalid():
    import work_buddy.mcp_server.ops.events_ops as ops

    out = ops.event_source_dry_run(
        proposal={"name": "s", "source_type": "bogus", "interval": "6h"}
    )
    assert out["ok"] is False
