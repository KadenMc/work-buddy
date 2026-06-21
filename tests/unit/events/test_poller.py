"""Unit tests for the reconciling poller (producer side)."""

from __future__ import annotations

from work_buddy.events.sources import poller as P
from work_buddy.events.sources.definition import from_frontmatter

BASE_FM = {
    "kind": "event_source",
    "source": {"type": "fake", "url": "x", "interval": "5m"},
    "extract": {"mode": "json_path", "path": "$.ceo"},
    "action": {"name": "notify"},
    "allowed_actions": ["notify"],
    "enabled": True,
}


def _src(payload, **overrides):
    fm = {**BASE_FM, "fake_payload": payload, **overrides}
    return from_frontmatter("nvda", fm)


def test_first_poll_is_baseline_no_emit(tmp_path):
    published = []
    r = P.poll_source(_src({"ceo": "A"}), publish=published.append, state_directory=tmp_path)
    assert r["is_first"] is True
    assert r["emitted"] is False
    assert published == []


def test_unchanged_then_changed(tmp_path):
    published = []
    pub = published.append
    P.poll_source(_src({"ceo": "A"}), publish=pub, state_directory=tmp_path)         # baseline
    r2 = P.poll_source(_src({"ceo": "A"}), publish=pub, state_directory=tmp_path)     # no change
    assert r2["changed"] is False
    assert published == []
    r3 = P.poll_source(_src({"ceo": "B"}), publish=pub, state_directory=tmp_path)     # change
    assert r3["changed"] is True and r3["emitted"] is True
    assert len(published) == 1
    evt = published[0]
    assert evt.type == "ai.workbuddy.source.nvda.changed"
    assert evt.data["current"] == "B" and evt.data["prev"] == "A"
    assert evt.source == "/wb/source/nvda" and evt.modality == "pull"


def test_dry_run_has_zero_side_effects(tmp_path):
    published = []
    P.poll_source(_src({"ceo": "A"}), publish=published.append, state_directory=tmp_path)  # baseline
    r = P.dry_run(_src({"ceo": "B"}), publish=published.append, state_directory=tmp_path)
    assert r["changed"] is True
    assert "would_emit" in r and r["emitted"] is False
    assert published == []  # nothing published
    # State was NOT advanced — a real poll with B still registers the change.
    r2 = P.poll_source(_src({"ceo": "B"}), publish=published.append, state_directory=tmp_path)
    assert r2["changed"] is True and r2["emitted"] is True


def test_cursor_from_all_fires_on_first(tmp_path):
    published = []
    r = P.poll_source(
        _src({"ceo": "A"}, cursor={"from": "all"}),
        publish=published.append,
        state_directory=tmp_path,
    )
    assert r["is_first"] is True and r["emitted"] is True


def test_fetch_failure_is_non_fatal(tmp_path):
    def boom(_src):
        raise RuntimeError("network down")

    r = P.poll_source(_src({"ceo": "A"}), fetch=boom, publish=lambda e: None, state_directory=tmp_path)
    assert r["emitted"] is False and "error" in r
