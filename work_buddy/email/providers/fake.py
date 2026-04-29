"""In-memory email provider for tests, dry runs, and local exploration.

Lets the entire triage pipeline (collection → adapter → background producer →
pool) be exercised without Thunderbird. Tests construct a provider with
fixture summaries; production never selects this provider unless
``email.provider: fake`` is set explicitly in config.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from work_buddy.email.errors import EmailMessageNotFound
from work_buddy.email.models import (
    EmailFolder,
    EmailMessage,
    EmailMessageHandle,
    EmailSummary,
)


class FakeEmailProvider:
    """Provider with a hand-loaded message store. No network, no I/O.

    Use ``add(summary, body=...)`` to populate. ``health()`` is always OK so
    tests don't have to mock probe behavior unless they're specifically
    testing failure modes.
    """

    name = "fake"

    def __init__(self) -> None:
        self._summaries: dict[str, EmailSummary] = {}      # stable_key → summary
        self._bodies: dict[str, str] = {}                  # stable_key → body
        self._accounts: list[dict] = []
        self._folders: list[EmailFolder] = []
        self._display_log: list[tuple[EmailMessageHandle, str]] = []

    # --- Mutators (test-only) ---------------------------------------------

    def add_account(
        self,
        account_id: str,
        name: str,
        *,
        type: str = "imap",
        identities: list[dict] | None = None,
    ) -> None:
        self._accounts.append({
            "id": account_id, "name": name, "type": type,
            "allowed": True, "identities": identities or [],
        })

    def add_folder(self, folder: EmailFolder) -> None:
        self._folders.append(folder)

    def add(self, summary: EmailSummary, *, body: str = "") -> None:
        if summary.stable_key in self._summaries:
            raise ValueError(f"duplicate stable_key: {summary.stable_key}")
        self._summaries[summary.stable_key] = summary
        self._bodies[summary.stable_key] = body

    def add_many(self, items: Iterable[tuple[EmailSummary, str]]) -> None:
        for summary, body in items:
            self.add(summary, body=body)

    @property
    def display_log(self) -> list[tuple[EmailMessageHandle, str]]:
        return list(self._display_log)

    # --- EmailProvider ----------------------------------------------------

    def health(self) -> dict:
        return {"ok": True, "plugin": "fake", "protocol_version": "0.0.0",
                "accounts_allowed": len(self._accounts),
                "accessible_accounts": len(self._accounts)}

    def list_accounts(self) -> list[dict]:
        return [dict(a) for a in self._accounts]

    def list_folders(self, *, account_id=None, folder_path=None):
        out = []
        for f in self._folders:
            if account_id and f.account_id != account_id:
                continue
            if folder_path and not f.path.startswith(folder_path):
                continue
            out.append(replace(f))
        return out

    def _filter(self, summaries, *, unread_only, flagged_only,
                folder_path, account_id):
        for s in summaries:
            if unread_only and s.read:
                continue
            if flagged_only and not s.flagged:
                continue
            if account_id and s.account_id != account_id:
                continue
            if folder_path and not s.handle.folder_path.startswith(folder_path):
                continue
            yield s

    def recent_messages(self, *, days_back=2, max_results=50, unread_only=True,
                        flagged_only=False, folder_path=None, account_id=None,
                        include_subfolders=True) -> list[EmailSummary]:
        # `days_back` ignored — fixture data is timeless. Tests assert via the
        # other filters and ordering.
        items = list(self._summaries.values())
        items = list(self._filter(items, unread_only=unread_only,
                                  flagged_only=flagged_only,
                                  folder_path=folder_path,
                                  account_id=account_id))
        items.sort(key=lambda s: s.date or "", reverse=True)
        return items[:max_results]

    def search_messages(self, *, query, max_results=50, unread_only=False,
                        flagged_only=False, folder_path=None, account_id=None,
                        include_subfolders=True) -> list[EmailSummary]:
        tokens = (query or "").lower().split()
        out = []
        for s in self._filter(self._summaries.values(),
                              unread_only=unread_only, flagged_only=flagged_only,
                              folder_path=folder_path, account_id=account_id):
            blob = " ".join([s.subject, s.sender, s.preview, s.recipients]).lower()
            if all(t in blob for t in tokens):
                out.append(s)
        out.sort(key=lambda s: s.date or "", reverse=True)
        return out[:max_results]

    def get_message(self, handle, *, max_body_chars=8000) -> EmailMessage:
        # Lookup by handle: prefer matching by both folder + provider id,
        # else fall back to provider id alone (tests sometimes only set ids).
        for key, summary in self._summaries.items():
            if (summary.handle.provider_message_id == handle.provider_message_id
                    and (not handle.folder_path
                         or summary.handle.folder_path == handle.folder_path)):
                body = self._bodies.get(key, "")
                truncated = len(body) > max_body_chars
                return EmailMessage(
                    summary=summary,
                    body=body[:max_body_chars] if truncated else body,
                    body_format="text",
                    body_truncated=truncated,
                    body_length=len(body),
                )
        raise EmailMessageNotFound(
            f"fake: no message with provider_message_id={handle.provider_message_id!r}"
        )

    def display_message(self, handle, *, mode="3pane") -> dict:
        # Verify the handle resolves; record the call for tests.
        for summary in self._summaries.values():
            if summary.handle.provider_message_id == handle.provider_message_id:
                self._display_log.append((handle, mode))
                return {"ok": True, "mode": mode, "subject": summary.subject}
        raise EmailMessageNotFound(
            f"fake: no message with provider_message_id={handle.provider_message_id!r}"
        )

    def message_exists(self, handle) -> bool | None:
        """Existence check: must match provider_message_id AND folder_path."""
        for summary in self._summaries.values():
            if (summary.handle.provider_message_id == handle.provider_message_id
                    and summary.handle.folder_path == handle.folder_path):
                return True
        return False

    # Test helper — simulate a message moving / being deleted between
    # capture and the next pool sweep.
    def remove(self, *, provider_message_id: str, folder_path: str) -> bool:
        for key, summary in list(self._summaries.items()):
            if (summary.handle.provider_message_id == provider_message_id
                    and summary.handle.folder_path == folder_path):
                del self._summaries[key]
                self._bodies.pop(key, None)
                return True
        return False
