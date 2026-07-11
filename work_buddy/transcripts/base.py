"""Provider protocol for harness transcript sources."""

from __future__ import annotations

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
        days: int,
        project_filter: list[str] | None = None,
    ) -> Iterable[TranscriptSession]: ...

    def session_from_path(self, path: Path) -> TranscriptSession | None: ...

    def iter_turns(self, session: TranscriptSession) -> Iterable[TranscriptTurn]: ...

    def iter_tool_calls(
        self,
        session: TranscriptSession,
    ) -> Iterable[TranscriptToolCall]: ...
