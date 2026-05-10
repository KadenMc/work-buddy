"""Telegram render URL routing.

Notifications are delivered to Telegram with a deep-link back to the
dashboard. Most notifications correspond to a workflow-view tab and
use the ``/#view/<id>`` pattern. Thread Resolution Requests
deliberately don't create a workflow-view (they'd flood the top-bar
with one tab per wait-state thread, which the Threads tab was built
to replace) — they route into the Threads tab via
``/#tab=threads&tpath=<thread_id>`` instead.

This pins the URL routing so a regression doesn't reintroduce the
silent-fall-through bug where the deep-link landed on a missing
workflow-view and the dashboard's hashchange handler ate it.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from work_buddy.notifications.models import Notification, ResponseType

# The render module imports python-telegram-bot's InlineKeyboardButton /
# InlineKeyboardMarkup. The package isn't installed in the unit-test
# environment (sidecar deploy installs it). Stub it so the module
# imports cleanly — these tests don't exercise keyboard markup,
# only URL routing.
if "telegram" not in sys.modules:
    _telegram_stub = types.ModuleType("telegram")

    class _StubButton:
        def __init__(self, *a, **kw):
            pass

    class _StubMarkup:
        def __init__(self, *a, **kw):
            pass

    _telegram_stub.InlineKeyboardButton = _StubButton
    _telegram_stub.InlineKeyboardMarkup = _StubMarkup
    sys.modules["telegram"] = _telegram_stub


_BASE = "https://example.tailee1d49.ts.net"


@pytest.fixture
def cfg(monkeypatch):
    """Stub ``_cfg`` so the dashboard.external_url is set."""
    from work_buddy.telegram import render as render_mod
    monkeypatch.setattr(
        render_mod, "_cfg",
        {"dashboard": {"external_url": _BASE}},
    )
    return render_mod


def _resolution_request_notif(thread_id: str) -> Notification:
    """Mirrors ``threads.resolution_surface.publish``'s notification shape."""
    return Notification(
        notification_id=f"resolution-{thread_id}",
        title=f"Approve action: {thread_id}",
        body="proposal summary here",
        response_type=ResponseType.NONE.value,
        custom_template={
            "type": "resolution_request",
            "thread_id": thread_id,
            "fsm_state": "awaiting_confirmation",
            "card_kind": "consent",
        },
        expandable=True,
    )


def _ordinary_notif() -> Notification:
    """A non-thread notification (e.g. a consent prompt for an
    autonomous capability call) that DOES correspond to a real
    workflow-view on the dashboard."""
    return Notification(
        notification_id="capability-consent-abc123",
        title="Approve eval_js",
        response_type=ResponseType.CUSTOM.value,
        custom_template={"consent_meta": {"operation": "obsidian.eval_js"}},
        expandable=True,
    )


class TestDashboardUrlFor:
    def test_resolution_request_routes_to_threads_tab(self, cfg):
        notif = _resolution_request_notif("th-abc123")
        url = cfg._dashboard_url_for(notif)
        assert url == f"{_BASE}/#tab=threads&tpath=th-abc123"

    def test_ordinary_notification_uses_workflow_view_pattern(self, cfg):
        notif = _ordinary_notif()
        url = cfg._dashboard_url_for(notif)
        assert url == f"{_BASE}/#view/capability-consent-abc123"

    def test_resolution_request_without_thread_id_falls_back(self, cfg):
        """Defensive: if the custom_template is malformed (no
        thread_id), fall back to the workflow-view URL — silent-fall-
        through is bad, but garbage-in / garbage-out is acceptable."""
        notif = Notification(
            notification_id="resolution-malformed",
            title="malformed",
            response_type=ResponseType.NONE.value,
            custom_template={"type": "resolution_request"},
            expandable=True,
        )
        url = cfg._dashboard_url_for(notif)
        assert url == f"{_BASE}/#view/resolution-malformed"

    def test_no_external_url_returns_none(self, monkeypatch):
        from work_buddy.telegram import render as render_mod
        monkeypatch.setattr(render_mod, "_cfg", {"dashboard": {}})
        notif = _resolution_request_notif("th-xyz")
        assert render_mod._dashboard_url_for(notif) is None


class TestRenderNotificationLinkRouting:
    """End-to-end render check — the rendered Telegram message text
    contains the right URL pattern depending on notification type."""

    def test_thread_resolution_message_links_to_threads_tab(self, cfg):
        # MarkdownV2 escaping inserts backslashes before `=` and other
        # special characters, so check the escaped form that Telegram
        # actually renders.
        notif = _resolution_request_notif("th-abc123")
        rendered = cfg.render_notification(notif)
        assert "tab\\=threads" in rendered["text"]
        assert "tpath\\=th\\-abc123" in rendered["text"]
        assert "view/resolution\\-" not in rendered["text"]

    def test_ordinary_consent_message_links_to_workflow_view(self, cfg):
        notif = _ordinary_notif()
        rendered = cfg.render_notification(notif)
        assert "view/capability\\-consent\\-abc123" in rendered["text"]
        assert "tab\\=threads" not in rendered["text"]
