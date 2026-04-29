"""Tests for ThunderbirdEmailProvider — HTTP client against a fake bridge.

We stand up a real ``http.server`` on localhost in a thread, write a fake
connection file pointing at it, and exercise the client. This catches:

  - URL composition + auth header
  - JSON request/response shape
  - typed-error mapping (403 → EmailBridgeUnauthorized, network → unreachable)
  - stable-key derivation from bridge response shape
  - 403-and-retry-once on stale token
"""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from work_buddy.email.errors import (
    EmailBridgeUnauthorized,
    EmailBridgeUnreachable,
    EmailMessageNotFound,
)
from work_buddy.email.models import EmailMessageHandle
from work_buddy.email.providers import thunderbird as tb_mod


# ---------------------------------------------------------------------------
# Fake bridge HTTP server
# ---------------------------------------------------------------------------


class _BridgeState:
    def __init__(self, token: str = "test-token-1") -> None:
        self.token = token
        self.requests: list[dict] = []          # for assertions
        self.health_payload = {
            "ok": True,
            "plugin": "thunderbird-work-buddy",
            "protocol_version": "0.1.0",
            "accounts_allowed": 1,
            "accessible_accounts": 1,
        }
        self.accounts_payload = {
            "accounts": [{"id": "acct1", "name": "Personal", "type": "imap",
                          "allowed": True, "identities": []}],
            "allowlist_size": 1,
        }
        self.recent_payload = {
            "messages": [
                {
                    "provider_message_id": "m1@host",
                    "thread_id": 1,
                    "subject": "Hello",
                    "author": "Alice <alice@x>",
                    "recipients": "me@x",
                    "cc": "",
                    "date": "2026-04-28T12:00:00Z",
                    "folder": "Inbox",
                    "folder_type": "inbox",
                    "folder_path": "imap://acct1/INBOX",
                    "account_id": "acct1",
                    "read": False,
                    "flagged": False,
                    "tags": [],
                    "preview": "Hi",
                },
            ],
            "total_collected": 1, "limit": 50, "days_back": 2,
        }
        self.next_403_count = 0     # if >0, next N requests fail with 403 (test stale-token retry)


