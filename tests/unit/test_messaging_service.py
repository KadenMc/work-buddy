"""Unit tests for the messaging Flask service endpoints.

Covers the Stop-hook block contract (context 200 vs 204), the born-status
passthrough, and reply/patch — none of which had endpoint-level coverage before.
Uses an in-process Flask test client against the ``tmp_messaging_db`` temp DB
(the service's ``_get_conn`` resolves through the patched ``models._db_path``).
"""

import pytest

from work_buddy.messaging.service import app
from work_buddy.messaging.models import create_message, record_read


@pytest.fixture
def client(tmp_messaging_db):
    app.config.update(TESTING=True)
    return app.test_client()


def _ctx_url(recipient, session, hook_event):
    return (
        f"/messages?recipient={recipient}&session={session}"
        f"&status=pending&format=context&hook_event={hook_event}"
    )


class TestContextBlockingContract:
    """The Stop hook turns a non-empty context summary into a decision:block."""

    def test_stop_204_when_all_read(self, client, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="proj", type="task", subject="Seen")
        record_read(conn, msg["id"], "sess-1", reader_project="proj")
        resp = client.get(_ctx_url("proj", "sess-2", "Stop"))
        assert resp.status_code == 204

    def test_stop_200_when_unread(self, client, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        create_message(conn, sender="a", recipient="proj", type="event", subject="Fresh")
        resp = client.get(_ctx_url("proj", "sess-1", "Stop"))
        assert resp.status_code == 200
        assert "Fresh" in resp.get_json()["hookSpecificOutput"]["additionalContext"]

    def test_stop_surface_once_then_release(self, client, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        create_message(conn, sender="a", recipient="proj", type="event", subject="OneShot")
        first = client.get(_ctx_url("proj", "sess-1", "Stop"))
        assert first.status_code == 200  # blocks once, auto-marks read
        second = client.get(_ctx_url("proj", "sess-1", "Stop"))
        assert second.status_code == 204  # released — no longer keeps the block alive

    def test_sessionstart_still_shows_read_message(self, client, tmp_messaging_db):
        """Non-blocking summaries keep showing read-but-recent context."""
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="proj", type="task", subject="Recent")
        record_read(conn, msg["id"], "sess-1", reader_project="proj")
        resp = client.get(_ctx_url("proj", "sess-2", "SessionStart"))
        assert resp.status_code == 200
        assert "Recent" in resp.get_json()["hookSpecificOutput"]["additionalContext"]

    def test_high_priority_blocks_until_resolved(self, client, tmp_messaging_db):
        """A high-priority message keeps returning 200 across Stop renders until resolved."""
        conn, _ = tmp_messaging_db
        msg = create_message(
            conn, sender="x", recipient="proj", type="retry_exhausted",
            subject="Boom", priority="high",
        )
        assert client.get(_ctx_url("proj", "s1", "Stop")).status_code == 200
        # Still blocks after being read (high priority, unlike a normal message).
        assert client.get(_ctx_url("proj", "s1", "Stop")).status_code == 200
        client.patch(f"/messages/{msg['id']}", json={"status": "resolved"})
        assert client.get(_ctx_url("proj", "s1", "Stop")).status_code == 204


class TestStatusPassthrough:
    def test_post_honors_status(self, client):
        resp = client.post("/messages", json={
            "sender": "sidecar:retry_queue", "recipient": "proj",
            "type": "retry_success", "subject": "Done", "status": "resolved",
        })
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "resolved"

    def test_post_defaults_pending(self, client):
        resp = client.post("/messages", json={
            "sender": "a", "recipient": "proj", "type": "task", "subject": "Q",
        })
        assert resp.status_code == 201
        assert resp.get_json()["status"] == "pending"

    def test_born_resolved_never_blocks_stop(self, client):
        client.post("/messages", json={
            "sender": "sidecar:retry_queue", "recipient": "proj",
            "recipient_session": "sess-1", "type": "retry_success",
            "subject": "FYI", "status": "resolved",
        })
        resp = client.get(_ctx_url("proj", "sess-1", "Stop"))
        assert resp.status_code == 204


class TestReplyAndPatch:
    def test_reply_creates_threaded_message(self, client, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        parent = create_message(conn, sender="a", recipient="proj", type="task", subject="Orig")
        resp = client.post(f"/messages/{parent['id']}/reply", json={"sender": "proj", "body": "ack"})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["thread_id"] == parent["thread_id"]
        assert data["in_reply_to"] == parent["id"]

    def test_patch_updates_status(self, client, tmp_messaging_db):
        conn, _ = tmp_messaging_db
        msg = create_message(conn, sender="a", recipient="proj", type="task", subject="S")
        resp = client.patch(f"/messages/{msg['id']}", json={"status": "resolved"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "resolved"
