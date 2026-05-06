"""Tests for ``work_buddy.collectors.vault_recon_collector``.

Significance rules are tested with hand-crafted snapshot fixtures. The full
collector entry point is tested with mocked ``vault_recon`` and isolated
ledger paths.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.collectors import vault_recon_collector as vrc


def _ts(days_ago: float = 0) -> str:
    """ISO timestamp N days before now."""
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat()


def _mk_snapshot(
    days_ago: float = 0,
    type_values: dict | None = None,
    type_by_status: dict | None = None,
    tag_tree: dict | None = None,
    recent_activity_by_path: dict | None = None,
) -> dict:
    """Construct a minimal snapshot for rule testing."""
    snap: dict = {"snapshot_ts": _ts(days_ago), "pages_walked": 100}
    if type_values is not None:
        snap["frontmatter_values"] = {
            "type": {
                "values": [
                    {"value": v, "count": c} for v, c in type_values.items()
                ],
                "distinct_count": len(type_values),
                "truncated": False,
            }
        }
    if type_by_status is not None:
        snap["type_by_status"] = type_by_status
    if tag_tree is not None:
        snap["tag_tree"] = tag_tree
    if recent_activity_by_path is not None:
        snap["recent_activity_by_path"] = recent_activity_by_path
    return snap


# ── flatten_tag_tree ─────────────────────────────────────────────


def test_flatten_tag_tree_d2_with_children():
    tree = {
        "mide": {
            "_count": 10,
            "children": {
                "workflow": {"_count": 6, "children": {}},
                "system": {"_count": 4, "children": {}},
            },
        },
    }
    flat = vrc._flatten_tag_tree_d2(tree)
    assert flat == {"#mide/workflow": 6, "#mide/system": 4}


def test_flatten_tag_tree_d2_root_without_children():
    tree = {"todo": {"_count": 3, "children": {}}}
    flat = vrc._flatten_tag_tree_d2(tree)
    assert flat == {"#todo": 3}


def test_flatten_tag_tree_d2_empty():
    assert vrc._flatten_tag_tree_d2({}) == {}
    assert vrc._flatten_tag_tree_d2(None) == {}  # type: ignore[arg-type]


# ── rule_new_type ────────────────────────────────────────────────


def _empty_prior_snapshots(n: int) -> list[dict]:
    """N prior snapshots with no `type` frontmatter — used to clear the
    bootstrap gate without supplying spurious 'precedent' for type values."""
    return [
        _mk_snapshot(days_ago=n - i, type_values={})
        for i in range(n)
    ]


def test_new_type_fires_on_unprecedented_type():
    current = _mk_snapshot(type_values={"hypothesis": 5})
    fires = vrc.rule_new_type(current, _empty_prior_snapshots(3))
    assert len(fires) == 1
    assert fires[0]["rule"] == "new_type"
    assert fires[0]["focus"] == "type:hypothesis"
    assert fires[0]["evidence"]["current_count"] == 5
    assert fires[0]["evidence"]["historical_max"] == 0


def test_new_type_does_not_fire_when_precedent_exists():
    current = _mk_snapshot(type_values={"hypothesis": 5})
    prior = _empty_prior_snapshots(2) + [
        _mk_snapshot(days_ago=1, type_values={"hypothesis": 4}),
    ]
    fires = vrc.rule_new_type(current, prior)
    assert fires == []


def test_new_type_does_not_fire_below_threshold():
    current = _mk_snapshot(type_values={"draft": 4})  # < min 5
    fires = vrc.rule_new_type(current, _empty_prior_snapshots(3))
    assert fires == []


def test_new_type_does_not_fire_on_bootstrap():
    """Without enough prior history, every existing type would otherwise
    look 'new' — the rule must suppress."""
    current = _mk_snapshot(type_values={"hypothesis": 5, "experiment": 3})
    assert vrc.rule_new_type(current, []) == []
    assert vrc.rule_new_type(current, _empty_prior_snapshots(2)) == []


# ── rule_new_tag_family ──────────────────────────────────────────


def test_new_tag_family_fires_when_zero_prior_and_above_threshold():
    current = _mk_snapshot(
        days_ago=0,
        tag_tree={"mide": {"_count": 15, "children": {"new-system": {"_count": 12, "children": {}}}}},
    )
    prior = [_mk_snapshot(days_ago=8, tag_tree={})]  # >7 days back, no tag_tree
    fires = vrc.rule_new_tag_family(current, prior)
    assert len(fires) == 1
    assert fires[0]["focus"] == "#mide/new-system"


def test_new_tag_family_does_not_fire_when_prior_count_nonzero():
    current = _mk_snapshot(
        tag_tree={"mide": {"_count": 15, "children": {"workflow": {"_count": 12, "children": {}}}}},
    )
    prior = [_mk_snapshot(
        days_ago=8,
        tag_tree={"mide": {"_count": 8, "children": {"workflow": {"_count": 8, "children": {}}}}},
    )]
    fires = vrc.rule_new_tag_family(current, prior)
    assert fires == []


# ── rule_stuck_state ─────────────────────────────────────────────


def test_stuck_state_fires_on_unchanged_non_terminal_cell():
    current = _mk_snapshot(type_by_status={"hypothesis": {"PROPOSED": 3}})
    prior = [_mk_snapshot(days_ago=31, type_by_status={"hypothesis": {"PROPOSED": 3}})]
    fires = vrc.rule_stuck_state(current, prior)
    assert len(fires) == 1
    assert fires[0]["focus"] == "hypothesis:PROPOSED"


def test_stuck_state_skips_terminal_status():
    current = _mk_snapshot(type_by_status={"experiment": {"COMPLETED": 5}})
    prior = [_mk_snapshot(days_ago=31, type_by_status={"experiment": {"COMPLETED": 5}})]
    fires = vrc.rule_stuck_state(current, prior)
    assert fires == []


def test_stuck_state_skips_when_count_changed():
    current = _mk_snapshot(type_by_status={"thread": {"PROPOSED": 5}})
    prior = [_mk_snapshot(days_ago=31, type_by_status={"thread": {"PROPOSED": 3}})]
    fires = vrc.rule_stuck_state(current, prior)
    assert fires == []


# ── rule_path_activity_spike ─────────────────────────────────────


def test_path_spike_fires_above_3x_baseline():
    current = _mk_snapshot(recent_activity_by_path={"repos/electricrag": 30})
    prior = [
        _mk_snapshot(days_ago=i, recent_activity_by_path={"repos/electricrag": 5})
        for i in range(1, 8)
    ]
    fires = vrc.rule_path_activity_spike(current, prior)
    assert len(fires) == 1
    assert fires[0]["focus"] == "path:repos/electricrag"
    assert fires[0]["evidence"]["ratio"] == 6.0


def test_path_spike_fires_on_new_region_activity():
    current = _mk_snapshot(recent_activity_by_path={"repos/new-project": 10})
    prior = [
        _mk_snapshot(days_ago=i, recent_activity_by_path={"repos/electricrag": 5})
        for i in range(1, 8)
    ]
    fires = vrc.rule_path_activity_spike(current, prior)
    spike_focuses = [f["focus"] for f in fires]
    assert "path:repos/new-project" in spike_focuses


def test_path_spike_needs_minimum_history():
    current = _mk_snapshot(recent_activity_by_path={"repos/electricrag": 30})
    prior = [_mk_snapshot(days_ago=1, recent_activity_by_path={"repos/electricrag": 5})]
    fires = vrc.rule_path_activity_spike(current, prior)
    assert fires == []  # need >=3 prior snapshots


# ── rule_status_backlog_growing ──────────────────────────────────


def test_backlog_growing_fires_on_monotonic_growth():
    # 7 prior snapshots, monotonic growth, current count meaningful
    prior = [
        _mk_snapshot(days_ago=7 - i, type_by_status={"hypothesis": {"PROPOSED": i + 1}})
        for i in range(7)
    ]
    current = _mk_snapshot(type_by_status={"hypothesis": {"PROPOSED": 10}})
    fires = vrc.rule_status_backlog_growing(current, prior)
    assert len(fires) == 1
    assert fires[0]["focus"] == "hypothesis:PROPOSED"


def test_backlog_growing_does_not_fire_on_flat():
    prior = [
        _mk_snapshot(days_ago=7 - i, type_by_status={"hypothesis": {"PROPOSED": 3}})
        for i in range(7)
    ]
    current = _mk_snapshot(type_by_status={"hypothesis": {"PROPOSED": 3}})
    fires = vrc.rule_status_backlog_growing(current, prior)
    assert fires == []


def test_backlog_growing_skips_terminal():
    prior = [
        _mk_snapshot(days_ago=7 - i, type_by_status={"experiment": {"COMPLETED": i}})
        for i in range(7)
    ]
    current = _mk_snapshot(type_by_status={"experiment": {"COMPLETED": 10}})
    fires = vrc.rule_status_backlog_growing(current, prior)
    assert fires == []


# ── escalation deduplication ─────────────────────────────────────


def test_is_recent_escalation_true_within_window():
    history = [
        {"rule": "stuck_state", "focus": "h:P", "ts": _ts(days_ago=2)},
    ]
    assert vrc._is_recent_escalation("stuck_state", "h:P", history) is True


def test_is_recent_escalation_false_outside_window():
    # Default 7-day window applies when no notification_id is set
    history = [
        {"rule": "stuck_state", "focus": "h:P", "ts": _ts(days_ago=10)},
    ]
    assert vrc._is_recent_escalation("stuck_state", "h:P", history) is False


def test_is_recent_escalation_false_for_different_focus():
    history = [
        {"rule": "stuck_state", "focus": "other:P", "ts": _ts(days_ago=1)},
    ]
    assert vrc._is_recent_escalation("stuck_state", "h:P", history) is False


# ── Kind-from-choice resolution ─────────────────────────────────


def test_kind_from_choice_explicit_wins():
    assert vrc._kind_from_choice({"key": "anything", "kind": "act"}) == "act"
    assert vrc._kind_from_choice({"key": "anything", "kind": "decline"}) == "decline"
    assert vrc._kind_from_choice({"key": "anything", "kind": "defer"}) == "defer"


def test_kind_from_choice_returns_none_when_missing():
    """No `kind` field at all → None (caller falls back to legacy 7d)."""
    # Key name does NOT influence the result — heuristic was removed.
    assert vrc._kind_from_choice({"key": "morning_bundle"}) is None
    assert vrc._kind_from_choice({"key": "contract_now"}) is None
    assert vrc._kind_from_choice({"key": "dismiss"}) is None
    assert vrc._kind_from_choice({"key": "more"}) is None
    assert vrc._kind_from_choice({}) is None


def test_kind_from_choice_returns_none_for_invalid_kind():
    """Garbage `kind` value → None (caller falls back to legacy 7d)."""
    assert vrc._kind_from_choice({"key": "dismiss", "kind": "nonsense"}) is None
    assert vrc._kind_from_choice({"key": "morning_bundle", "kind": "nonsense"}) is None
    assert vrc._kind_from_choice({"kind": ""}) is None
    assert vrc._kind_from_choice({"kind": None}) is None


# ── Kind-aware suppression windows ──────────────────────────────


class _FakeNotif:
    def __init__(self, value, choices, responded_at):
        self.response = {"value": value} if value else None
        self.choices = choices
        self.responded_at = responded_at


def _patch_get_notification(monkeypatch, mapping):
    def fake_get(notification_id):
        return mapping.get(notification_id)
    monkeypatch.setattr(
        "work_buddy.notifications.store.get_notification", fake_get
    )


def test_decline_response_extends_suppression_to_90_days(monkeypatch):
    """Dismiss → user doesn't want this; suppress for 90 days from response."""
    notif = _FakeNotif(
        value="dismiss",
        choices=[
            {"key": "act_thing", "label": "Do it", "kind": "act"},
            {"key": "dismiss", "label": "No thanks", "kind": "decline"},
        ],
        responded_at=_ts(days_ago=20),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=20), "notification_id": "req_X",
    }]
    # 20 days ago + 90-day window → still suppressing (until day 70 from now)
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is True


