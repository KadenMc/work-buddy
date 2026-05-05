"""Quarantine trigger functions for the triage pool sweep (Slice 1).

Each function decides whether a single :class:`ClarifyEntry` should
transition to ``state=quarantined``. They are dispatched by name from
the source descriptor's ``quarantine_triggers`` list.

Trigger contract
----------------

Every trigger has the signature::

    def trigger_<name>(
        entry: ClarifyEntry,
        descriptor: SourceDescriptor,
    ) -> str | None

Return value:

- ``None`` — the trigger did not fire; entry stays in its current state.
- A non-empty string — the trigger fired; the string is the
  human-readable reason that gets persisted to ``quarantine_reason``
  (e.g. ``"source_removed"``).

Exceptions
----------

Triggers MUST be defensive. The sweep runs daily, unattended, against
every pending entry — a single trigger raising would kill the whole
pass. Catch transport / filesystem failures and return ``None``
(treat ambiguous as "still live"); never raise. Use the module logger
to surface degraded checks.

Bridge access
-------------

For sources that need to consult the Obsidian bridge (journal, inline),
use the soft-import pattern: import inside the function so module
import doesn't pay the cost. Treat bridge unavailability as
"can't decide → leave alone."
"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from work_buddy.logging_config import get_logger

if TYPE_CHECKING:
    from work_buddy.clarify.background import ClarifyEntry
    from work_buddy.clarify.sources import SourceDescriptor

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vault_root() -> Path | None:
    """Return the configured vault root, or None if unconfigured.

    Reads top-level ``vault_root`` from ``config.yaml``. Returns
    ``None`` (rather than raising) on any config-load failure so
    triggers running in unattended sweeps degrade gracefully.
    """
    try:
        from work_buddy.config import load_config
        root = (load_config() or {}).get("vault_root")
        return Path(root) if root else None
    except Exception:
        return None


def _journal_dir() -> Path | None:
    """Return ``<vault_root>/<obsidian.journal_dir>``, or None if unset."""
    root = _vault_root()
    if root is None:
        return None
    try:
        from work_buddy.config import load_config
        rel = (
            (load_config() or {})
            .get("obsidian", {})
            .get("journal_dir", "journal")
        )
    except Exception:
        rel = "journal"
    return root / rel


def _resolve_vault_path(rel_path: str) -> Path | None:
    """Resolve a vault-relative path, or absolute if already absolute."""
    if not rel_path:
        return None
    candidate = Path(rel_path)
    if candidate.is_absolute():
        return candidate
    root = _vault_root()
    if root is None:
        return None
    return root / rel_path


def _read_text_safe(path: Path) -> str | None:
    """Read file text or return None on any failure (defensive)."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.debug("trigger: read failed for %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def trigger_source_removed(
    entry: "ClarifyEntry",
    descriptor: "SourceDescriptor",
) -> str | None:
    """Fire when the entry's source no longer resolves.

    Per-source resolution:

    - ``inline`` — the source file at ``item.metadata.file_path``
      no longer exists on disk.
    - ``journal_thread`` — the journal file for ``item.metadata.source_dates``
      (or today's date as fallback) no longer exists.
    - ``chrome_tab`` — the tab id is absent from the live tab ledger
      at ``<data_root>/chrome/tab_ledger.json``.

    Defensive: when the check itself fails (vault root unconfigured,
    ledger unreadable), returns ``None`` (treat as still live) and
    logs at debug. We do NOT quarantine on ambiguity — that would be
    destructive of user data.
    """
    source = entry.source or ""
    meta = (entry.item or {}).get("metadata", {}) or {}

    if source in {"inline"}:
        rel = meta.get("file_path", "")
        path = _resolve_vault_path(rel)
        if path is None:
            return None  # can't decide → leave alone
        if not path.exists():
            return "source_removed"
        return None

    if source == "journal_thread":
        # Journal entries record source_dates as a list (e.g.
        # ["2026-04-19"]) but legacy entries often left it empty
        # and stored ``journal_date`` (singular) instead. Accept
        # both. If neither is present, can't decide → leave alone.
        dates = list(meta.get("source_dates") or [])
        if not dates and meta.get("journal_date"):
            dates = [meta["journal_date"]]
        if not dates:
            return None
        journal_dir = _journal_dir()
        if journal_dir is None:
            return None
        all_gone = True
        for date_str in dates:
            candidate = journal_dir / f"{date_str}.md"
            if candidate.exists():
                all_gone = False
                break
        return "source_removed" if all_gone else None

    if source == "email_message":
        # Ask the email bridge whether the message is still at its
        # captured (provider_message_id, folder_path). If the user
        # moved it to trash, archived it, or deleted it, the bridge's
        # findMessage will fail and we get exists=False → fire.
        #
        # Defensive: the provider returns None when it can't decide
        # (bridge unreachable, account access changed, malformed
        # handle). Treat None as "still live" — never quarantine on
        # ambiguity. The unattended sweep must not punish a brief
        # bridge outage by burning real triage entries.
        provider_msg_id = meta.get("provider_message_id") or ""
        folder_path = meta.get("folder_path") or ""
        if not provider_msg_id or not folder_path:
            return None  # malformed metadata — leave alone
        try:
            from work_buddy.email.errors import EmailError
            from work_buddy.email.models import EmailMessageHandle
            from work_buddy.email.provider import get_email_provider
        except ImportError as exc:
            logger.debug("trigger: email module not importable: %s", exc)
            return None
        try:
            provider = get_email_provider()
        except EmailError as exc:
            logger.debug("trigger: email provider unavailable: %s", exc)
            return None
        try:
            exists = provider.message_exists(EmailMessageHandle(
                provider_message_id=provider_msg_id,
                folder_path=folder_path,
            ))
        except Exception as exc:  # noqa: BLE001 — defensive: never raise from a sweep trigger
            logger.debug(
                "trigger: message_exists raised for %s: %s",
                provider_msg_id, exc,
            )
            return None
        if exists is False:
            return "source_removed"
        # exists is True or None (couldn't decide) → leave alone
        return None

    if source == "chrome_tab":
        from work_buddy.paths import data_dir
        ledger_path = data_dir("chrome") / "tab_ledger.json"
        if not ledger_path.exists():
            return None  # ledger missing — can't decide
        try:
            import json
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("trigger: chrome ledger unreadable: %s", exc)
            return None
        tab_id = meta.get("tab_id") or entry.item_id
        # Ledger shape varies — accept either dict-keyed or list-of-dicts.
        if isinstance(ledger, dict):
            tabs = ledger.get("tabs", ledger)
        else:
            tabs = ledger
        if isinstance(tabs, dict):
            present = tab_id in tabs
        elif isinstance(tabs, list):
            present = any(
                (t.get("id") == tab_id or t.get("tab_id") == tab_id)
                for t in tabs if isinstance(t, dict)
            )
        else:
            present = True
        return None if present else "source_removed"

    # Unknown source — never quarantine.
    return None


