"""Provider protocol + factory.

Consumers (capabilities, triage adapter) depend on the protocol; the concrete
backend is selected via ``email.provider`` in config. Test code can register
:class:`work_buddy.email.providers.fake.FakeEmailProvider` and exercise the
full pipeline without Thunderbird.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from work_buddy.email.models import (
    EmailFolder,
    EmailMessage,
    EmailMessageHandle,
    EmailSummary,
)


@runtime_checkable
class EmailProvider(Protocol):
    """Stable interface every email backend must implement.

    Methods raise typed :class:`work_buddy.email.errors.EmailError` subclasses
    on failure so capability wrappers can ``isinstance``-classify and pick
    appropriate retry / display behavior.
    """

    name: str
    """Short identifier for diagnostics, e.g. ``"thunderbird"``."""

    # --- Discovery ---------------------------------------------------------

    def health(self) -> dict:
        """Quick liveness check. Returns the backend's health payload."""

    def list_accounts(self) -> list[dict]:
        """Return one entry per account exposed by the backend."""

    def list_folders(
        self,
        *,
        account_id: str | None = None,
        folder_path: str | None = None,
    ) -> list[EmailFolder]:
        """List folders, optionally scoped to an account or starting URI."""

    # --- Read --------------------------------------------------------------

    def recent_messages(
        self,
        *,
        days_back: int = 2,
        max_results: int = 50,
        unread_only: bool = True,
        flagged_only: bool = False,
        folder_path: str | None = None,
        account_id: str | None = None,
        include_subfolders: bool = True,
    ) -> list[EmailSummary]:
        """Return summaries newer than ``days_back`` days."""

    def search_messages(
        self,
        *,
        query: str,
        max_results: int = 50,
        unread_only: bool = False,
        flagged_only: bool = False,
        folder_path: str | None = None,
        account_id: str | None = None,
        include_subfolders: bool = True,
    ) -> list[EmailSummary]:
        """Token search across subject / sender / preview / recipients."""

    def get_message(
        self,
        handle: EmailMessageHandle,
        *,
        max_body_chars: int = 8000,
    ) -> EmailMessage:
        """Fetch a single message including body."""

    def display_message(
        self,
        handle: EmailMessageHandle,
        *,
        mode: str = "3pane",
    ) -> dict:
        """Open a message in the user's mail UI (provider-specific)."""

    def message_exists(self, handle: EmailMessageHandle) -> bool | None:
        """Check whether the message is still at its captured folder URI.

        Returns:
            ``True`` — message is still present.
            ``False`` — message is gone (moved or deleted).
            ``None`` — could not determine (bridge unreachable, account
                access changed, malformed handle). The caller should
                treat ``None`` as "still live" — never quarantine on
                ambiguity.

        Used by :func:`work_buddy.triage.sources_triggers.trigger_source_removed`
        to fire the ``source_removed`` quarantine reason when the user
        moves an email out of its captured folder.
        """


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_email_provider() -> EmailProvider:
    """Return the configured email provider.

    Selection is driven by ``email.provider`` in config (default
    ``"thunderbird"``). If the chosen provider's transport is unavailable —
    e.g. Thunderbird isn't running — callers should `isinstance`-check the
    error rather than swallowing it; the gateway's tool-probe layer is the
    correct place to short-circuit before reaching this factory.

    Tests can override by importing :class:`FakeEmailProvider` directly and
    bypassing this factory.
    """
    from work_buddy.config import load_config
    from work_buddy.email.errors import EmailProviderDisabled

    cfg = (load_config() or {}).get("email", {}) or {}
    if cfg.get("enabled", True) is False:
        raise EmailProviderDisabled("email.enabled is False in config")

    name = (cfg.get("provider") or "thunderbird").lower()
    if name == "thunderbird":
        from work_buddy.email.providers.thunderbird import ThunderbirdEmailProvider
        return ThunderbirdEmailProvider()
    if name == "fake":
        from work_buddy.email.providers.fake import FakeEmailProvider
        return FakeEmailProvider()
    raise EmailProviderDisabled(f"Unknown email.provider: {name!r}")