def test_decline_response_stops_suppressing_after_90_days(monkeypatch):
    notif = _FakeNotif(
        value="dismiss",
        choices=[{"key": "dismiss", "label": "No thanks", "kind": "decline"}],
        responded_at=_ts(days_ago=95),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=95), "notification_id": "req_X",
    }]
    # 95 days ago + 90-day window → window expired, no longer suppressing
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is False


def test_act_response_suppresses_30_days(monkeypatch):
    notif = _FakeNotif(
        value="morning_bundle",
        choices=[{"key": "morning_bundle", "label": "Surface daily", "kind": "act"}],
        responded_at=_ts(days_ago=10),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=10), "notification_id": "req_X",
    }]
    # 10 days ago + 30 → still suppressing
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is True


def test_act_response_stops_after_30_days(monkeypatch):
    notif = _FakeNotif(
        value="morning_bundle",
        choices=[{"key": "morning_bundle", "label": "Surface daily", "kind": "act"}],
        responded_at=_ts(days_ago=35),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=35), "notification_id": "req_X",
    }]
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is False


def test_defer_response_keeps_7_day_window(monkeypatch):
    notif = _FakeNotif(
        value="more",
        choices=[{"key": "more", "label": "Tell me more", "kind": "defer"}],
        responded_at=_ts(days_ago=5),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=5), "notification_id": "req_X",
    }]
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is True

    # Same shape but 8 days ago → out of window
    notif.responded_at = _ts(days_ago=8)
    history[0]["ts"] = _ts(days_ago=8)
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is False