def trigger_source_edited_beyond_match(
    entry: "ClarifyEntry",
    descriptor: "SourceDescriptor",
) -> str | None:
    """Fire when the captured text no longer appears in the source.

    Currently scoped to ``journal_thread``. Compares the entry's
    captured ``item.text`` against today's running notes (or the
    entry's source dates) using :class:`difflib.SequenceMatcher` —
    no embedding service dependency. Threshold comes from the
    descriptor's ``config["edit_match_threshold"]`` (default 0.85).

    Defensive: if the journal file isn't readable or the threshold
    can't be evaluated, returns ``None``.

    NOTE: difflib.ratio() is a coarse approximation of cosine
    similarity — good enough for "is the captured text approximately
    still present?" Switching to embedding-based similarity is a
    future upgrade; the dispatch contract stays the same.
    """
    source = entry.source or ""
    if source != "journal_thread":
        return None

    meta = (entry.item or {}).get("metadata", {}) or {}
    captured = (entry.item or {}).get("text", "") or ""
    if not captured:
        return None

    threshold = float(
        (descriptor.config or {}).get("edit_match_threshold", 0.85)
    )

    dates = list(meta.get("source_dates") or [])
    if not dates and meta.get("journal_date"):
        dates = [meta["journal_date"]]
    if not dates:
        return None

    journal_dir = _journal_dir()
    if journal_dir is None:
        return None

    # Best ratio across all source-date journals — if even one still
    # contains the text, leave the entry alone.
    best_ratio = 0.0
    any_readable = False
    for date_str in dates:
        candidate = journal_dir / f"{date_str}.md"
        text = _read_text_safe(candidate)
        if text is None:
            continue
        any_readable = True
        # SequenceMatcher.ratio() is O(n*m) — for short captured
        # snippets against full-day journal files this is fine
        # (~tens of KB; sub-second). For very long captures, fall
        # back to a substring containment check first.
        if captured in text:
            best_ratio = 1.0
            break
        ratio = SequenceMatcher(None, captured, text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio

    if not any_readable:
        return None  # can't decide — leave alone

    return "source_edited_beyond_match" if best_ratio < threshold else None


def trigger_tag_removed(
    entry: "ClarifyEntry",
    descriptor: "SourceDescriptor",
) -> str | None:
    """Fire when the entry's capture tag is no longer on the source paragraph.

    Currently a placeholder — inline captures don't yet write back a
    confirmation tag at capture time. The trigger function is wired
    so that adding tag-write to the inline handler is a one-line
    change (flip ``inline`` source's ``quarantine_triggers`` to
    include ``tag_removed``).

    Until then: returns ``None`` always (no-op).
    """
    # Implementation placeholder — see docstring. Returns None so
    # configuring this trigger today is a no-op rather than a bug.
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_TRIGGER_DISPATCH = {
    "source_removed": trigger_source_removed,
    "source_edited_beyond_match": trigger_source_edited_beyond_match,
    "tag_removed": trigger_tag_removed,
}


def evaluate_triggers(
    entry: "ClarifyEntry",
    descriptor: "SourceDescriptor",
) -> str | None:
    """Run the descriptor's quarantine triggers; return first reason that fires.

    ``None`` means the entry is still live. The order of triggers in
    the descriptor is significant — first fire wins.

    Defensive: any trigger that raises is logged and skipped (treated
    as "did not fire"). The sweep is unattended; one bad trigger
    must not poison the whole run.
    """
    for trigger_name in descriptor.quarantine_triggers:
        fn = _TRIGGER_DISPATCH.get(trigger_name)
        if fn is None:
            logger.warning(
                "evaluate_triggers: unknown trigger %r for source %r",
                trigger_name, descriptor.name,
            )
            continue
        try:
            reason = fn(entry, descriptor)
        except Exception as exc:
            logger.warning(
                "evaluate_triggers: %r raised on entry %s/%s: %s",
                trigger_name, entry.run_id, entry.item_id, exc,
            )
            continue
        if reason:
            return reason
    return None
