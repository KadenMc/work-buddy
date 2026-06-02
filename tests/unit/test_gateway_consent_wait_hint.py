"""The gateway consent-timeout result advertises the shell `wait` command.

When ``_auto_consent_request`` times out, its result must carry a
``wait_hint`` telling the agent how to wait for the user's decision from a
shell watcher (the Monitor tool) and then retry — the discovery hook that
makes the status CLI usable in the loop the gateway half-builds.
"""

from __future__ import annotations


class _FakeDispatcher:
    """Stands in for SurfaceDispatcher: delivery is a no-op and the poll
    always times out (returns None)."""

    def deliver(self, *a, **k):
        return None

    def poll_response(self, *a, **k):
        return None  # simulate the user not responding within the window

    def dismiss_others(self, *a, **k):
        return None


def test_timeout_result_includes_wait_hint(tmp_agents_dir, monkeypatch):
    from work_buddy.mcp_server.tools import gateway
    from work_buddy.notifications import dispatcher as disp

    monkeypatch.setattr(
        disp.SurfaceDispatcher, "from_config",
        classmethod(lambda cls: _FakeDispatcher()),
    )

    result = gateway._auto_consent_request(
        ["task_toggle"], "task_toggle", "op_test123", timeout=0,
    )

    assert result["status"] == "timeout"
    assert "request_id" in result and result["request_id"]
    assert "wait_hint" in result
    hint = result["wait_hint"]
    assert result["request_id"] in hint
    assert "/tmp/wb/status consent wait" in hint