def test_pending_response_uses_default_7_day_window(monkeypatch):
    """No response yet → fall back to 7-day window from firing time."""
    notif = _FakeNotif(value=None, choices=[], responded_at=None)
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=3), "notification_id": "req_X",
    }]
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is True

    history[0]["ts"] = _ts(days_ago=10)
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is False


def test_legacy_entry_without_notification_id_uses_7_days():
    """Entries from before notification_id was tracked still suppress 7 days."""
    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=3),  # no notification_id
    }]
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is True

    history[0]["ts"] = _ts(days_ago=10)
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is False


def test_missing_kind_falls_back_to_7d_legacy(monkeypatch):
    """Choices without `kind` are treated as if no response landed: the
    entry uses the 7-day legacy window from firing `ts`, not 90 days
    from `responded_at`. Symmetric with no-notification and pending-
    response cases — keeps resolution clean."""
    notif = _FakeNotif(
        value="dismiss",
        choices=[{"key": "dismiss", "label": "Not interesting"}],  # no `kind`
        responded_at=_ts(days_ago=2),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    # Firing 2 days ago + 7-day legacy window → still suppressing
    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=2), "notification_id": "req_X",
    }]
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is True

    # Firing 10 days ago → past the 7-day legacy window → no longer suppressing
    # (would still be within 90-day window if heuristic were active — proves it's gone)
    history[0]["ts"] = _ts(days_ago=10)
    notif.responded_at = _ts(days_ago=10)
    assert vrc._is_recent_escalation("new_type", "type:hypothesis", history) is False


