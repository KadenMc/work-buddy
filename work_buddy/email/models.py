"""Provider-agnostic email shapes.

Designed so a Gmail / Microsoft Graph / IMAP / Maildir backend could replace
the Thunderbird bridge without changing any consumer of these dataclasses.

Stable keys
-----------
``EmailSummary.stable_key`` is the durable identifier that survives a
Thunderbird restart, an IMAP folder-key shuffle, or a move between folders.
We prefer the RFC 822 ``Message-ID`` when present, then a content hash
covering ``(from, date, subject)`` if not. Operational handles that may
drift (Thunderbird's per-process ``messageKey``, IMAP UID, etc.) belong in
:class:`EmailMessageHandle` and only in metadata — never the stable key.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EmailFolder:
    """One folder visible to the bridge."""

    path: str            # provider-specific folder URI
    name: str
    type: str            # "inbox" | "sent" | "drafts" | "trash" | "junk" | "archive" | "folder" | …
    account_id: str
    total_messages: int
    unread_messages: int
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EmailMessageHandle:
    """An operational handle to one message in the underlying provider.

    Carry alongside the stable key so follow-up calls (display, get-body) can
    address the message by its operational ID where stable keys aren't enough.
    Don't rely on these surviving a provider restart.
    """

    provider_message_id: str   # backend's transient handle (Thunderbird's mime2 messageId)
    folder_path: str           # backend folder URI

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EmailSummary:
    """A header-only view of one message — what triage cards render."""

    stable_key: str
    handle: EmailMessageHandle
    subject: str
    sender: str
    recipients: str
    cc: str
    date: str | None        # ISO-8601 string; None when provider had no date
    folder: str             # human-friendly folder name
    account_id: str
    read: bool
    flagged: bool
    tags: list[str] = field(default_factory=list)
    preview: str = ""
    rfc_message_id: str = ""
    # Provider-side folder type ("inbox" | "sent" | "drafts" | "trash" |
    # "junk" | "archive" | "templates" | "queue" | "folder" | …). Used by
    # the triage adapter's within-run dedup heuristic to prefer the user's
    # primary view of a message when Gmail's labels-as-folders model
    # surfaces the same RFC Message-ID under multiple folder URIs.
    folder_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["handle"] = self.handle.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EmailSummary:
        h = d.get("handle") or {}
        return cls(
            stable_key=d["stable_key"],
            handle=EmailMessageHandle(
                provider_message_id=h.get("provider_message_id", ""),
                folder_path=h.get("folder_path", ""),
            ),
            subject=d.get("subject", ""),
            sender=d.get("sender", ""),
            recipients=d.get("recipients", ""),
            cc=d.get("cc", ""),
            date=d.get("date"),
            folder=d.get("folder", ""),
            account_id=d.get("account_id", ""),
            read=bool(d.get("read", False)),
            flagged=bool(d.get("flagged", False)),
            tags=list(d.get("tags") or []),
            preview=d.get("preview", ""),
            rfc_message_id=d.get("rfc_message_id", ""),
            folder_type=d.get("folder_type", ""),
        )


@dataclass
class EmailMessage:
    """A message body view — fetched on demand by the bridge."""

    summary: EmailSummary
    body: str
    body_format: str         # "text" | "markdown" | "html"
    body_truncated: bool = False
    body_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary.to_dict(),
            "body": self.body,
            "body_format": self.body_format,
            "body_truncated": self.body_truncated,
            "body_length": self.body_length,
        }


def stable_key_for(
    *,
    rfc_message_id: str | None,
    sender: str,
    date: str | None,
    subject: str,
) -> str:
    """Compute a durable key for a message.

    Prefer the RFC 822 ``Message-ID`` when present (the canonical durable
    identifier). When absent (rare; some providers strip it), fall back to a
    short hash of the immutable triplet ``(sender, date, subject)``. Both
    paths are deterministic so re-running collection on the same message
    yields the same key.
    """
    if rfc_message_id and rfc_message_id.strip():
        return f"mid:{rfc_message_id.strip().strip('<>')}"
    h = hashlib.sha1(
        "␟".join([sender or "", date or "", subject or ""]).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:16]
    return f"hash:{h}"
