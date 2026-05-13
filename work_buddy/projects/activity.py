"""Activity scoring for projects.

Combines several recency-sensitive signals into a single float so the
dashboard can sort active projects by "how recently are you working
on this." Higher score = more recent + more frequent activity.

Signals (all weighted exponentially-decayed by event age, half-life
14 days by default):

- **Project revisions** — store mutations from the ``project_revisions``
  table. Captures user-authored signal that doesn't necessarily touch
  git or the filesystem (description edits, status changes, dashboard
  saves).
- **Folder mtimes** — most recent file modification time inside each
  non-archived project folder. Catches notes-only work that never
  touches git.
- **Git commits** — every commit in the score window contributes its
  own decayed weight, so a project with 50 commits today scores
  dramatically higher than a project with one. Folder-driven via the
  new attribution path in ``sync.py``.

Git operations are the expensive piece — each repo costs ~300ms.
We cache per-folder for 5 minutes in process so repeated calls during
a single dashboard auto-refresh cycle don't pay the cost twice.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Default weights per signal type. Tuned so a project with one
# recent file edit and one recent revision sorts above a project
# with no signals, and a project with many recent commits sorts
# above a project with one file edit.
_W_REVISION = 1.5
_W_FOLDER_MTIME = 2.0
_W_GIT_COMMIT = 1.0

# How far back to look for events. 60 days × 14-day half-life means
# the oldest events contribute ~5% of a same-day event; further-back
# events round to zero.
_DEFAULT_WINDOW_DAYS = 60
_DEFAULT_HALF_LIFE_DAYS = 14.0

# Per-folder git activity cache. Module-level so repeated calls within
# a short window share results. TTL is 5 minutes — long enough for the
# dashboard's auto-refresh to be free, short enough that hand-driven
# commits during dev surface in the next list refresh.
_GIT_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_GIT_CACHE_TTL_SECONDS = 300.0


def _parse_iso_utc(s: str) -> datetime | None:
    """Parse an ISO 8601 timestamp into a UTC-aware datetime, or None."""
    if not s:
        return None
    try:
        # Python 3.11+ handles ``Z`` suffix natively; for safety strip
        # it and substitute the canonical offset.
        cleaned = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _age_days(event_time: datetime, now: datetime) -> float:
    return (now - event_time).total_seconds() / 86400.0


def _get_git_activity_cached(
    repo_path: Path,
    *,
    window_days: int,
    ttl_seconds: float = _GIT_CACHE_TTL_SECONDS,
) -> dict[str, Any] | None:
    """Read git activity for a folder with a short-lived in-process cache.

    Wraps :func:`work_buddy.projects.sync._read_git_repo_activity`. The
    cache key is the resolved absolute path so symlink and case-normal
    paths share entries.
    """
    try:
        key = str(repo_path.resolve())
    except OSError:
        return None

    now = time.time()
    cached = _GIT_CACHE.get(key)
    if cached and (now - cached[0]) < ttl_seconds:
        return cached[1]

    # Import lazily to avoid pulling sync's module-level dependencies
    # into every caller of activity scoring.
    from work_buddy.projects.sync import _read_git_repo_activity
    try:
        activity = _read_git_repo_activity(
            repo_path, score_window_days=window_days,
        )
    except Exception:
        logger.debug("Git activity read failed for %s", repo_path, exc_info=True)
        activity = None
    _GIT_CACHE[key] = (now, activity)
    return activity


def compute_activity_score(
    project: dict[str, Any],
    *,
    half_life_days: float = _DEFAULT_HALF_LIFE_DAYS,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> float:
    """Return a non-negative activity score for ``project``.

    ``project`` is the dict shape returned by
    :func:`work_buddy.projects.store.get_project` /
    :func:`list_projects` — must include ``id`` and ``folders``.

    The score is a sum of weighted decayed contributions from three
    signal sources; see module docstring. Higher = more recent activity.
    Score of 0 means "no signals in the last ``window_days`` days."
    """
    decay = math.log(2.0) / half_life_days
    now = now or datetime.now(timezone.utc)
    score = 0.0

    # 1. Project revisions — cheap SQL query.
    try:
        from work_buddy.projects import store
        revs = store.list_revisions(project["id"], limit=200)
        for r in revs:
            rev_time = _parse_iso_utc(r.get("created_at", ""))
            if rev_time is None:
                continue
            age = _age_days(rev_time, now)
            if 0 <= age <= window_days:
                score += _W_REVISION * math.exp(-decay * age)
    except Exception:
        logger.debug(
            "Revision-score read failed for project %s",
            project.get("slug"), exc_info=True,
        )

    # 2. Folder mtimes — one stat per non-archived folder.
    for f in project.get("folders", []) or []:
        if f.get("archived"):
            continue
        try:
            mtime = Path(f["path"]).stat().st_mtime
        except OSError:
            continue
        mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        age = _age_days(mtime_dt, now)
        if 0 <= age <= window_days:
            score += _W_FOLDER_MTIME * math.exp(-decay * age)

    # 3. Git commits — folder-driven, cached.
    for f in project.get("folders", []) or []:
        if f.get("archived"):
            continue
        try:
            folder_path = Path(f["path"])
        except OSError:
            continue
        activity = _get_git_activity_cached(folder_path, window_days=window_days)
        if not activity:
            continue
        for d in activity.get("commit_dates", []):
            commit_time = _parse_iso_utc(d)
            if commit_time is None:
                continue
            age = _age_days(commit_time, now)
            if 0 <= age <= window_days:
                score += _W_GIT_COMMIT * math.exp(-decay * age)
        break  # first git folder wins; second repo on same project rare

    return score


def sort_active_by_activity(
    projects: list[dict[str, Any]],
    *,
    half_life_days: float = _DEFAULT_HALF_LIFE_DAYS,
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Sort the ``active`` rows in ``projects`` by activity score DESC.

    Non-active rows keep their incoming order. Mutates each active row
    in place to add an ``activity_score`` field for downstream callers
    (e.g. the dashboard frontend can display a sparkline based on it).
    """
    active = [p for p in projects if p.get("status") == "active"]
    other = [p for p in projects if p.get("status") != "active"]

    now = datetime.now(timezone.utc)
    for p in active:
        p["activity_score"] = compute_activity_score(
            p, half_life_days=half_life_days,
            window_days=window_days, now=now,
        )
    active.sort(
        key=lambda p: (-p.get("activity_score", 0.0), p.get("slug", "")),
    )
    return active + other


def clear_git_cache() -> None:
    """Drop the per-folder git activity cache.

    Test hook. The dashboard never calls this; the cache TTL handles
    expiry naturally.
    """
    _GIT_CACHE.clear()