# ── Query derivation per rule ───────────────────────────────────


def test_query_for_firing_new_type():
    q = vrc._query_for_firing("new_type", "type:hypothesis")
    assert q is not None
    # Direct field access (`type = ...`), NOT `$frontmatter.type` — the
    # latter errors with "null index string" when pages lack the field.
    # `exists(...)` guards against that.
    assert q["query"] == '@page and exists(type) and type = "hypothesis"'
    assert "hypothesis" in q["name"]
    assert "format" in q
    assert "fields" in q


def test_query_for_firing_stuck_state():
    q = vrc._query_for_firing("stuck_state", "hypothesis:PROPOSED")
    assert q is not None
    assert '"hypothesis"' in q["query"]
    assert '"PROPOSED"' in q["query"]
    assert "exists(type)" in q["query"]
    assert "exists(status)" in q["query"]
    # Should NOT use $frontmatter.X path (errors on missing fields)
    assert "$frontmatter." not in q["query"]


def test_query_for_firing_status_backlog_growing_same_as_stuck():
    q1 = vrc._query_for_firing("stuck_state", "hypothesis:PROPOSED")
    q2 = vrc._query_for_firing("status_backlog_growing", "hypothesis:PROPOSED")
    assert q1["query"] == q2["query"]


def test_query_for_firing_new_tag_family():
    q = vrc._query_for_firing("new_tag_family", "#mide/workflow")
    assert q is not None
    assert "#mide/workflow" in q["query"]
    assert q["query"].startswith("@page")


