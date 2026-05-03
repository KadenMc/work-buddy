"""v5 Stage 4.11 — per-action context status."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.threads import context_status as cs


class TestContextStatus:
    def test_universally_available_token(self):
        # @filesystem has empty tool_ids → always available
        r = cs.context_status("@filesystem")
        assert r["available"] is True
        assert r["kind"] == "always"

    def test_user_only_token(self):
        r = cs.context_status("@physical")
        assert r["available"] is False
        assert r["kind"] == "user_only"

    def test_unknown_token(self):
        r = cs.context_status("@unknown_token_zzz")
        assert r["available"] is False
        assert r["kind"] == "unknown"
        assert "unknown" in r["reason"].lower()

    def test_probe_gated_available(self):
        with patch("work_buddy.tools.is_tool_available", return_value=True):
            r = cs.context_status("@email_send")
        assert r["available"] is True
        assert r["kind"] == "probe_gated"

    def test_probe_gated_unavailable(self):
        with patch("work_buddy.tools.is_tool_available", return_value=False):
            r = cs.context_status("@email_send")
        assert r["available"] is False
        assert r["kind"] == "probe_gated"
        assert "thunderbird" in r["reason"]

    def test_bulk(self):
        with patch("work_buddy.tools.is_tool_available", return_value=True):
            results = cs.context_statuses(["@filesystem", "@email_send", "@physical"])
        assert len(results) == 3
        assert results[0]["kind"] == "always"
        assert results[1]["kind"] == "probe_gated"
        assert results[2]["kind"] == "user_only"

    def test_bulk_empty(self):
        assert cs.context_statuses([]) == []
        assert cs.context_statuses(None) == []
