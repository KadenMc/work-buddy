"""Filesystem watcher that signals the scheduler when job files change.

Uses ``watchdog`` (kernel-event filesystem notifications: inotify on
Linux, ReadDirectoryChangesW on Windows, kqueue on macOS) to spot
``.md`` create/modify/delete events under each of the scheduler's job
directories. On a hit, the watcher sets ``Scheduler.jobs_reload_pending``
— a ``threading.Event`` that the daemon's main loop waits on. The next
tick sees the event set and calls ``_hot_reload()`` immediately, bypassing
the 30s interval.

All scheduler mutation stays on the main thread; this module only
toggles a flag. That's deliberate — ``_hot_reload`` mutates several
unsynchronised attributes that ``Scheduler.tick`` and
``Scheduler.update_state`` also read, and adding cross-thread mutation
would require locks across all three. The signal-an-event approach
keeps the existing single-writer invariant intact.

The 30s polling reload in ``Scheduler.tick`` stays as a safety net for
the rare cases where filesystem events are dropped (NFS/Docker overlay
mounts; not relevant on local NTFS today, but cheap insurance).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from work_buddy.logging_config import get_logger

if TYPE_CHECKING:
    from work_buddy.sidecar.scheduler.engine import Scheduler

logger = get_logger(__name__)


class _JobFileEventHandler(FileSystemEventHandler):
    """Sets the scheduler's reload flag on any ``.md`` change.

    Kept tiny on purpose: filesystem events arrive on the watchdog
    observer thread, and we want to hand off to the main thread as
    quickly as possible. No file I/O, no logic — just flag the
    scheduler and return.
    """

    def __init__(self, scheduler: "Scheduler") -> None:
        self._scheduler = scheduler

    def _is_relevant(self, event: FileSystemEvent) -> bool:
        if event.is_directory:
            return False
        # ``src_path`` is bytes on some platforms; coerce to str.
        path = str(event.src_path)
        return path.endswith(".md")

    def _signal(self, event: FileSystemEvent, kind: str) -> None:
        if not self._is_relevant(event):
            return
        logger.debug("Job file %s: %s", kind, event.src_path)
        self._scheduler.jobs_reload_pending.set()

    def on_created(self, event: FileSystemEvent) -> None:
        self._signal(event, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._signal(event, "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._signal(event, "deleted")

    def on_moved(self, event: FileSystemEvent) -> None:
        # A rename within the watched dir surfaces as on_moved with both
        # src_path and dest_path; either side mattering is enough to
        # trigger a reload.
        self._signal(event, "moved")


class JobsWatcher:
    """Watch the scheduler's jobs directories and signal on changes.

    Reads the directory list from ``scheduler._jobs_dirs`` at start time.
    If a directory doesn't exist at start, it's logged and skipped (the
    30s safety-net poll still picks up jobs there if it appears later).
    Config-time changes to ``sidecar.user_jobs_dir`` after start are NOT
    reconciled in v1 — the watcher keeps observing the original paths
    until the daemon restarts.
    """

    def __init__(self, scheduler: "Scheduler") -> None:
        self._scheduler = scheduler
        self._observer: Observer | None = None

    def start(self) -> None:
        """Schedule a watch for each existing jobs directory."""
        observer = Observer()
        handler = _JobFileEventHandler(self._scheduler)

        watched: list[Path] = []
        skipped: list[Path] = []
        for path, _source in self._scheduler._jobs_dirs:
            if not path.exists():
                skipped.append(path)
                continue
            observer.schedule(handler, str(path), recursive=False)
            watched.append(path)

        if not watched:
            logger.warning(
                "JobsWatcher: no jobs directories exist; "
                "falling back to scheduler's 30s poll only.",
            )
            self._observer = None
            return

        observer.start()
        self._observer = observer
        logger.info(
            "JobsWatcher started: watching %d director%s (%s)",
            len(watched), "y" if len(watched) == 1 else "ies",
            ", ".join(str(p) for p in watched),
        )
        for path in skipped:
            logger.warning(
                "JobsWatcher: skipping non-existent path %s "
                "(scheduler will still see jobs here on its 30s poll if "
                "the directory is later created).", path,
            )

    def stop(self, *, join_timeout: float = 2.0) -> None:
        """Stop the observer and join its thread."""
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=join_timeout)
        except Exception:
            logger.warning("JobsWatcher: observer stop/join failed", exc_info=True)
        finally:
            self._observer = None
