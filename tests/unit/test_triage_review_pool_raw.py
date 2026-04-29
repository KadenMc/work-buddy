"""Slice 1 follow-up: raw-entry rendering in _build_presentation_from_pool.

Verdicted entries (verdict.recommended_action populated) keep their
original presentation. Raw entries (verdict.raw=True) get a
captured-text-led title, no IR context noise, and a clear rationale
explaining the verdict-pending state.
"""

from __future__ import annotations

from work_buddy.triage.background import PoolEntry
from work_buddy.triage.capabilities.triage_review_pool import (
    _build_presentation_from_pool,
    _raw_intent_from_text,
)


# ---------------------------------------------------------------------------
# _raw_intent_from_text
# ---------------------------------------------------------------------------


def test_strips_leading_capture_marker() -> None:
    text = (
        "> #wb/capture/mobile from Kaden McKeen (@kadenmckeen) at 2026-04-27 10:17\n"
        "GTD: he has a nice sort of is this thing 2 minutes?"
    )
    intent = _raw_intent_from_text(text)
    assert "GTD:" in intent
    assert "wb/capture/mobile" not in intent
    assert "Kaden McKeen" not in intent


def test_keeps_topic_prefix_after_marker() -> None:
    text = "> capture marker\nIdea: weekly ETF tracking habit"
    intent = _raw_intent_from_text(text)
    assert intent.startswith("Idea:")


def test_returns_truncated_when_no_marker() -> None:
    text = "Direct text, no capture marker, just a plain thought."
    intent = _raw_intent_from_text(text, max_chars=30)
    assert intent.startswith("Direct text")
    assert len(intent) <= 31  # 30 chars + ellipsis


def test_handles_empty_text() -> None:
    assert "needs triage" in _raw_intent_from_text("")
    assert "needs triage" in _raw_intent_from_text(None)  # type: ignore[arg-type]


def test_collapses_internal_newlines() -> None:
    text = "> marker\nFirst line of thought\nsecond line of thought"
    intent = _raw_intent_from_text(text)
    assert "\n" not in intent
    assert "First line" in intent


# ---------------------------------------------------------------------------
# _build_presentation_from_pool with raw entries
# ---------------------------------------------------------------------------


def _make_raw_entry(item_id: str, text: str) -> PoolEntry:
    return PoolEntry(
        run_id="bgt_test",
        adapter="journal_triage",
        source="journal_thread",
        item_id=item_id,
        item={
            "id": item_id,
            "text": text,
            "label": text[:30],
            "source": "journal_thread",
            "metadata": {
                "ir_context": [
                    {"display_text": "noisy IR hit 1", "score": 0.05},
                    {"display_text": "noisy IR hit 2", "score": 0.04},
                ],
            },
        },
        verdict={"raw": True},
        created_at="2026-04-27T10:00:00+00:00",
    )


def _make_verdicted_entry(item_id: str, text: str) -> PoolEntry:
    return PoolEntry(
        run_id="bgt_test",
        adapter="journal_triage",
        source="journal_thread",
        item_id=item_id,
        item={
            "id": item_id,
            "text": text,
            "label": text[:30],
            "source": "journal_thread",
            "metadata": {"ir_context": [{"display_text": "old hit", "score": 0.02}]},
        },
        verdict={
            "recommended_action": "create_task",
            "rationale": "Real verdict rationale.",
            "group_intent": "Real intent",
            "confidence": 0.9,
        },
        created_at="2026-04-27T10:00:00+00:00",
    )


def test_raw_entry_intent_is_captured_text_not_ir_context() -> None:
    """The captured text becomes the card title; IR context is dropped
    from the visible context field."""
    entry = _make_raw_entry(
        "j_001",
        "> #wb/capture/mobile from Kaden at 10:17\nGTD: do the thing",
    )
    pres = _build_presentation_from_pool([entry])
    leave_group = pres["groups_by_action"]["leave"][0]
    assert "GTD: do the thing" in leave_group["intent"]
    assert "wb/capture" not in leave_group["intent"]
    # IR context noise must not show up in the rendered context block.
    assert "noisy IR hit" not in leave_group["context"]
    assert leave_group["context"] == ""
    assert leave_group["is_raw"] is True


def test_raw_entry_rationale_explains_pending_state() -> None:
    entry = _make_raw_entry("j_002", "> marker\nplain thought")
    pres = _build_presentation_from_pool([entry])
    leave_group = pres["groups_by_action"]["leave"][0]
    assert "verdict pending" in leave_group["rationale"].lower()
    assert "slice 3" in leave_group["rationale"].lower()


# ---------------------------------------------------------------------------
# _build_presentation_from_pool emits per-source `actions` arrays
# ---------------------------------------------------------------------------


