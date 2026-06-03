"""Mode registry — declared agent modes and their activation constraints.

A *mode* is a named, per-session toggle (e.g. ``dev``, ``knowledge``) that
gates capability/workflow availability via ``available_when`` declarations.
"""

from work_buddy.modes.registry import (
    ModeDef,
    get_known_mode_ids,
    get_known_modes,
    get_mode_def,
)

__all__ = ["ModeDef", "get_known_modes", "get_known_mode_ids", "get_mode_def"]