def test_query_for_firing_path_activity_spike():
    q = vrc._query_for_firing("path_activity_spike", "path:repos/electricrag")
    assert q is not None
    assert 'path("repos/electricrag")' in q["query"]


def test_query_for_firing_unknown_rule_returns_none():
    assert vrc._query_for_firing("nonexistent_rule", "type:foo") is None


def test_query_for_firing_malformed_focus_returns_none():
    # new_type expects "type:<value>"
    assert vrc._query_for_firing("new_type", "no_prefix") is None
    # stuck_state expects "<type>:<status>" with colon
    assert vrc._query_for_firing("stuck_state", "no_colon") is None


# ── Accepted-queries persistence + processing ──────────────────


def test_process_accept_responses_no_responses(tmp_path, monkeypatch):
    """No notification_id on entries → nothing to process."""
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: tmp_path)
    history = [
        {"rule": "new_type", "focus": "type:foo", "ts": _ts(days_ago=1)},
    ]
    out, added = vrc._process_accept_responses(history)
    assert added == 0
    assert not (tmp_path / "accepted_queries.json").exists()


def test_process_accept_responses_writes_query_on_act(tmp_path, monkeypatch):
    """kind=act response → query derived + appended to file + entry marked processed."""
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: tmp_path)
    notif = _FakeNotif(
        value="add_stuck_monitor",
        choices=[{"key": "add_stuck_monitor", "label": "Watch it", "kind": "act"}],
        responded_at=_ts(days_ago=0),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=1), "notification_id": "req_X",
    }]
    out, added = vrc._process_accept_responses(history)

    assert added == 1
    assert out[0]["processed_act"] is True

    accepted_path = tmp_path / "accepted_queries.json"
    assert accepted_path.exists()
    queries = json.loads(accepted_path.read_text(encoding="utf-8"))
    assert len(queries) == 1
    q = queries[0]
    assert q["source_rule"] == "new_type"
    assert q["source_focus"] == "type:hypothesis"
    assert q["source_notification_id"] == "req_X"
    assert "hypothesis" in q["query"]


def test_process_accept_responses_skips_decline(tmp_path, monkeypatch):
    """kind=decline → no query added, but entry IS marked processed (terminal)."""
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: tmp_path)
    notif = _FakeNotif(
        value="dismiss",
        choices=[{"key": "dismiss", "label": "no", "kind": "decline"}],
        responded_at=_ts(days_ago=0),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=1), "notification_id": "req_X",
    }]
    out, added = vrc._process_accept_responses(history)

    assert added == 0
    assert out[0]["processed_act"] is True  # decline is terminal — don't re-check
    assert not (tmp_path / "accepted_queries.json").exists()


