"""Last-mile feature-preference admission for cached registry entries.

The MCP registry is intentionally cached and can outlive a preference change.
Every execution surface therefore re-evaluates the entry's declared runtime
requirements immediately before invoking it.  Preference lookup failures are
fail-closed for entries with requirements because an unreadable preference
store cannot prove that a privacy-sensitive integration remains enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeAdmission:
    """Result of checking one registry entry against current preferences."""

    opted_out: tuple[str, ...] = ()
    preference_available: bool = True

    @property
    def allowed(self) -> bool:
        return self.preference_available and not self.opted_out


def evaluate_runtime_admission(entry: Any) -> RuntimeAdmission:
    """Recheck an entry's declared requirements without trusting registry age."""

    requirements = set(getattr(entry, "requires", ()) or ())
    if not requirements:
        return RuntimeAdmission()

    try:
        from work_buddy.health.preferences import is_wanted
        from work_buddy.tools import obsidian_backed_tools

        opted_out = {
            requirement
            for requirement in requirements
            if is_wanted(requirement) is False
        }
        # Keep the last-mile privacy boundary independent of mutable probe
        # cache lifecycle. These are the built-in bridge-backed tool ids; the
        # graph adds any future/transitive registrations when available.
        bridge_backed = obsidian_backed_tools() | {
            "obsidian",
            "datacore",
            "google_calendar",
        }
        if (
            is_wanted("obsidian") is False
            and requirements.intersection(bridge_backed)
        ):
            opted_out.add("obsidian")
    except Exception:
        return RuntimeAdmission(preference_available=False)

    return RuntimeAdmission(opted_out=tuple(sorted(opted_out)))


__all__ = ["RuntimeAdmission", "evaluate_runtime_admission"]