def _make_handler(state: _BridgeState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args, **_kw):  # silence test noise
            return

        def _check_auth(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if state.next_403_count > 0:
                state.next_403_count -= 1
                self._json(403, {"error": "Forbidden (simulated stale token)"})
                return False
            if auth != f"Bearer {state.token}":
                self._json(403, {"error": "Forbidden"})
                return False
            return True

        def _json(self, status, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw or b"{}")

        def do_GET(self):
            if not self._check_auth():
                return
            state.requests.append({"method": "GET", "path": self.path})
            if self.path == "/health":
                return self._json(200, state.health_payload)
            if self.path == "/accounts":
                return self._json(200, state.accounts_payload)
            self._json(404, {"error": f"Unknown route: GET {self.path}"})

        def do_POST(self):
            if not self._check_auth():
                return
            body = self._read_body()
            state.requests.append({"method": "POST", "path": self.path, "body": body})
            if self.path == "/messages/recent":
                return self._json(200, state.recent_payload)
            if self.path == "/messages/get":
                pid = body.get("provider_message_id")
                if pid == "missing":
                    return self._json(400, {"error": "Message not found: missing"})
                summary = state.recent_payload["messages"][0]
                payload = dict(summary)
                payload.update({
                    "body": "Hello world",
                    "body_format": "text",
                    "body_truncated": False,
                    "body_length": 11,
                })
                return self._json(200, payload)
            if self.path == "/messages/display":
                return self._json(200, {"ok": True, "mode": body.get("mode", "3pane"),
                                        "subject": "Hello"})
            if self.path == "/folders":
                return self._json(200, {"folders": []})
            self._json(404, {"error": f"Unknown route: POST {self.path}"})

    return Handler


@contextmanager
def _running_bridge(state: _BridgeState, tmp_path: Path, monkeypatch):
    handler_cls = _make_handler(state)
    # Bind to an ephemeral port.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Write a fake connection file the provider will discover.
    conn_dir = tmp_path / "thunderbird-work-buddy"
    conn_dir.mkdir(parents=True, exist_ok=True)
    conn_file = conn_dir / "connection.json"
    conn_file.write_text(json.dumps({
        "plugin": "thunderbird-work-buddy",
        "version": "0.1.0",
        "port": port,
        "token": state.token,
        "pid": 0,
        "profile_dir": str(tmp_path),
    }), encoding="utf-8")

    monkeypatch.setattr(
        tb_mod, "connection_file_path", lambda: conn_file,
    )

    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_discover_connection_missing_file_raises_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(tb_mod, "connection_file_path", lambda: tmp_path / "no-such")
    with pytest.raises(EmailBridgeUnreachable):
        tb_mod.discover_connection()


def test_discover_connection_malformed_json_raises_unreachable(tmp_path, monkeypatch):
    p = tmp_path / "connection.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(tb_mod, "connection_file_path", lambda: p)
    with pytest.raises(EmailBridgeUnreachable):
        tb_mod.discover_connection()


def test_discover_connection_wrong_plugin_raises(tmp_path, monkeypatch):
    p = tmp_path / "connection.json"
    p.write_text(json.dumps({
        "plugin": "imposter", "version": "0.1.0",
        "port": 1, "token": "x",
    }), encoding="utf-8")
    monkeypatch.setattr(tb_mod, "connection_file_path", lambda: p)
    with pytest.raises(EmailBridgeUnreachable):
        tb_mod.discover_connection()


def test_health_round_trip(tmp_path, monkeypatch):
    state = _BridgeState()
    with _running_bridge(state, tmp_path, monkeypatch):
        provider = tb_mod.ThunderbirdEmailProvider()
        h = provider.health()
        assert h["ok"] is True
        assert h["plugin"] == "thunderbird-work-buddy"
    # Auth header was sent
    assert state.requests[0]["method"] == "GET"
    assert state.requests[0]["path"] == "/health"


def test_recent_messages_maps_to_summary_with_stable_key(tmp_path, monkeypatch):
    state = _BridgeState()
    with _running_bridge(state, tmp_path, monkeypatch):
        provider = tb_mod.ThunderbirdEmailProvider()
        summaries = provider.recent_messages(days_back=2, unread_only=True)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.subject == "Hello"
    assert s.handle.provider_message_id == "m1@host"
    assert s.handle.folder_path == "imap://acct1/INBOX"
    # rfc message id present → stable key uses it
    assert s.stable_key == "mid:m1@host"
    # folder_type + account_id come through from the bridge
    assert s.folder_type == "inbox"
    assert s.account_id == "acct1"
    assert s.folder == "Inbox"


def test_get_message_unknown_id_raises_not_found(tmp_path, monkeypatch):
    state = _BridgeState()
    with _running_bridge(state, tmp_path, monkeypatch):
        provider = tb_mod.ThunderbirdEmailProvider()
        with pytest.raises(EmailMessageNotFound):
            provider.get_message(EmailMessageHandle("missing", "imap://acct1/INBOX"))


def test_403_then_recovery_via_connection_refresh(tmp_path, monkeypatch):
    """Stale token: bridge returns 403 once; the client refreshes the connection
    file and retries successfully."""
    state = _BridgeState(token="token-A")
    with _running_bridge(state, tmp_path, monkeypatch):
        provider = tb_mod.ThunderbirdEmailProvider()
        # Force a 403 on the next request — the client should refresh the
        # connection file (still pointing at the same valid token here) and
        # retry once. Without retry, this would raise.
        state.next_403_count = 1
        info = provider.health()
        assert info["ok"] is True
    # Recorded: a 403'd request followed by a successful one.
    methods = [(r["method"], r["path"]) for r in state.requests]
    assert methods.count(("GET", "/health")) == 1   # only the successful retry was recorded; rejected one short-circuited before requests.append


def test_persistent_403_raises_unauthorized(tmp_path, monkeypatch):
    state = _BridgeState(token="token-A")
    with _running_bridge(state, tmp_path, monkeypatch):
        provider = tb_mod.ThunderbirdEmailProvider()
        # Force 403 on both attempts (initial + retry).
        state.next_403_count = 5
        with pytest.raises(EmailBridgeUnauthorized):
            provider.health()


def test_unreachable_port_raises_unreachable(tmp_path, monkeypatch):
    # Write a connection file pointing at a port nothing is listening on.
    p = tmp_path / "connection.json"
    p.write_text(json.dumps({
        "plugin": "thunderbird-work-buddy",
        "version": "0.1.0",
        "port": 1,    # privileged port, definitely not listening
        "token": "x",
        "pid": 0,
        "profile_dir": str(tmp_path),
    }), encoding="utf-8")
    monkeypatch.setattr(tb_mod, "connection_file_path", lambda: p)
    provider = tb_mod.ThunderbirdEmailProvider(timeout_seconds=1)
    with pytest.raises(EmailBridgeUnreachable):
        provider.health()
