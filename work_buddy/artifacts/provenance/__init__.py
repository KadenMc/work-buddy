"""Provenance flavors — identity / audit / session tagging.

A backend may have no provenance (``provenance=None``) — that just
means session-filtered queries aren't supported.
"""

from __future__ import annotations

from work_buddy.artifacts.provenance.session_tagged import SessionTagged

__all__ = ["SessionTagged"]
