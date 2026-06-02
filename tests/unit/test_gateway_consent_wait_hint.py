"""The gateway consent-timeout message advertises the shell `wait` command.

When ``_auto_consent_request`` times out, its ``message`` must tell the
agent how to wait for the user's decision from a shell watcher (the Monitor
tool) and then retry — the discovery hook that makes the status CLI usable
in the loop the gateway half-builds. The guidance lives in ``message``
itself (not a separate field) so an agent acting on the message can't miss
it.
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


def test_timeout_message_advertises_shell_wait(tmp_agents_dir, monkeypatch):
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
    assert result.get("request_id")
    msg = result["message"]
    # Wait guidance is folded into the message itself, with the request id,
    # the free indefinite-wait suggestion, and the retry handoff.
    assert "/tmp/wb/status consent wait" in msg
    assert result["request_id"] in msg
    assert "--timeout -1" in msg