def test_process_accept_responses_leaves_pending_unprocessed(tmp_path, monkeypatch):
    """No response yet → don't mark processed (will re-check next run)."""
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: tmp_path)
    notif = _FakeNotif(value=None, choices=[], responded_at=None)
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=1), "notification_id": "req_X",
    }]
    out, added = vrc._process_accept_responses(history)

    assert added == 0
    assert "processed_act" not in out[0]


def test_process_accept_responses_dedupes_via_existing_file(tmp_path, monkeypatch):
    """Re-running on the same accepted notification doesn't re-add the query."""
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: tmp_path)
    notif = _FakeNotif(
        value="add_stuck_monitor",
        choices=[{"key": "add_stuck_monitor", "label": "Watch", "kind": "act"}],
        responded_at=_ts(days_ago=0),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "new_type", "focus": "type:hypothesis",
        "ts": _ts(days_ago=1), "notification_id": "req_X",
    }]
    # First run: should add
    _, added1 = vrc._process_accept_responses(history)
    assert added1 == 1

    # Reset the entry (simulate a fresh run that hasn't seen processed_act yet,
    # e.g., entries appended later via _rewrite logic — the file-level dedup
    # via existing_ids should catch it).
    history[0].pop("processed_act", None)
    _, added2 = vrc._process_accept_responses(history)
    assert added2 == 0
    queries = json.loads((tmp_path / "accepted_queries.json").read_text(encoding="utf-8"))
    assert len(queries) == 1


def test_process_accept_responses_unknown_rule_marks_processed(tmp_path, monkeypatch):
    """Act on a rule with no known query template → log warning, mark processed."""
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: tmp_path)
    notif = _FakeNotif(
        value="some_act",
        choices=[{"key": "some_act", "label": "x", "kind": "act"}],
        responded_at=_ts(days_ago=0),
    )
    _patch_get_notification(monkeypatch, {"req_X": notif})

    history = [{
        "rule": "weird_rule_with_no_template", "focus": "anything",
        "ts": _ts(days_ago=1), "notification_id": "req_X",
    }]
    out, added = vrc._process_accept_responses(history)
    assert added == 0
    assert out[0]["processed_act"] is True  # don't keep re-checking


# ── prune_old ────────────────────────────────────────────────────


def test_prune_old_drops_snapshots_outside_window():
    snapshots = [
        _mk_snapshot(days_ago=70),
        _mk_snapshot(days_ago=30),
        _mk_snapshot(days_ago=1),
    ]
    pruned = vrc._prune_old(snapshots, window_days=60)
    assert len(pruned) == 2  # 70d-old dropped


# ── full collector entry point ───────────────────────────────────


def test_vault_recon_collect_handles_bridge_error(tmp_path, monkeypatch):
    """When vault_recon raises, the collector returns a structured error."""
    monkeypatch.setattr(
        vrc, "_ledger_dir", lambda: tmp_path / "vault_recon"
    )
    monkeypatch.setattr(
        vrc, "_user_jobs_dir", lambda: tmp_path / "user_jobs"
    )

    def boom(*args, **kwargs):
        raise RuntimeError("bridge unavailable")

    with patch("work_buddy.obsidian.datacore.env.vault_recon", boom):
        result = vrc.vault_recon_collect(skip_escalation=True)

    assert "error" in result
    assert result["stage"] == "snapshot"


