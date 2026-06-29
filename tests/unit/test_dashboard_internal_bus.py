"""Contract tests for the dashboard's cross-process event ingress.

Covers the ``/internal/bus`` endpoint (loopback gate, validation, republish
onto the in-process bus) and ``events.publish_cross_process`` (posts to that
endpoint). Cross-process events travel sidecar -> dashboard with no durable
store in the path.
"""

import json
from unittest.mock import patch

import pytest

from work_buddy.dashboard import events as ev
from work_buddy.dashboard import service as dash


@pytest.fixture
def client():
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


# ---------------------------------------------------------------------------
# /internal/bus endpoint
# ---------------------------------------------------------------------------

def test_internal_bus_rejects_non_loopback(client):
    """A remote caller must never be able to inject bus events."""
    resp = client.post(
        "/internal/bus",
        json={"event_type": "task.created", "payload": {}},
        environ_base={"REMOTE_ADDR": "10.1.2.3"},
    )
    assert resp.status_code == 403


def test_internal_bus_requires_event_type(client):
    resp = client.post("/internal/bus", json={"payload": {}})
    assert resp.status_code == 400


def test_internal_bus_publishes_to_in_process_bus(client):
    published: list[tuple[str, object]] = []

    class _Bus:
        def publish(self, event_type, payload=None):
            published.append((event_type, payload))

    # internal_bus does `from ...events import get_bus` at call time, so
    # patching the module attribute is sufficient.
    with patch.object(ev, "get_bus", return_value=_Bus()):
        resp = client.post(
            "/internal/bus",
            json={"event_type": "task.created", "payload": {"id": 7}},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
    assert resp.status_code == 200
    assert published == [("task.created", {"id": 7})]


# ---------------------------------------------------------------------------
# publish_cross_process -> /internal/bus
# ---------------------------------------------------------------------------

def test_publish_cross_process_posts_to_internal_bus():
    captured: dict[str, object] = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    with patch("urllib.request.urlopen", _fake_urlopen):
        ok = ev.publish_cross_process("task.created", {"id": 1})

    assert ok is True
    assert captured["url"].endswith("/internal/bus")
    assert captured["url"].startswith("http://127.0.0.1:")
    assert captured["body"] == {"event_type": "task.created", "payload": {"id": 1}}


def test_publish_cross_process_swallows_unreachable_dashboard():
    """Best-effort by contract: a down dashboard returns False, never raises."""
    from urllib.error import URLError

    def _boom(req, timeout=None):
        raise URLError("connection refused")

    with patch("urllib.request.urlopen", _boom):
        assert ev.publish_cross_process("task.created", {"id": 1}) is False
