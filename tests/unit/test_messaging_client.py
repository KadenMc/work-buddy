"""Unit tests for the messaging HTTP client wrapper (payload construction).

client.py had no direct coverage; these pin the ``status`` passthrough that lets
the retry sweep emit born-resolved notifications.
"""

from unittest.mock import patch

from work_buddy.messaging import client


def test_send_message_includes_status_when_non_default():
    with patch.object(client, "_request", return_value={"id": "x"}) as m:
        client.send_message(
            sender="sidecar:retry_queue", recipient="work-buddy",
            type="retry_success", subject="ok", status="resolved",
        )
    payload = m.call_args.args[2]
    assert payload["status"] == "resolved"


def test_send_message_omits_default_status():
    with patch.object(client, "_request", return_value={"id": "x"}) as m:
        client.send_message(sender="a", recipient="b", type="task", subject="q")
    payload = m.call_args.args[2]
    assert "status" not in payload  # default 'pending' is not sent (server default)
