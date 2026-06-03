"""Declared agent modes and the lookups that resolve them.

A *mode* is a named, per-session toggle (``dev``, ``knowledge``, …) that gates
capability and workflow availability. Each mode is an inert YAML declaration
under ``declarations/``; this module loads them into :class:`ModeDef` records
and exposes the lookups used by:

- the capability loader and workflow builder, to validate that an
  ``available_when`` gate string references only known mode ids;
- the ``mode_toggle`` capability, to enforce a mode's ``activatable_when``
  constraint before activating it;
- the gateway, to resolve a session's active modes for search/dispatch gating.

Mode ids must match the gate-DSL identifier grammar (``[A-Za-z0-9_]+`` — see
``work_buddy/control/gates.py``) so they can appear as leaves in an
``available_when`` / ``activatable_when`` expression. A mode's
``activatable_when`` is itself a gate-DSL string, evaluated at toggle time
against the *other* active modes, so mutual-exclusion or dependency
constraints are one declaration line rather than code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_DECLARATIONS_DIR = Path(__file__).parent / "declarations"
_MODE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class ModeDef:
    """An inert mode declaration.

    ``activatable_when`` is an optional gate-DSL string evaluated against the
    set of *other* active modes at toggle time; ``None`` means no constraint.
    """

    id: str
    label: str
    description: str
    activatable_when: str | None = None


def _load_modes(declarations_dir: Path) -> dict[str, ModeDef]:
    """Load every ``*.yaml`` mode declaration in ``declarations_dir``.

    Raises ``ValueError`` on a malformed declaration: missing/invalid id,
    duplicate id, or an ``activatable_when`` that fails to parse or references
    an unknown mode id. Fail-loud at load time beats a gate that silently
    evaluates false forever.
    """
    from work_buddy.control import gates

    modes: dict[str, ModeDef] = {}
    for path in sorted(declarations_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        mode_id = str(raw.get("id", "")).strip()
        if not mode_id:
            raise ValueError(f"mode declaration {path.name} is missing 'id'")
        if not _MODE_ID_RE.match(mode_id):
            raise ValueError(
                f"mode id {mode_id!r} in {path.name} is not a valid gate "
                f"identifier ([A-Za-z0-9_]+)"
            )
        if mode_id in modes:
            raise ValueError(f"duplicate mode id {mode_id!r} ({path.name})")
        modes[mode_id] = ModeDef(
            id=mode_id,
            label=str(raw.get("label") or mode_id),
            description=str(raw.get("description") or ""),
            activatable_when=(raw.get("activatable_when") or None),
        )

    known_ids = set(modes)
    for mode in modes.values():
        if mode.activatable_when:
            gate = gates.parse_gate(mode.activatable_when)
            gates.validate(gate, known_ids)
    return modes


@lru_cache(maxsize=1)
def get_known_modes() -> dict[str, ModeDef]:
    """All declared modes keyed by id (cached per process)."""
    return _load_modes(_DECLARATIONS_DIR)


def get_known_mode_ids() -> set[str]:
    """The set of declared mode ids — the known-component set for gate validation."""
    return set(get_known_modes())


def get_mode_def(mode_id: str) -> ModeDef | None:
    """Look up one mode by id, or ``None`` if it is not declared."""
    return get_known_modes().get(mode_id)
