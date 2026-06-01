"""Regression guard: every raw log directory is governed by a LOG_FILES artifact.

Orphaned logs accumulate when a raw-log directory is written continuously but
registered with *no* reaping artifact — the failure mode this guards against.
If a future change adds a new raw-log directory (or drops one of these
registrations), extend ``RAW_LOG_DIRS`` and keep the invariant: the directory
is covered by a ``DirShape.LOG_FILES`` artifact with an ``MtimeWindow`` trigger.

(`logs-global` is included as the pre-existing exemplar; it intentionally has no
live-log pin because nothing keeps a live handle on `.data/logs/`.)
"""

from __future__ import annotations

import pytest

from work_buddy.artifacts import MtimeWindow
from work_buddy.artifacts.backends.directory_tree import DirShape
from work_buddy.artifacts.default_registrations import (
    _is_live_log_file,
    register_default_artifacts,
)
from work_buddy.artifacts.registry import get_artifact
from work_buddy.paths import data_dir

# name -> (data_dir subpath, expects a live-log pin?)
RAW_LOG_DIRS = {
    "service-logs": ("runtime/service_logs", True),
    "agents-logs": ("agents/logs", True),
    "logs-global": ("logs", False),
}


@pytest.mark.parametrize("name", sorted(RAW_LOG_DIRS))
def test_raw_log_dir_registered_with_log_files_artifact(name):
    register_default_artifacts()  # idempotent by name
    subpath, _ = RAW_LOG_DIRS[name]
    art = get_artifact(name)
    assert art is not None, f"{name} artifact is not registered"
    assert art.storage._root == data_dir(subpath)
    assert art.storage._shape == DirShape.LOG_FILES
    assert isinstance(art.lifecycle.trigger, MtimeWindow)


@pytest.mark.parametrize(
    "name", sorted(n for n, (_, pin) in RAW_LOG_DIRS.items() if pin)
)
def test_live_log_is_pinned(name):
    register_default_artifacts()
    art = get_artifact(name)
    # The shared name-based predicate keeps each dir's live <name>.log.
    assert art.lifecycle.retention_predicate is _is_live_log_file


@pytest.mark.parametrize("name", ["service-logs", "agents-logs"])
def test_retention_window_is_seven_days(name):
    register_default_artifacts()
    art = get_artifact(name)
    assert art.lifecycle.trigger._max_age_days == 7
