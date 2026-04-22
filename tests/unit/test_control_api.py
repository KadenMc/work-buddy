"""Unit tests for GET /api/control/graph dashboard endpoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def client():
    from work_buddy.dashboard.service import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    """Make sure other tests don't leak a stale cache into these API calls."""
    from work_buddy.control.graph import invalidate_graph
    invalidate_graph()
    yield
    invalidate_graph()


@pytest.mark.unit
def test_control_graph_endpoint_returns_200(client):
    resp = client.get("/api/control/graph")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "nodes" in data
    assert "cache" in data


@pytest.mark.unit
def test_control_graph_endpoint_contains_expected_domains(client):
    resp = client.get("/api/control/graph")
    data = resp.get_json()
    node_ids = set(data["nodes"].keys())
    # At least these domains must be present (per plan sign-off)
    for expected in [
        "domain:journal",
        "domain:notifications",
        "domain:knowledge",
        "domain:browser",
        "domain:calendar",
        "domain:runtime",
        "domain:system",
    ]:
        assert expected in node_ids


@pytest.mark.unit
def test_control_graph_node_structure(client):
    resp = client.get("/api/control/graph")
    data = resp.get_json()
    sample_node = data["nodes"]["domain:journal"]
    for field in (
        "id", "kind", "label", "description",
        "grouping_parents", "dependencies",
        "preference", "effective_state",
        "requirement_ids", "affects_capabilities",
        "status_reason", "blocking_issues",
    ):
        assert field in sample_node, f"missing field: {field}"


@pytest.mark.unit
def test_reprobe_endpoint_blocked_in_read_only(client):
    with patch("work_buddy.dashboard.service._is_read_only", return_value=True):
        resp = client.post("/api/control/reprobe")
    assert resp.status_code == 403


@pytest.mark.unit
def test_reprobe_endpoint_runs_probe_all_and_returns_graph(client):
    """The endpoint must call probe_all(force=True) and return the
    rebuilt graph shape."""
    with patch("work_buddy.dashboard.service._is_read_only", return_value=False), \
         patch("work_buddy.tools._register_default_probes") as mock_reg, \
         patch("work_buddy.tools.probe_all") as mock_probe:
        resp = client.post("/api/control/reprobe")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "nodes" in data
    assert "cache" in data
    # probe_all was called with force=True
    assert mock_reg.called
    assert mock_probe.called
    args, kwargs = mock_probe.call_args
    assert kwargs.get("force") is True or (args and args[0] is True)


@pytest.mark.unit
def test_control_graph_endpoint_handles_internal_error(client):
    """If build_graph raises, the endpoint returns 500 with an error body."""
    with patch(
        "work_buddy.control.graph.build_graph",
        side_effect=RuntimeError("boom"),
    ):
        resp = client.get("/api/control/graph")
        assert resp.status_code == 500
        assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# POST /api/control/preference
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_control_preference_rejects_empty_body(client):
    with patch("work_buddy.dashboard.service._is_read_only", return_value=False):
        resp = client.post("/api/control/preference", json={})
    assert resp.status_code == 400
    assert "updates" in resp.get_json()["error"]


@pytest.mark.unit
def test_control_preference_blocked_in_read_only(client):
    with patch("work_buddy.dashboard.service._is_read_only", return_value=True):
        resp = client.post(
            "/api/control/preference",
            json={"updates": {"telegram": {"wanted": False}}},
        )
    assert resp.status_code == 403


@pytest.mark.unit
def test_control_preference_writes_and_returns_graph(client):
    """Happy path: POST a preference update, get fresh graph + written list."""
    with patch("work_buddy.dashboard.service._is_read_only", return_value=False), \
         patch(
             "work_buddy.health.preferences.apply_preference_updates",
             return_value=["telegram"],
         ) as mock_apply, \
         patch("work_buddy.consent.grant_consent") as mock_grant:
        resp = client.post(
            "/api/control/preference",
            json={"updates": {"telegram": {"wanted": False, "reason": "not using"}}},
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["written"] == ["telegram"]
    assert "nodes" in data
    # The endpoint grants consent before applying updates (auto-consent pattern)
    assert mock_grant.called
    assert mock_apply.called


@pytest.mark.unit
def test_control_preference_surfaces_backend_error_as_500(client):
    with patch("work_buddy.dashboard.service._is_read_only", return_value=False), \
         patch(
             "work_buddy.health.preferences.apply_preference_updates",
             side_effect=RuntimeError("disk full"),
         ), \
         patch("work_buddy.consent.grant_consent"):
        resp = client.post(
            "/api/control/preference",
            json={"updates": {"telegram": {"wanted": False}}},
        )
    assert resp.status_code == 500
    assert "disk full" in resp.get_json()["error"]
