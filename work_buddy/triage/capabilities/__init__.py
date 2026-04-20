"""Registered triage capabilities.

Each module here exports a single top-level callable registered in
``work_buddy/mcp_server/registry.py``. The module split is deliberate
so each capability stays under ~100 lines and individually importable.
"""

from work_buddy.triage.capabilities.triage_submit import triage_submit
from work_buddy.triage.capabilities.triage_review_pool import triage_review_pool
from work_buddy.triage.capabilities.journal_triage_scan import journal_triage_scan
from work_buddy.triage.capabilities.inline_triage_scan import inline_triage_scan

__all__ = [
    "triage_submit",
    "triage_review_pool",
    "journal_triage_scan",
    "inline_triage_scan",
]