def _make_email_entry(item_id: str, *, provider_message_id: str, folder_path: str) -> PoolEntry:
    """Email pool entry with the metadata fields the open_action descriptor
    references. Mirrors the shape produced by
    :func:`work_buddy.email.triage_adapter._summary_to_item`."""
    return PoolEntry(
        run_id="bgt_test_email",
        adapter="email_triage",
        source="email_message",
        item_id=item_id,
        item={
            "id": item_id,
            "text": "Subject: Hello\nFrom: Alice",
            "label": "Hello",
            "source": "email_message",
            "metadata": {
                "stable_key": f"mid:{provider_message_id}",
                "provider_message_id": provider_message_id,
                "folder_path": folder_path,
                "subject": "Hello",
                "sender": "Alice",
            },
        },
        verdict={
            "recommended_action": "leave",
            "rationale": "Test verdict.",
            "group_intent": "Test intent",
        },
        created_at="2026-04-27T10:00:00+00:00",
    )


def test_email_entry_emits_open_in_thunderbird_action() -> None:
    """The descriptor's open_action should surface as a single action
    on the rendered card. This is the contract the dashboard depends on."""
    entry = _make_email_entry(
        "email_abc",
        provider_message_id="abc@host",
        folder_path="imap://acct1/INBOX",
    )
    pres = _build_presentation_from_pool([entry])
    # The verdict action is "leave"; find the group there.
    leave_groups = pres["groups_by_action"]["leave"]
    assert len(leave_groups) == 1
    items = leave_groups[0]["items"]
    assert len(items) == 1
    actions = items[0].get("actions", [])
    assert len(actions) == 1
    a = actions[0]
    assert a["label"] == "Open in Thunderbird"
    assert a["command_id"] == "work-buddy::email_display"
    assert a["params"] == {
        "provider_message_id": "abc@host",
        "folder_path": "imap://acct1/INBOX",
    }


def test_journal_entry_does_not_get_actions() -> None:
    """Sources without an open_action descriptor should NOT have an
    `actions` key on their items — keeps the existing rendering path
    (raw <a href> link) unchanged for chrome/journal/inline."""
    entry = _make_raw_entry("j_001", "> #wb/capture\nplain thought")
    pres = _build_presentation_from_pool([entry])
    leave_groups = pres["groups_by_action"]["leave"]
    assert leave_groups
    items = leave_groups[0]["items"]
    assert items
    # Either no `actions` key at all (preferred) or empty list — both
    # are valid; the frontend only renders buttons when len > 0.
    assert "actions" not in items[0] or items[0]["actions"] == []


def test_email_entry_with_missing_metadata_omits_action() -> None:
    """If an email entry somehow loses its provider_message_id (legacy
    pool data, schema drift), the action is silently dropped — better
    no button than a broken click."""
    entry = _make_email_entry(
        "email_broken",
        provider_message_id="ok@host",
        folder_path="imap://acct1/INBOX",
    )
    # Strip the metadata field that the action requires
    entry.item["metadata"]["provider_message_id"] = ""
    pres = _build_presentation_from_pool([entry])
    leave_groups = pres["groups_by_action"]["leave"]
    assert leave_groups
    items = leave_groups[0]["items"]
    # No actions emitted because the param resolved to empty
    assert "actions" not in items[0] or items[0]["actions"] == []


def test_verdicted_entry_unchanged_by_raw_branch() -> None:
    """Verdicted entries must keep their existing presentation —
    rationale, group_intent, IR context all preserved."""
    entry = _make_verdicted_entry("j_003", "verdicted text")
    pres = _build_presentation_from_pool([entry])
    create_group = pres["groups_by_action"]["create_task"][0]
    assert create_group["intent"] == "Real intent"
    assert create_group["rationale"] == "Real verdict rationale."
    # IR context still rendered (shows up in the "context" field non-empty)
    assert create_group["context"]  # non-empty
    assert create_group["is_raw"] is False


def test_mixed_raw_and_verdicted_entries() -> None:
    """A pool with both kinds renders each correctly."""
    entries = [
        _make_raw_entry("j_004", "> marker\nraw thought"),
        _make_verdicted_entry("j_005", "verdicted text"),
    ]
    pres = _build_presentation_from_pool(entries)
    raw_group = pres["groups_by_action"]["leave"][0]
    verdicted_group = pres["groups_by_action"]["create_task"][0]
    assert raw_group["is_raw"] is True
    assert verdicted_group["is_raw"] is False
    assert "verdict pending" in raw_group["rationale"].lower()
    assert "verdict pending" not in verdicted_group["rationale"].lower()
