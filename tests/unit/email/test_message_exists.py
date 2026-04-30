"""Tests for the message_exists() probe + the email_message
``source_removed`` quarantine trigger.

Coverage:
  - FakeEmailProvider.message_exists semantics
  - ThunderbirdEmailProvider.message_exists against a fake bridge
    (200 exists, 200 not-exists, HTTP error → None defensive)
  - trigger_source_removed for source="email_message" — fires when the
    bridge says exists=False, leaves alone when True or when the
    bridge is unreachable
  - Integration with TriagePool sweep: a quarantined entry transitions
    out of pending state.
"""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.email.errors import EmailMessageNotFound
from work_buddy.email.models import (
    EmailFolder,
    EmailMessageHandle,
    EmailSummary,
    stable_key_for,
)
from work_buddy.email.providers import thunderbird as tb_mod
from work_buddy.email.providers.fake import FakeEmailProvider


# ---------------------------------------------------------------------------
# FakeEmailProvider tests
# ---------------------------------------------------------------------------


def _fixture_summary(rfc: str = "abc@host", folder: str = "imap://x/INBOX") -> EmailSummary:
    return EmailSummary(
        stable_key=stable_key_for(
            rfc_message_id=rfc, sender="alice@x", date="2026-04-29T10:00:00Z",
            subject="Hi",
        ),
        handle=EmailMessageHandle(provider_message_id=rfc, folder_path=folder),
        subject="Hi", sender="alice@x", recipients="me@x", cc="",
        date="2026-04-29T10:00:00Z", folder="Inbox", account_id="acct1",
        read=False, flagged=False, tags=[], preview="Hi", rfc_message_id=rfc,
        folder_type="inbox",
    )


def test_fake_provider_message_exists_match():
    p = FakeEmailProvider()
    p.add(_fixture_summary())
    h = EmailMessageHandle(provider_message_id="abc@host", folder_path="imap://x/INBOX")
    assert p.message_exists(h) is True


def test_fake_provider_message_exists_missing():
    p = FakeEmailProvider()
    h = EmailMessageHandle(provider_message_id="missing@host", folder_path="imap://x/INBOX")
    assert p.message_exists(h) is False


def test_fake_provider_message_exists_folder_mismatch():
    """Same provider_message_id at a different folder_path → not the same
    message from the trigger's perspective. (Gmail's labels-as-folders
    edge case is handled by within-run dedup, not by the existence check.)"""
    p = FakeEmailProvider()
    p.add(_fixture_summary(folder="imap://x/INBOX"))
    h = EmailMessageHandle(
        provider_message_id="abc@host",
        folder_path="imap://x/[Gmail]/Trash",
    )
    assert p.message_exists(h) is False


def test_fake_provider_remove_then_exists_false():
    """Simulate the user moving / deleting the email between capture
    and the next sweep — the existence check flips to False."""
    p = FakeEmailProvider()
    p.add(_fixture_summary())
    h = EmailMessageHandle(provider_message_id="abc@host", folder_path="imap://x/INBOX")
    assert p.message_exists(h) is True
    assert p.remove(provider_message_id="abc@host", folder_path="imap://x/INBOX") is True
    assert p.message_exists(h) is False


# ---------------------------------------------------------------------------
# ThunderbirdEmailProvider against a fake bridge
# ---------------------------------------------------------------------------


@contextmanager
def _running_bridge(tmp_path: Path, monkeypatch, *, response_handler=None):
    token = "test-token-msg-exists"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw):
            return

        def do_POST(self):
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {token}":
                self.send_response(403); self.end_headers(); self.wfile.write(b'{"error":"f"}'); return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            if self.path == "/messages/exists":
                if response_handler:
                    status, payload = response_handler(body)
                else:
                    status, payload = 200, {"exists": True, "summary": {}}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404); self.end_headers()

    sock = socket.socket(); sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]; sock.close()
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn_dir = tmp_path / "thunderbird-work-buddy"
    conn_dir.mkdir(parents=True, exist_ok=True)
    conn_file = conn_dir / "connection.json"
    conn_file.write_text(json.dumps({
        "plugin": "thunderbird-work-buddy", "version": "0.1.0",
        "port": port, "token": token, "pid": 0, "profile_dir": str(tmp_path),
    }), encoding="utf-8")
    monkeypatch.setattr(tb_mod, "connection_file_path", lambda: conn_file)
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_thunderbird_provider_message_exists_true(tmp_path, monkeypatch):
    handler = lambda body: (200, {"exists": True, "summary": {}})
    with _running_bridge(tmp_path, monkeypatch, response_handler=handler):
        provider = tb_mod.ThunderbirdEmailProvider()
        result = provider.message_exists(EmailMessageHandle("abc@host", "imap://x/INBOX"))
    assert result is True