def test_vault_recon_collect_writes_ledger_on_success(tmp_path, monkeypatch):
    """Successful snapshot is appended to ledger, latest.json is written."""
    ledger_dir = tmp_path / "vault_recon"
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: ledger_dir)
    monkeypatch.setattr(vrc, "_user_jobs_dir", lambda: tmp_path / "user_jobs")

    snapshot = _mk_snapshot(
        type_values={"hypothesis": 3},
        type_by_status={"hypothesis": {"PROPOSED": 3}},
        tag_tree={},
        recent_activity_by_path={"repos/x": 5},
    )

    with patch("work_buddy.obsidian.datacore.env.vault_recon", return_value=snapshot):
        result = vrc.vault_recon_collect(skip_escalation=True)

    assert "error" not in result
    assert result["ledger_size"] == 1

    snapshots_path = ledger_dir / "snapshots.json"
    latest_path = ledger_dir / "latest.json"
    assert snapshots_path.exists()
    assert latest_path.exists()

    snapshots = json.loads(snapshots_path.read_text())
    assert len(snapshots) == 1
    assert snapshots[0]["snapshot_ts"] == snapshot["snapshot_ts"]


def _seed_ledger_with_history(ledger_dir: Path, type_values: dict, n_snapshots: int = 3):
    """Seed snapshots.json with N empty-of-this-type snapshots so the bootstrap
    gate clears without contaminating historical_max for the type under test."""
    ledger_dir.mkdir(parents=True, exist_ok=True)
    snaps = [_mk_snapshot(days_ago=n_snapshots - i, type_values={}) for i in range(n_snapshots)]
    (ledger_dir / "snapshots.json").write_text(json.dumps(snaps))


def test_vault_recon_collect_dedupes_recent_escalations(tmp_path, monkeypatch):
    """An escalation already recorded within the suppression window doesn't fire again."""
    ledger_dir = tmp_path / "vault_recon"
    user_jobs_dir = tmp_path / "user_jobs"
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: ledger_dir)
    monkeypatch.setattr(vrc, "_user_jobs_dir", lambda: user_jobs_dir)

    _seed_ledger_with_history(ledger_dir, type_values={"hypothesis": 5})
    # Pre-seed escalation history with a recent firing for the same focus
    (ledger_dir / "escalation_history.jsonl").write_text(
        json.dumps({
            "rule": "new_type",
            "focus": "type:hypothesis",
            "ts": _ts(days_ago=1),
            "job_path": "old.md",
        }) + "\n"
    )

    snapshot = _mk_snapshot(type_values={"hypothesis": 5})

    with patch("work_buddy.obsidian.datacore.env.vault_recon", return_value=snapshot), \
         patch.object(vrc, "_agent_spawn_consent_granted", return_value=True):
        result = vrc.vault_recon_collect()

    assert result["rules_fired"] == 1
    assert result["escalations_spawned"] == 0  # dedupe suppressed
    assert not list(user_jobs_dir.glob("*.md"))


def test_vault_recon_collect_spawns_investigation_job(tmp_path, monkeypatch):
    """A novel rule firing produces a one-shot type:prompt job in user_jobs.

    Consent for sidecar:agent_spawn is mocked as granted; the consent-missing
    path is exercised in test_vault_recon_collect_surfaces_consent_when_missing.
    """
    ledger_dir = tmp_path / "vault_recon"
    user_jobs_dir = tmp_path / "user_jobs"
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: ledger_dir)
    monkeypatch.setattr(vrc, "_user_jobs_dir", lambda: user_jobs_dir)

    _seed_ledger_with_history(ledger_dir, type_values={"hypothesis": 5})

    snapshot = _mk_snapshot(type_values={"hypothesis": 5})

    with patch("work_buddy.obsidian.datacore.env.vault_recon", return_value=snapshot), \
         patch.object(vrc, "_agent_spawn_consent_granted", return_value=True):
        result = vrc.vault_recon_collect()

    assert result["rules_fired"] == 1
    assert result["escalations_spawned"] == 1

    jobs = list(user_jobs_dir.glob("*.md"))
    assert len(jobs) == 1
    job_text = jobs[0].read_text()
    assert "type: prompt" in job_text
    assert "spawn_mode: headless_ephemeral" in job_text
    assert "recurring: false" in job_text
    # YAML frontmatter must have an opening AND closing --- delimiter,
    # else the scheduler silently drops the file on hot-reload.
    assert job_text.startswith("---\n")
    # The prompt MUST be in the markdown body (after closing ---), not
    # in the frontmatter. The scheduler reads prompt=body.strip() at
    # work_buddy/sidecar/scheduler/jobs.py:183. A body-empty file means
    # empty prompt → executor returns "Empty prompt." error.
    fm_end = job_text.index("\n---\n", 4) + 5  # past the closing ---\n
    body_after_fm = job_text[fm_end:].strip()
    assert "type:hypothesis" in body_after_fm  # focus is in the prompt body
    assert "investigation agent" in body_after_fm
    assert len(body_after_fm) > 200, "prompt body should be substantial"


