"""Backend implementations for the artifact lifecycle system.

Each backend implements the ``Storage`` protocol from
:mod:`work_buddy.artifacts.protocol`. Backends are deliberately
flat — no inheritance hierarchy across backends, just a shared protocol
they each conform to. Consumers compose a backend with a lifecycle and
optional provenance to register an Artifact.
"""

from __future__ import annotations

from work_buddy.artifacts.backends.directory_tree import (
    DirectoryTreeStorage,
    DirShape,
)
from work_buddy.artifacts.backends.filesystem import FilesystemStorage
from work_buddy.artifacts.backends.json_records import (
    JsonRecordsShape,
    JsonRecordsStorage,
)
from work_buddy.artifacts.backends.jsonl import JsonlStorage
from work_buddy.artifacts.backends.sqlite_rollup import SqliteRollupStorage
from work_buddy.artifacts.backends.sqlite_rows import SqliteRowsStorage

__all__ = [
    "DirShape",
    "DirectoryTreeStorage",
    "FilesystemStorage",
    "JsonRecordsShape",
    "JsonRecordsStorage",
    "JsonlStorage",
    "SqliteRollupStorage",
    "SqliteRowsStorage",
]
