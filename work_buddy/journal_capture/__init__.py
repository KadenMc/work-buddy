"""Production Journal capture domain.

The legacy :mod:`work_buddy.journal` module remains the compatibility layer for
daily Markdown.  This package owns stable capture, entry, effect, and
projection identities.  Exact ingress history is owned by ``work_buddy.sources``;
Journal stores only its domain composition and source references.
"""

from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCapture,
    JournalEntry,
    JournalMigrationRecord,
    ProcessingState,
)
from work_buddy.journal_capture.store import JournalCaptureStore

__all__ = [
    "CaptureMode",
    "CaptureTarget",
    "JournalCapture",
    "JournalCaptureStore",
    "JournalEntry",
    "JournalMigrationRecord",
    "ProcessingState",
]