def test_vault_recon_collect_surfaces_consent_when_missing(tmp_path, monkeypatch):
    """When sidecar:agent_spawn isn't granted, the collector surfaces a
    consent_request with delta context rather than silently writing a job
    that the executor would refuse."""
    ledger_dir = tmp_path / "vault_recon"
    user_jobs_dir = tmp_path / "user_jobs"
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: ledger_dir)
    monkeypatch.setattr(vrc, "_user_jobs_dir", lambda: user_jobs_dir)

    _seed_ledger_with_history(ledger_dir, type_values={"hypothesis": 5})

    snapshot = _mk_snapshot(type_values={"hypothesis": 5})
    fake_request = {"request_id": "req_test_abc"}

    with patch("work_buddy.obsidian.datacore.env.vault_recon", return_value=snapshot), \
         patch.object(vrc, "_agent_spawn_consent_granted", return_value=False), \
         patch("work_buddy.consent.create_consent_request", return_value=fake_request) as mock_cr:
        result = vrc.vault_recon_collect()

    assert result["rules_fired"] == 1
    assert result["escalations_spawned"] == 0
    assert result["consent_requested"] == 1
    assert not list(user_jobs_dir.glob("*.md"))  # NO job written when consent missing

    # Verify consent_request was called with rich delta context
    mock_cr.assert_called_once()
    call_kwargs = mock_cr.call_args.kwargs
    assert call_kwargs["operation"] == "sidecar:agent_spawn"
    assert "type:hypothesis" in call_kwargs["reason"]
    assert call_kwargs["context"]["rule"] == "new_type"
    assert call_kwargs["context"]["focus"] == "type:hypothesis"

    # consent_request_history.jsonl records the firing for dedup
    history_path = ledger_dir / "consent_request_history.jsonl"
    assert history_path.exists()
    history_entry = json.loads(history_path.read_text().strip())
    assert history_entry["rule"] == "new_type"
    assert history_entry["request_id"] == "req_test_abc"


def test_vault_recon_collect_dedupes_consent_requests(tmp_path, monkeypatch):
    """Two collector runs on the same delta with consent missing should only
    surface one consent_request — the user shouldn't be spammed."""
    ledger_dir = tmp_path / "vault_recon"
    user_jobs_dir = tmp_path / "user_jobs"
    monkeypatch.setattr(vrc, "_ledger_dir", lambda: ledger_dir)
    monkeypatch.setattr(vrc, "_user_jobs_dir", lambda: user_jobs_dir)

    _seed_ledger_with_history(ledger_dir, type_values={"hypothesis": 5})

    snapshot = _mk_snapshot(type_values={"hypothesis": 5})
    fake_request = {"request_id": "req_test_abc"}

    with patch("work_buddy.obsidian.datacore.env.vault_recon", return_value=snapshot), \
         patch.object(vrc, "_agent_spawn_consent_granted", return_value=False), \
         patch("work_buddy.consent.create_consent_request", return_value=fake_request) as mock_cr:
        vrc.vault_recon_collect()
        result_2 = vrc.vault_recon_collect()

    # First run surfaced a consent request; second run should be silent.
    assert mock_cr.call_count == 1
    assert result_2["consent_requested"] == 0
