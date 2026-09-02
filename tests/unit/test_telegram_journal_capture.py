from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from types import SimpleNamespace

from work_buddy.journal_capture.ingress import (
    JournalIngressQueued,
    telegram_ingress_identity,
)
from work_buddy.security.actors import ActorRef
from work_buddy.telegram import handlers


def _actor() -> ActorRef:
    return ActorRef(
        issuer_authority_id="issuer_test_1234",
        subject="local_actor_1234",
        kind="human",
        tenant_scope_id="tenant_test_1234",
    )


def test_capture_command_payload_preserves_content_after_one_delimiter() -> None:
    assert handlers._capture_command_payload("/capture hello") == "hello"
    assert handlers._capture_command_payload("/capture   hello\nworld  ") == "  hello\nworld  "
    assert handlers._capture_command_payload("/capture\r\nhello") == "hello"
    assert handlers._capture_command_payload("/capture") == ""


def test_telegram_identity_is_stable_and_content_free() -> None:
    first = telegram_ingress_identity(
        enrolled_actor=_actor(),
        chat_id=10,
        message_id=20,
        update_id=30,
        user_id=40,
    )
    retry = telegram_ingress_identity(
        enrolled_actor=_actor(),
        chat_id=10,
        message_id=20,
        update_id=30,
        user_id=40,
    )
    other = telegram_ingress_identity(
        enrolled_actor=_actor(),
        chat_id=10,
        message_id=21,
        update_id=31,
        user_id=40,
    )

    assert first == retry
    assert first.client_mutation_id != other.client_mutation_id
    assert first.trusted.inputter == _actor()
    assert first.trusted.inputter_assurance == "allowlisted_telegram_chat"
    assert first.trusted.permitted_purposes == ("journal.materialize",)


def test_telegram_capture_uses_native_source_first_ingress_without_logging_prose(
    monkeypatch,
    caplog,
) -> None:
    private_text = "  private capture\nwith exact spacing  "
    replies: list[str] = []

    async def reply_text(value: str, **_kwargs) -> None:
        replies.append(value)

    message = SimpleNamespace(
        message_id=20,
        date=datetime(2026, 8, 27, 15, 0, tzinfo=UTC),
        text=private_text,
        reply_text=reply_text,
    )
    update = SimpleNamespace(
        effective_message=message,
        message=message,
        effective_chat=SimpleNamespace(id=10),
        effective_user=SimpleNamespace(id=40),
        update_id=30,
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.local_identity_api._authority",
        lambda: SimpleNamespace(enrolled_actor=_actor),
    )
    monkeypatch.setattr(
        "work_buddy.journal_capture.api._services",
        lambda: (object(), object(), object()),
    )
    monkeypatch.setattr(
        "work_buddy.journal_capture.projection.current_day",
        lambda: {"dayId": "journal-day:2026-08-27:America/New_York:04:00"},
    )
    submitted: dict[str, object] = {}

    def submit(_self, **kwargs):
        submitted.update(kwargs)
        return SimpleNamespace(
            capture=SimpleNamespace(capture_id="capture_test_1234"),
            commit=SimpleNamespace(deduplicated=False),
        )

    monkeypatch.setattr(
        "work_buddy.journal_capture.ingress.JournalCaptureIngress.submit",
        submit,
    )
    monkeypatch.setattr(
        "work_buddy.obsidian.vault_writer.write_at_location",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Obsidian write used")),
    )

    with caplog.at_level(logging.INFO, logger="work_buddy.telegram.handlers"):
        handlers._log_inbound(update)
        asyncio.run(handlers._do_capture(update, private_text, SimpleNamespace()))

    assert submitted["exact_text"] == private_text
    assert submitted["input_mode"] == "direct_entry"
    assert submitted["day_id"] == "journal-day:2026-08-27:America/New_York:04:00"
    assert str(submitted["target"]) == "running_notes"
    assert str(submitted["mode"]) == "dumb"
    assert replies == ["Captured to Journal Running Notes."]
    assert private_text not in caplog.text


def test_telegram_capture_reports_durable_cutover_queue_without_logging_prose(
    monkeypatch,
    caplog,
) -> None:
    private_text = "private queued capture"
    replies: list[str] = []

    async def reply_text(value: str, **_kwargs) -> None:
        replies.append(value)

    message = SimpleNamespace(
        message_id=21,
        date=datetime(2026, 8, 27, 15, 1, tzinfo=UTC),
        text=private_text,
        reply_text=reply_text,
    )
    update = SimpleNamespace(
        effective_message=message,
        message=message,
        effective_chat=SimpleNamespace(id=10),
        effective_user=SimpleNamespace(id=40),
        update_id=31,
    )
    monkeypatch.setattr(
        "work_buddy.dashboard.local_identity_api._authority",
        lambda: SimpleNamespace(enrolled_actor=_actor),
    )
    monkeypatch.setattr(
        "work_buddy.journal_capture.api._services",
        lambda: (object(), object(), object()),
    )
    monkeypatch.setattr(
        "work_buddy.journal_capture.projection.current_day",
        lambda: {"dayId": "journal-day:2026-08-27:America/New_York:04:00"},
    )

    def submit(_self, **_kwargs):
        raise JournalIngressQueued(SimpleNamespace(deduplicated=False))

    monkeypatch.setattr(
        "work_buddy.journal_capture.ingress.JournalCaptureIngress.submit",
        submit,
    )

    with caplog.at_level(logging.INFO, logger="work_buddy.telegram.handlers"):
        asyncio.run(handlers._do_capture(update, private_text, SimpleNamespace()))

    assert replies == ["Saved and queued for Journal while maintenance finishes."]
    assert private_text not in caplog.text
    assert "Journal capture failed" not in caplog.text
