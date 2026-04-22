"""Requirement fixers — programmatic and input-required.

Each fixer is called from ``POST /api/control/fix/<req_id>`` after the
endpoint validates that the requirement opts into a fix and after
consent is granted.

Return shape::

    {"ok": bool, "detail": str, "side_effects": list[str]}

``side_effects`` is for the UI to show the user what changed (e.g.
list of files written, dirs created). Optional.

Fixers should be:
  * Idempotent — running twice produces the same end state.
  * Specific in their detail message — say what was created/changed.
  * Honest about partial failure — return ``ok=False`` if anything
    blocks completion; never raise (the dispatcher converts exceptions
    to {ok: False, detail: ...}).

Phase Fix-A ships only the smoke-test fix here; Fix-B/C/D land the
real ones.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Smoke fix (Fix-A) — proves the dispatch pipeline end-to-end
# ---------------------------------------------------------------------------

def fix_data_writable() -> dict[str, Any]:
    """Create the ``data/`` directory at the repo root if it doesn't exist.

    Smoke test for the fix system. The check itself
    (``check_data_writable`` in requirement_checks.py) creates and
    deletes a sentinel file to verify writability — this fix just
    creates the directory if missing. If the dir exists but isn't
    writable (permissions issue), this fix can't help and returns
    ``ok=False``.
    """
    from work_buddy.paths import data_dir

    target = data_dir()
    side_effects: list[str] = []

    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=True)
            side_effects.append(f"Created {target}")
        except OSError as exc:
            return {
                "ok": False,
                "detail": f"Could not create {target}: {exc}",
                "side_effects": side_effects,
            }
    elif not target.is_dir():
        return {
            "ok": False,
            "detail": (
                f"{target} exists but is not a directory — manual cleanup "
                "required (it might be a stray file with that name)."
            ),
            "side_effects": [],
        }

    # Verify writability the same way the check does
    sentinel = target / ".wb-fix-sentinel"
    try:
        sentinel.write_text("ok", encoding="utf-8")
        sentinel.unlink()
    except OSError as exc:
        return {
            "ok": False,
            "detail": (
                f"{target} exists but is not writable (sentinel write "
                f"failed: {exc}). Check filesystem permissions."
            ),
            "side_effects": side_effects,
        }

    return {
        "ok": True,
        "detail": f"data/ directory ready at {target}",
        "side_effects": side_effects,
    }
