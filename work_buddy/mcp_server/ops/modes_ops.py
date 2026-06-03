"""Mode-domain ops.

``mode_toggle`` flips a per-session mode on or off, mirroring ``task_toggle``'s
``None``-flips / explicit-set API. Activating a mode is gated by its
``activatable_when`` constraint (evaluated against the other active modes). Mode
state lives on the agent's session manifest; the gateway injects the agent's
session id so the toggle targets the calling agent, not the server process.
"""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op


def mode_toggle(
    mode_id: str,
    active: bool | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Toggle a session mode on or off.

    ``active=None`` flips the current state; ``True`` / ``False`` set it
    explicitly. Returns the full active-mode set after the change so a caller
    never needs a separate status call. Activation is refused when the mode's
    ``activatable_when`` constraint is not satisfied by the other active modes.
    """
    from work_buddy.agent_session import get_active_modes, set_active_modes
    from work_buddy.control import gates
    from work_buddy.modes.registry import get_mode_def

    mode = get_mode_def(mode_id)
    if mode is None:
        return {
            "error": f"Unknown mode {mode_id!r}.",
            "denied_by": "unknown_mode",
            "active_modes": sorted(get_active_modes(agent_session_id)),
        }

    current = get_active_modes(agent_session_id)
    was_active = mode_id in current
    target = (not was_active) if active is None else bool(active)

    if target == was_active:
        # Idempotent no-op — report the unchanged state.
        return {
            "mode_id": mode_id,
            "active": was_active,
            "previous": was_active,
            "active_modes": sorted(current),
        }

    if target:
        # Activation: enforce the mode's constraint against the OTHER active
        # modes (the set this mode would be joining).
        if mode.activatable_when:
            others = current - {mode_id}
            gate = gates.parse_gate(mode.activatable_when)
            if not gates.evaluate(gate, others):
                return {
                    "error": (
                        f"Mode {mode_id!r} cannot be activated: its constraint "
                        f"{mode.activatable_when!r} is not satisfied by the "
                        f"active modes."
                    ),
                    "denied_by": "activation_constraint",
                    "constraint": mode.activatable_when,
                    "active_modes": sorted(current),
                }
        new_modes = current | {mode_id}
    else:
        new_modes = current - {mode_id}

    set_active_modes(new_modes, session_id=agent_session_id)
    return {
        "mode_id": mode_id,
        "active": target,
        "previous": was_active,
        "active_modes": sorted(new_modes),
    }


def _register() -> None:
    register_op("op.wb.mode_toggle", mode_toggle)


_register()
