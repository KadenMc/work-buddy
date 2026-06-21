"""Per-source fire-rate tracking + auto-suspend.

A source's firings are logged to ``<state>/<name>.fires.json`` — deliberately
**separate** from the poller's cursor file (``<name>.json``): the poller and the
reaction consumer are different writers with different lifecycles, and sharing one
file would clobber. The log is a list of ISO timestamps, pruned to a rolling
window on every touch, so it never grows unbounded.

If a source fires more than ``max_per_hour`` times in the window, the reaction
consumer suspends it (a flapping watcher is notification spam) and tells the user.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

WINDOW_S = 3600


def _fires_path(name: str, directory: Path | None = None) -> Path:
    from work_buddy.events.sources.state import state_dir

    d = Path(directory) if directory is not None else state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.fires.json"


def _load(name: str, directory: Path | None = None) -> list[str]:
    p = _fires_path(name, directory)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def _prune(fires: list[str], now: datetime, window_s: int = WINDOW_S) -> list[str]:
    cutoff = now - timedelta(seconds=window_s)
    kept: list[str] = []
    for ts in fires:
        try:
            t = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t >= cutoff:
            kept.append(ts)
    return kept


def fires_last_hour(name: str, now: datetime, directory: Path | None = None) -> int:
    """How many times ``name`` has fired within the rolling window ending at ``now``."""
    return len(_prune(_load(name, directory), now))


def record_fire(name: str, now: datetime, directory: Path | None = None) -> int:
    """Record a firing at ``now`` and return the new in-window count."""
    fires = _prune(_load(name, directory), now)
    fires.append(now.isoformat())
    try:
        _fires_path(name, directory).write_text(json.dumps(fires), encoding="utf-8")
    except OSError:  # pragma: no cover — defensive
        logger.warning("ratelimit: could not persist fire-log for %s", name)
    return len(fires)
