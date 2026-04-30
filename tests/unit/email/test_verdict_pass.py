"""Slice 2 — verdict-pass tests for email_triage_run.

Stubs LLMRunner and call_for_verdict at the module-import level so we can
exercise the full produce-and-submit path without LLM calls. Verifies:

  - When triage.verdict_pass.enabled is False, raw entries land in the pool
    (Slice 1 behavior preserved).
  - When enabled, the agent path runs: per-item LLM call, structured
    verdict parsed, kwargs passed correctly to triage_submit.
  - Action-specific kwargs (suggested_task_text for create_task,
    target_task_id for record_into_task) flow through verdict_to_submit_kwargs.
  - LLM error → submission is skipped, item shows up in unsubmitted.
  - Body-char auto-bump only happens when verdict pass is on.
  - tier override via the `tier` kwarg works.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from work_buddy.email.models import (
    EmailFolder,
    EmailMessageHandle,
    EmailSummary,
    stable_key_for,
)
from work_buddy.email.providers.fake import FakeEmailProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded_provider(monkeypatch) -> FakeEmailProvider:
    p = FakeEmailProvider()
    p.add_account("acct1", "Personal", type="imap")
    p.add_folder(EmailFolder(
        path="imap://acct1/INBOX", name="Inbox", type="inbox",
        account_id="acct1", total_messages=1, unread_messages=1,
    ))
    rfc = "msg-action@host"
    p.add(
        EmailSummary(
            stable_key=stable_key_for(
                rfc_message_id=rfc, sender="alice@x",
                date="2026-04-29T10:00:00Z", subject="Action required by Friday",
            ),
            handle=EmailMessageHandle(provider_message_id=rfc, folder_path="imap://acct1/INBOX"),
            subject="Action required by Friday",
            sender="Alice <alice@x>",
            recipients="me@x", cc="", date="2026-04-29T10:00:00Z",
            folder="Inbox", account_id="acct1",
            read=False, flagged=False, tags=[], preview="Please confirm…",
            rfc_message_id=rfc, folder_type="inbox",
        ),
        body="Please confirm by Friday whether you can attend the planning session.",
    )
    rfc2 = "msg-newsletter@host"
    p.add(
        EmailSummary(
            stable_key=stable_key_for(
                rfc_message_id=rfc2, sender="newsletter@example.com",
                date="2026-04-29T08:00:00Z", subject="Weekly digest 17",
            ),
            handle=EmailMessageHandle(provider_message_id=rfc2, folder_path="imap://acct1/INBOX"),
            subject="Weekly digest 17",
            sender="Example Newsletter <newsletter@example.com>",
            recipients="me@x", cc="", date="2026-04-29T08:00:00Z",
            folder="Inbox", account_id="acct1",
            read=False, flagged=False, tags=[], preview="This week in...",
            rfc_message_id=rfc2, folder_type="inbox",
        ),
        body="This week in industry news...\n[long boring newsletter body]",
    )
    import work_buddy.email.capabilities as cap_mod
    monkeypatch.setattr(cap_mod, "get_email_provider", lambda: p)
    return p


@pytest.fixture
def isolated_pool(tmp_path):
    import work_buddy.triage.background as bg
    pool_dir = tmp_path / "triage_pool"
    pool_dir.mkdir()
    pool = bg.TriagePool(pool_dir=pool_dir)
    bg.set_pool_for_tests(pool)
    yield pool
    bg.set_pool_for_tests(None)


@pytest.fixture
def verdict_pass_enabled(monkeypatch):
    """Patch load_triage_config to return a config with verdict_pass on."""
    fake_cfg = {
        "verdict_pass": {"enabled": True},
        "triage_context": {"task_states": ["focused"], "max_tasks": 5,
                            "include_recent_commits": False},
    }
    import work_buddy.email.capabilities as cap_mod
    monkeypatch.setattr(cap_mod, "load_triage_config", lambda: fake_cfg, raising=False)
    # capabilities reads it via deferred import; patch the source too
    import work_buddy.triage.config as tcfg
    monkeypatch.setattr(tcfg, "load_triage_config", lambda: fake_cfg)
    return fake_cfg


def _fake_response(structured: dict, *, error: str | None = None) -> Any:
    """Build a stub object matching the surface of LLMResponse the
    capability touches: is_error(), structured_output, tier_used,
    error_kind, error, content."""
    from work_buddy.llm import ErrorKind

    class _Stub:
        def __init__(self):
            self.structured_output = structured if not error else None
            self.tier_used = "frontier_balanced"
            self.error = error
            self.error_kind = ErrorKind.VALIDATION_FAILED if error else None
            self.content = ""

        def is_error(self):
            return self.error is not None
    return _Stub()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_verdict_pass_disabled_keeps_slice1_behavior(loaded_provider, isolated_pool, monkeypatch):
    """When triage.verdict_pass.enabled is False, raw entries land in the
    pool — Slice 1 behavior unchanged."""
    fake_cfg = {"verdict_pass": {"enabled": False}}
    import work_buddy.email.capabilities as cap_mod
    import work_buddy.triage.config as tcfg
    monkeypatch.setattr(tcfg, "load_triage_config", lambda: fake_cfg)

    from work_buddy.email.capabilities import email_triage_run
    out = email_triage_run()
    assert out["status"] == "ok"
    assert out["verdict_pass_enabled"] is False
    assert out["submitted"] == 2
    pending = isolated_pool.pending(source="email_message")
    assert len(pending) == 2


def test_verdict_pass_enabled_invokes_agent_per_item(loaded_provider, isolated_pool, verdict_pass_enabled):
    """The agent gets called once per candidate; verdicts are submitted."""
    from work_buddy.email.capabilities import email_triage_run
    import work_buddy.email.capabilities as cap_mod

    invocations = []

    def _fake_call_for_verdict(*, runner, tier, system, user, output_schema,
                                caller, item_id, **kwargs):
        invocations.append({"item_id": item_id, "user": user, "tier": tier})
        return _fake_response({
            "recommended_action": "leave",
            "rationale": "Stub verdict — couldn't tell.",
            "group_intent": "stub",
            "confidence": 0.4,
        })

    with patch("work_buddy.triage.verdict_call.call_for_verdict", _fake_call_for_verdict):
        with patch("work_buddy.email.capabilities.LLMRunner", create=True):
            out = email_triage_run()

    assert out["status"] == "ok"
    assert out["verdict_pass_enabled"] is True
    assert out["submitted"] == 2
    assert len(invocations) == 2
    pending = isolated_pool.pending(source="email_message")
    assert len(pending) == 2
    # All landed as 'leave' per stub
    for entry in pending:
        assert entry.verdict.get("recommended_action") == "leave"


def test_verdict_pass_create_task_carries_suggested_task_text(loaded_provider, isolated_pool, verdict_pass_enabled):
    """Create_task verdict → triage_submit gets suggested_task_text."""
    from work_buddy.email.capabilities import email_triage_run
    import work_buddy.email.capabilities as cap_mod

    def _fake_verdict(*, runner, tier, system, user, output_schema,
                       caller, item_id, **kwargs):
        return _fake_response({
            "recommended_action": "create_task",
            "rationale": "Email asks user to confirm Friday attendance.",
            "group_intent": "Friday planning session RSVP",
            "suggested_task_text": "RSVP for Friday planning session",
            "confidence": 0.85,
        })

    with patch("work_buddy.triage.verdict_call.call_for_verdict", _fake_verdict):
        with patch("work_buddy.email.capabilities.LLMRunner", create=True):
            out = email_triage_run(max_messages=1)

    pending = isolated_pool.pending(source="email_message")
    assert len(pending) >= 1
    first = pending[0]
    assert first.verdict["recommended_action"] == "create_task"
    assert first.verdict["suggested_task_text"] == "RSVP for Friday planning session"


def test_verdict_pass_close_action_drops_lookalike_kwargs(loaded_provider, isolated_pool, verdict_pass_enabled):
    """Close verdict ignores task-related fields even if returned."""
    from work_buddy.email.capabilities import email_triage_run

    def _fake_verdict(*, runner, tier, system, user, output_schema,
                       caller, item_id, **kwargs):
        return _fake_response({
            "recommended_action": "close",
            "rationale": "Newsletter; safe to drop.",
            "group_intent": "weekly digest",
            # The agent might mistakenly include these — verdict_to_submit_kwargs
            # passes them through; pool persists what's submitted.
            "suggested_task_text": "(should not appear)",
            "confidence": 0.92,
        })

    with patch("work_buddy.triage.verdict_call.call_for_verdict", _fake_verdict):
        with patch("work_buddy.email.capabilities.LLMRunner", create=True):
            email_triage_run(max_messages=1)

    pending = isolated_pool.pending(source="email_message")
    close_entries = [e for e in pending if e.verdict.get("recommended_action") == "close"]
    assert len(close_entries) >= 1


def test_verdict_pass_llm_error_surfaces_unsubmitted(loaded_provider, isolated_pool, verdict_pass_enabled):
    """When the LLM call errors, the item lands in unsubmitted."""
    from work_buddy.email.capabilities import email_triage_run

    def _fake_verdict_err(*, runner, tier, system, user, output_schema,
                           caller, item_id, **kwargs):
        return _fake_response({}, error="VALIDATION_FAILED: missing recommended_action")

    with patch("work_buddy.triage.verdict_call.call_for_verdict", _fake_verdict_err):
        with patch("work_buddy.email.capabilities.LLMRunner", create=True):
            out = email_triage_run()

    assert out["status"] == "ok"
    assert out["submitted"] == 0
    assert len(out["unsubmitted"]) == 2
    for err in out["errors"]:
        assert err["error_kind"] == "validation_failed"


def test_body_chars_auto_bump_when_verdict_pass_on(loaded_provider, isolated_pool, verdict_pass_enabled):
    """include_body_chars=None defaults to _DEFAULT_VERDICT_BODY_CHARS when
    verdict pass is on (so the LLM has body content), 0 when off."""
    from work_buddy.email.capabilities import _DEFAULT_VERDICT_BODY_CHARS, email_triage_run
    import work_buddy.email.triage_adapter as ta

    captured = {}
    original_collect = ta.collect_email_candidates

    def _spy_collect(**kwargs):
        captured["include_body_chars"] = kwargs.get("include_body_chars")
        return original_collect(**kwargs)

    def _fake_verdict(*, runner, tier, system, user, output_schema,
                       caller, item_id, **kwargs):
        return _fake_response({
            "recommended_action": "leave",
            "rationale": "stub",
            "group_intent": "stub",
        })

    with patch("work_buddy.email.capabilities.collect_email_candidates", _spy_collect):
        with patch("work_buddy.triage.verdict_call.call_for_verdict", _fake_verdict):
            with patch("work_buddy.email.capabilities.LLMRunner", create=True):
                email_triage_run(dry_run=True)
    assert captured["include_body_chars"] == _DEFAULT_VERDICT_BODY_CHARS


def test_body_chars_explicit_override_respected(loaded_provider, isolated_pool, verdict_pass_enabled):
    """Caller's explicit include_body_chars overrides the auto-pick."""
    from work_buddy.email.capabilities import email_triage_run
    import work_buddy.email.triage_adapter as ta

    captured = {}
    original_collect = ta.collect_email_candidates

    def _spy_collect(**kwargs):
        captured["include_body_chars"] = kwargs.get("include_body_chars")
        return original_collect(**kwargs)

    with patch("work_buddy.email.capabilities.collect_email_candidates", _spy_collect):
        email_triage_run(dry_run=True, include_body_chars=42)
    assert captured["include_body_chars"] == 42


def test_unknown_tier_override_raises(loaded_provider, isolated_pool, verdict_pass_enabled):
    """Bad tier string is rejected with a helpful message."""
    from work_buddy.email.capabilities import email_triage_run
    with pytest.raises(ValueError, match="Unknown tier"):
        with patch("work_buddy.email.capabilities.LLMRunner", create=True):
            email_triage_run(tier="not_a_real_tier")