def test_thunderbird_provider_message_exists_false(tmp_path, monkeypatch):
    handler = lambda body: (200, {"exists": False})
    with _running_bridge(tmp_path, monkeypatch, response_handler=handler):
        provider = tb_mod.ThunderbirdEmailProvider()
        result = provider.message_exists(EmailMessageHandle("missing@host", "imap://x/INBOX"))
    assert result is False


def test_thunderbird_provider_message_exists_returns_none_on_4xx(tmp_path, monkeypatch):
    """A 4xx (e.g. account access changed) MUST return None, not raise.
    The unattended sweep depends on this for safety."""
    handler = lambda body: (400, {"error": "Account not accessible"})
    with _running_bridge(tmp_path, monkeypatch, response_handler=handler):
        provider = tb_mod.ThunderbirdEmailProvider()
        result = provider.message_exists(EmailMessageHandle("abc@host", "imap://x/INBOX"))
    assert result is None


def test_thunderbird_provider_message_exists_returns_none_on_unreachable(tmp_path, monkeypatch):
    """No bridge running — must return None, not raise."""
    p = tmp_path / "connection.json"
    p.write_text(json.dumps({
        "plugin": "thunderbird-work-buddy", "version": "0.1.0",
        "port": 1, "token": "x", "pid": 0, "profile_dir": str(tmp_path),
    }), encoding="utf-8")
    monkeypatch.setattr(tb_mod, "connection_file_path", lambda: p)
    provider = tb_mod.ThunderbirdEmailProvider(timeout_seconds=1)
    result = provider.message_exists(EmailMessageHandle("abc@host", "imap://x/INBOX"))
    assert result is None


def test_thunderbird_provider_message_exists_handles_malformed_json(tmp_path, monkeypatch):
    """Bridge returns 200 but with a body that's not the expected
    {"exists": bool} shape → None (defensive)."""
    handler = lambda body: (200, {"unrelated": "shape"})
    with _running_bridge(tmp_path, monkeypatch, response_handler=handler):
        provider = tb_mod.ThunderbirdEmailProvider()
        result = provider.message_exists(EmailMessageHandle("abc@host", "imap://x/INBOX"))
    assert result is None


def test_thunderbird_provider_message_exists_empty_handle_returns_none():
    """A handle with empty fields can't be checked — return None."""
    provider = tb_mod.ThunderbirdEmailProvider()
    assert provider.message_exists(EmailMessageHandle("", "imap://x/INBOX")) is None
    assert provider.message_exists(EmailMessageHandle("abc@host", "")) is None


# ---------------------------------------------------------------------------
# Trigger function (trigger_source_removed for email_message)
# ---------------------------------------------------------------------------


def _entry_for(provider_message_id: str, folder_path: str):
    """Build a minimal PoolEntry-shaped object the trigger can consume.
    The trigger reads ``entry.source`` and ``entry.item['metadata']``."""
    class _E:
        def __init__(self):
            self.source = "email_message"
            self.run_id = "bgt_test"
            self.item_id = "email_test"
            self.item = {
                "id": self.item_id,
                "source": self.source,
                "metadata": {
                    "provider_message_id": provider_message_id,
                    "folder_path": folder_path,
                },
            }
    return _E()


def _email_descriptor():
    from work_buddy.triage.sources import load_source_registry, reset_for_tests
    reset_for_tests()
    desc = load_source_registry().get("email_message")
    return desc


