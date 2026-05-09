"""Lifecycle composer + trigger/action sub-packages.

The :class:`Lifecycle` composer itself lives in
:mod:`work_buddy.artifacts.protocol` (it depends only on the Storage
and Trigger / ExpiryAction protocols). This subpackage hosts the
concrete trigger and action implementations.
"""

from __future__ import annotations

from work_buddy.artifacts.lifecycle.actions import Delete, TransformAndDelete
from work_buddy.artifacts.lifecycle.triggers import (
    MtimeWindow,
    PerRecordTtl,
    PerTypeTtl,
    TimeWindow,
)
from work_buddy.artifacts.protocol import Lifecycle  # re-export

__all__ = [
    "Delete",
    "Lifecycle",
    "MtimeWindow",
    "PerRecordTtl",
    "PerTypeTtl",
    "TimeWindow",
    "TransformAndDelete",
]
