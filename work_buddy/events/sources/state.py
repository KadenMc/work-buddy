"""Per-source cursor + last-seen value — a small JSON file under
``.data/event_sources/state/<name>.json``.

State carries ``{last_polled, last_value, last_hash}``. The cursor (the hash)
is advanced **after** a successful poll, so a crash mid-poll just re-fetches
(the spine's inbox makes the re-emit idempotent).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def state_dir() -> Path:
    from work_buddy.events.sources.loader import sources_dir

    d = sources_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(name: str, directory: Path | None = None) -> dict[str, Any]:
    d = directory if directory is not None else state_dir()
    p = d / f"{name}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_state(name: str, state: dict[str, Any], directory: Path | None = None) -> None:
    d = directory if directory is not None else state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(state, default=str), encoding="utf-8")
