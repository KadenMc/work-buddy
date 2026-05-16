"""Unit tests for the dashboard card registry (work_buddy.dashboard.cards)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.control.gates import Component
from work_buddy.dashboard import cards as cards_mod
from work_buddy.dashboard.cards import (
    CARD_REGISTRY,
    DashboardCard,
    active_component_ids,
    cards_for_tab,
    register_card,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_registry():
    """Snapshot and restore CARD_REGISTRY so tests can register freely."""
    snapshot = dict(CARD_REGISTRY)
    yield CARD_REGISTRY
    CARD_REGISTRY.clear()
    CARD_REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# active_component_ids — gated on feature preference, not the control graph
# ---------------------------------------------------------------------------


def test_active_component_ids_excludes_explicitly_opted_out():
    catalog = {"obsidian": object(), "telegram": object()}
    with patch.object(cards_mod, "COMPONENT_CATALOG", catalog), \
         patch.object(cards_mod, "is_wanted",
                      side_effect=lambda cid: False if cid == "obsidian" else True):
        assert active_component_ids() == {"telegram"}


def test_active_component_ids_includes_undecided_and_wanted():
    catalog = {"a": object(), "b": object(), "c": object()}
    # None = undecided, True = wanted — both count as active.
    states = {"a": None, "b": True, "c": False}
    with patch.object(cards_mod, "COMPONENT_CATALOG", catalog), \
         patch.object(cards_mod, "is_wanted", side_effect=lambda cid: states[cid]):
        assert active_component_ids() == {"a", "b"}


# ---------------------------------------------------------------------------
# cards_for_tab
# ---------------------------------------------------------------------------


def test_cards_for_tab_filters_by_mount_point(isolated_registry):
    isolated_registry.clear()
    register_card(DashboardCard(id="x.a", mount_point="activity"))
    register_card(DashboardCard(id="x.b", mount_point="overview"))
    with patch.object(cards_mod, "active_component_ids", return_value=set()):
        ids = [c["id"] for c in cards_for_tab("activity")]
    assert ids == ["x.a"]


def test_cards_for_tab_drops_card_when_gated_component_opted_out(isolated_registry):
    isolated_registry.clear()
    register_card(DashboardCard(
        id="obsidian.widget", mount_point="activity",
        gate=Component("obsidian"),
    ))
    with patch.object(cards_mod, "active_component_ids", return_value=set()):
        assert cards_for_tab("activity") == []
    with patch.object(cards_mod, "active_component_ids", return_value={"obsidian"}):
        assert [c["id"] for c in cards_for_tab("activity")] == ["obsidian.widget"]


def test_cards_for_tab_keeps_ungated_cards_regardless(isolated_registry):
    isolated_registry.clear()
    register_card(DashboardCard(id="core.always", mount_point="activity"))
    with patch.object(cards_mod, "active_component_ids", return_value=set()):
        assert [c["id"] for c in cards_for_tab("activity")] == ["core.always"]


def test_cards_for_tab_sorted_by_mount_slot(isolated_registry):
    isolated_registry.clear()
    register_card(DashboardCard(id="x.late", mount_point="activity", mount_slot=9))
    register_card(DashboardCard(id="x.early", mount_point="activity", mount_slot=1))
    with patch.object(cards_mod, "active_component_ids", return_value=set()):
        assert [c["id"] for c in cards_for_tab("activity")] == ["x.early", "x.late"]


def test_cards_for_tab_unknown_mount_point_is_empty():
    with patch.object(cards_mod, "active_component_ids", return_value=set()):
        assert cards_for_tab("no-such-mount-point") == []


# ---------------------------------------------------------------------------
# register_card
# ---------------------------------------------------------------------------


def test_register_card_rejects_duplicate_id(isolated_registry):
    register_card(DashboardCard(id="dup.test", mount_point="activity"))
    with pytest.raises(ValueError, match="already registered"):
        register_card(DashboardCard(id="dup.test", mount_point="activity"))


def test_register_card_validates_gate_against_catalog(isolated_registry):
    with pytest.raises(ValueError, match="unknown components"):
        register_card(DashboardCard(
            id="bad.gate", mount_point="activity",
            gate=Component("zzz_definitely_not_a_component"),
        ))


def test_builtin_activity_cards_are_registered():
    """The three Settings -> Activity widgets register at import."""
    for card_id in ("obsidian.bridge_sparkline", "core.event_log",
                    "core.notification_log"):
        assert card_id in CARD_REGISTRY
        assert CARD_REGISTRY[card_id].mount_point == "activity"


def test_builtin_bridge_card_is_gated_on_obsidian():
    assert CARD_REGISTRY["obsidian.bridge_sparkline"].gate == Component("obsidian")
    assert CARD_REGISTRY["core.event_log"].gate is None
    assert CARD_REGISTRY["core.notification_log"].gate is None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from work_buddy.dashboard.service import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_endpoint_returns_card_id_list(client):
    resp = client.get("/api/dashboard/cards/activity")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "cards" in data
    ids = {c["id"] for c in data["cards"]}
    # event log + notification log are ungated -> always present
    assert {"core.event_log", "core.notification_log"} <= ids


def test_endpoint_unknown_mount_point_returns_empty(client):
    resp = client.get("/api/dashboard/cards/no-such-mount")
    assert resp.status_code == 200
    assert resp.get_json() == {"cards": []}
