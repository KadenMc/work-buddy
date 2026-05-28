"""SessionTagged provenance — extract the creating session id from a record.

Three consumers tag records with sessions, with three different
schemas:

* **filesystem** artifacts have a ``session_id`` field on the meta dict
* **messaging** rows have ``sender_session`` and ``recipient_session``
  columns
* **agent_sessions** are themselves session directories — the dir name
  encodes the session

``SessionTagged`` accepts either a single field name or a list of
candidate field names; the first non-null value wins. The single-field
form covers filesystem and most consumers; the list form covers
messaging.

Capabilities declared: SESSION_TAGGED.
"""

from __future__ import annotations

from typing import Any

from work_buddy.artifacts.protocol import StorageTrait


class SessionTagged:
    """Per-record session tagging.

    Args:
        session_field: Single field name to read on each record.
            (Convenience for the common case; equivalent to passing
            ``session_columns=[session_field]``.)
        session_columns: Ordered list of candidate field names. The
            first that yields a non-empty value on a given record is
            returned.
    """

    capabilities = frozenset({StorageTrait.SESSION_TAGGED})

    def __init__(
        self,
        *,
        session_field: str | None = None,
        session_columns: list[str] | None = None,
    ) -> None:
        if session_field is not None and session_columns is not None:
            raise ValueError(
                "Pass either session_field OR session_columns, not both."
            )
        if session_field is None and session_columns is None:
            raise ValueError(
                "SessionTagged requires session_field or session_columns."
            )
        self._columns: list[str] = (
            list(session_columns) if session_columns is not None
            else [session_field]  # type: ignore[list-item]
        )

    def get_session(self, record: dict[str, Any]) -> str | None:
        for col in self._columns:
            val = record.get(col)
            if val:
                return str(val)
        return None
