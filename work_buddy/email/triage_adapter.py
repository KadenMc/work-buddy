"""Email → TriageItem adapter.

Drops email summaries into the existing source-agnostic triage substrate
(``work_buddy.triage.background.BackgroundTriageProducer``). Slice 1 is
intentionally simple — capture email candidates as raw entries
(``verdict_pass_enabled=False``); the LLM-verdict pass over emails is a
follow-up slice once the dashboard renders ``source="email_message"`` cards.

Stable IDs
----------
``TriageItem.id`` is derived from the message's :func:`stable_key_for`. Email
messages don't change content the way running-notes lines do, so we don't
fight content-hash drift the way the journal adapter does.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Iterable

from work_buddy.email.errors import EmailBridgeUnreachable, EmailError
from work_buddy.email.models import EmailSummary
from work_buddy.email.provider import get_email_provider, EmailProvider
from work_buddy.triage.items import TriageItem

log = logging.getLogger(__name__)

EMAIL_TRIAGE_SOURCE = "email_message"
EMAIL_TRIAGE_ADAPTER_NAME = "email_triage"


def _id_for(summary: EmailSummary) -> str:
    """Triage-pool ID. Stable across re-runs of the same message; opaque enough
    to read on its own (so dashboards don't have to dereference)."""
    return f"email_{_short_hash(summary.stable_key)}"


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _summary_to_item(summary: EmailSummary, *, body_preview: str = "") -> TriageItem:
    """Convert a fetched summary (with optional body) to a triage item."""
    text_parts = [
        f"Subject: {summary.subject}",
        f"From: {summary.sender}",
    ]
    if summary.preview:
        text_parts.append(f"Preview: {summary.preview}")
    if body_preview:
        text_parts.append(f"Body: {body_preview.strip()[:1500]}")
    text = "\n".join(text_parts)
    label = summary.subject or "(no subject)"
    if len(label) > 80:
        label = label[:79] + "…"

    return TriageItem(
        id=_id_for(summary),
        text=text,
        label=label,
        source=EMAIL_TRIAGE_SOURCE,
        # `url` carries a synthetic deep-link the dashboard can render. Future
        # work: a real `tbird:` URL handler. For now we use a non-clickable
        # marker that's still useful in logs and grep.
        url=f"thunderbird:msg/{summary.stable_key}",
        metadata={
            "stable_key": summary.stable_key,
            "rfc_message_id": summary.rfc_message_id,
            "provider_message_id": summary.handle.provider_message_id,
            "folder_path": summary.handle.folder_path,
            "folder": summary.folder,
            "folder_type": summary.folder_type,
            "account_id": summary.account_id,
            "sender": summary.sender,
            "recipients": summary.recipients,
            "cc": summary.cc,
            "subject": summary.subject,
            "date": summary.date,
            "tags": list(summary.tags),
            "read": summary.read,
            "flagged": summary.flagged,
        },
    )


# Folder-type ranking for within-run dedup. Lower = preferred. Gmail's
# labels-as-folders surfaces the same RFC Message-ID under multiple
# folder URIs (INBOX, [Gmail]/All Mail, [Gmail]/Important, plus any
# user labels). When we see duplicates, we want the operational handle
# the user thinks of as "the canonical place this message lives" —
# which for nearly all interactive triage is the inbox view.
_FOLDER_TYPE_PRIORITY = {
    "inbox": 0,
    "drafts": 1,
    "sent": 2,
    "archive": 3,
    "templates": 4,
    "folder": 5,         # user folder / label
    "queue": 6,
    "junk": 7,
    "trash": 8,
}


def _dedup_by_stable_key(summaries: list[EmailSummary]) -> list[EmailSummary]:
    """Drop within-run duplicates, keeping the best operational handle.

    Order of preference:
      1. lowest ``_FOLDER_TYPE_PRIORITY`` (inbox > archive > trash, etc.);
         unknown types fall to the bottom.
      2. ties broken by first-seen order (preserves the provider's natural
         folder-walk order so explicit folder filters still feel
         deterministic).

    The ordering is stable in the sense that re-running on the same input
    yields the same handle for each stable_key — important for triage
    idempotence.
    """
    best_for: dict[str, tuple[int, int, EmailSummary]] = {}  # key → (priority, original_index, summary)
    for idx, s in enumerate(summaries):
        prio = _FOLDER_TYPE_PRIORITY.get(s.folder_type, 99)
        existing = best_for.get(s.stable_key)
        if existing is None or prio < existing[0]:
            best_for[s.stable_key] = (prio, idx, s)
    # Restore the first-seen order so callers' downstream sorts (recent_messages
    # already sorts by date desc) aren't disturbed.
    return [v[2] for v in sorted(best_for.values(), key=lambda t: t[1])]


def collect_email_candidates(
    *,
    provider: EmailProvider | None = None,
    days_back: int = 2,
    max_messages: int = 50,
    unread_only: bool = True,
    folder_path: str | None = None,
    account_id: str | None = None,
    include_body_chars: int = 0,
) -> tuple[list[TriageItem], str | None]:
    """Producer-shaped collect callback.

    Returns ``(items, content_hash)``:
      - ``items``: zero-or-more :class:`TriageItem`s, source=``email_message``.
      - ``content_hash``: short stable hash of the candidate set so the
        producer can skip a re-run if nothing changed since last time.

    On bridge failure (``EmailBridgeUnreachable`` etc.) the function logs and
    returns ``([], None)`` — the producer treats this as "skipped: no items"
    rather than "errored". The tool probe is the right place to hard-fail.
    """
    if provider is None:
        try:
            provider = get_email_provider()
        except EmailError as exc:
            log.info("email_triage: provider unavailable: %s", exc)
            return [], None

    try:
        summaries = provider.recent_messages(
            days_back=days_back,
            # Over-fetch by 3× then dedup-and-cap so Gmail's
            # labels-as-folders duplicates don't eat the user's max_messages
            # budget. The bridge already caps at MAX_RESULTS_CAP=200 so
            # this can't run away.
            max_results=max_messages * 3,
            unread_only=unread_only,
            folder_path=folder_path,
            account_id=account_id,
        )
    except EmailBridgeUnreachable as exc:
        log.info("email_triage: bridge unreachable: %s", exc)
        return [], None
    except EmailError as exc:
        log.warning("email_triage: provider error: %s", exc)
        return [], None

    # Within-run dedup before any body fetch — saves work_buddy.email.get
    # round-trips for messages we'd then drop.
    pre_dedup = len(summaries)
    summaries = _dedup_by_stable_key(summaries)
    if len(summaries) < pre_dedup:
        log.info(
            "email_triage: deduped %d/%d duplicate folder hits "
            "(Gmail labels-as-folders)",
            pre_dedup - len(summaries), pre_dedup,
        )
    summaries = summaries[:max_messages]

    items: list[TriageItem] = []
    for s in summaries:
        body_preview = ""
        if include_body_chars > 0:
            try:
                msg = provider.get_message(s.handle, max_body_chars=include_body_chars)
                body_preview = msg.body or ""
            except EmailError as exc:
                log.debug("email_triage: get_message failed for %s: %s", s.stable_key, exc)
        items.append(_summary_to_item(s, body_preview=body_preview))

    if not items:
        return [], None
    content_hash = _hash_items(items)
    return items, content_hash


def _hash_items(items: Iterable[TriageItem]) -> str:
    """Hash the candidate set. Order-independent (we sort the keys)."""
    keys = sorted((it.metadata.get("stable_key") or it.id) for it in items)
    return hashlib.sha1(
        "␟".join(keys).encode("utf-8"), usedforsecurity=False,
    ).hexdigest()[:16]
