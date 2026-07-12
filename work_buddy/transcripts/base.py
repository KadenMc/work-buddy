"""Provider protocol for harness transcript sources."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Protocol

from work_buddy.transcripts.models import (
    TranscriptSession,
    TranscriptToolCall,
    TranscriptTurn,
)


class TranscriptProvider(Protocol):
    id: str
    harness_id: str
    label: str

    def discover(
        self,
        *,
        days: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        project_filter: list[str] | None = None,
    ) -> Iterable[TranscriptSession]:
        """Enumerate sessions whose file mtime is at/after the window floor.

        ``since`` (aware datetime) is the precise window start; ``days`` is
        day-granular sugar for it. ``until`` is accepted for a symmetric window
        API but is applied by callers on conversation time, not by mtime here
        (a resumed file's mtime is an unreliable upper bound).
        """
        ...

    def session_from_path(self, path: Path) -> TranscriptSession | None: ...

    def iter_turns(self, session: TranscriptSession) -> Iterable[TranscriptTurn]: ...

    def iter_tool_calls(
        self,
        session: TranscriptSession,
    ) -> Iterable[TranscriptToolCall]: ...