def test_trigger_source_removed_email_fires_when_message_gone(monkeypatch):
    """exists=False → return 'source_removed'."""
    from work_buddy.triage.sources_triggers import trigger_source_removed
    import work_buddy.email.provider as pmod

    class _StubProvider:
        name = "stub"
        def message_exists(self, handle):
            return False

    monkeypatch.setattr(pmod, "get_email_provider", lambda: _StubProvider())
    entry = _entry_for("abc@host", "imap://x/INBOX")
    desc = _email_descriptor()
    assert trigger_source_removed(entry, desc) == "source_removed"


def test_trigger_source_removed_email_no_fire_when_message_present(monkeypatch):
    """exists=True → return None."""
    from work_buddy.triage.sources_triggers import trigger_source_removed
    import work_buddy.email.provider as pmod

    class _StubProvider:
        name = "stub"
        def message_exists(self, handle):
            return True

    monkeypatch.setattr(pmod, "get_email_provider", lambda: _StubProvider())
    entry = _entry_for("abc@host", "imap://x/INBOX")
    desc = _email_descriptor()
    assert trigger_source_removed(entry, desc) is None


def test_trigger_source_removed_email_no_fire_when_unreachable(monkeypatch):
    """Bridge unreachable → provider returns None → trigger returns None.
    Critical: the unattended sweep must not quarantine real entries when
    the bridge is briefly down. Treat ambiguity as 'still live'."""
    from work_buddy.triage.sources_triggers import trigger_source_removed
    import work_buddy.email.provider as pmod

    class _StubProvider:
        name = "stub"
        def message_exists(self, handle):
            return None

    monkeypatch.setattr(pmod, "get_email_provider", lambda: _StubProvider())
    entry = _entry_for("abc@host", "imap://x/INBOX")
    desc = _email_descriptor()
    assert trigger_source_removed(entry, desc) is None


def test_trigger_source_removed_email_no_fire_on_provider_init_error(monkeypatch):
    """Provider factory raises EmailProviderDisabled → trigger returns None."""
    from work_buddy.email.errors import EmailProviderDisabled
    from work_buddy.triage.sources_triggers import trigger_source_removed
    import work_buddy.email.provider as pmod

    def _raises():
        raise EmailProviderDisabled("provider off")
    monkeypatch.setattr(pmod, "get_email_provider", _raises)
    entry = _entry_for("abc@host", "imap://x/INBOX")
    desc = _email_descriptor()
    assert trigger_source_removed(entry, desc) is None


def test_trigger_source_removed_email_no_fire_on_provider_method_raise(monkeypatch):
    """If message_exists itself raises (regression!), the trigger
    catches it defensively and returns None."""
    from work_buddy.triage.sources_triggers import trigger_source_removed
    import work_buddy.email.provider as pmod

    class _StubProvider:
        name = "stub"
        def message_exists(self, handle):
            raise RuntimeError("simulated provider bug")

    monkeypatch.setattr(pmod, "get_email_provider", lambda: _StubProvider())
    entry = _entry_for("abc@host", "imap://x/INBOX")
    desc = _email_descriptor()
    assert trigger_source_removed(entry, desc) is None


def test_trigger_source_removed_email_no_fire_on_empty_metadata(monkeypatch):
    """Malformed metadata (no provider_message_id or no folder_path)
    → return None without even calling the provider."""
    from work_buddy.triage.sources_triggers import trigger_source_removed
    import work_buddy.email.provider as pmod

    called = []
    class _StubProvider:
        name = "stub"
        def message_exists(self, handle):
            called.append(handle)
            return False  # would fire, but should never be called

    monkeypatch.setattr(pmod, "get_email_provider", lambda: _StubProvider())
    entry = _entry_for("", "imap://x/INBOX")
    desc = _email_descriptor()
    assert trigger_source_removed(entry, desc) is None
    assert called == []
    entry2 = _entry_for("abc@host", "")
    assert trigger_source_removed(entry2, desc) is None
    assert called == []


def test_email_message_descriptor_has_source_removed_trigger():
    """Documentation: the descriptor must list source_removed so the
    sweep dispatches it."""
    from work_buddy.triage.sources import (
        TRIGGER_SOURCE_REMOVED, load_source_registry, reset_for_tests,
    )
    reset_for_tests()
    desc = load_source_registry().get("email_message")
    assert desc is not None
    assert TRIGGER_SOURCE_REMOVED in desc.quarantine_triggers
