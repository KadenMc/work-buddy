"""Feature preferences — what the user wants enabled or disabled.

Preferences live in config.local.yaml under a ``features:`` key:

.. code-block:: yaml

    features:
      hindsight:
        wanted: false
        reason: "Not using personal memory system"
      telegram:
        wanted: false
      obsidian:
        wanted: true

Semantics:
    - ``wanted: true`` — User wants this. Probe it, show it, diagnose it.
    - ``wanted: false`` — User opted out. Don't probe, hide from dashboard,
      agents won't suggest it.  But keep awareness for "why isn't X working?"
    - ``wanted: null`` (or absent) — Undecided.  Probe normally, show normally.
      The setup wizard will ask about these.

``wanted: false`` implies ``tools.<id>.enabled: false`` (skip probe).
``wanted: true`` + ``tools.<id>.enabled: false`` means "I want it but it's
temporarily disabled for debugging".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from work_buddy.config import read_config_local, write_config_local
from work_buddy.consent import requires_consent


@dataclass
class FeaturePreference:
    """User preference for a single component.

    Attributes:
        component_id: Matches ComponentDef.id and ToolProbe.id.
        wanted: True (user wants it), False (opted out), None (undecided).
        reason: User-supplied reason for opting out (optional).
    """

    component_id: str
    wanted: bool | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"wanted": self.wanted}
        if self.reason:
            d["reason"] = self.reason
        return d

    @classmethod
    def from_dict(cls, component_id: str, data: dict[str, Any]) -> FeaturePreference:
        return cls(
            component_id=component_id,
            wanted=data.get("wanted"),
            reason=data.get("reason"),
        )


def load_preferences() -> dict[str, FeaturePreference]:
    """Load all feature preferences from config.local.yaml."""
    local = read_config_local()
    features = local.get("features", {})
    if not isinstance(features, dict):
        return {}
    return {
        comp_id: FeaturePreference.from_dict(comp_id, data)
        if isinstance(data, dict)
        else FeaturePreference(component_id=comp_id, wanted=data)
        for comp_id, data in features.items()
    }


def get_preference(component_id: str) -> FeaturePreference:
    """Get the preference for a single component.

    Returns a preference with ``wanted=None`` if not explicitly set.
    """
    prefs = load_preferences()
    return prefs.get(component_id, FeaturePreference(component_id=component_id))


def save_preferences(prefs: dict[str, FeaturePreference]) -> None:
    """Save feature preferences to config.local.yaml."""
    features: dict[str, Any] = {}
    for comp_id, pref in prefs.items():
        entry: dict[str, Any] = {"wanted": pref.wanted}
        if pref.reason:
            entry["reason"] = pref.reason
        features[comp_id] = entry
    write_config_local("features", features)


def set_preference(
    component_id: str,
    wanted: bool | None,
    reason: str | None = None,
) -> None:
    """Set the preference for a single component and persist."""
    prefs = load_preferences()
    prefs[component_id] = FeaturePreference(
        component_id=component_id,
        wanted=wanted,
        reason=reason,
    )
    save_preferences(prefs)


@requires_consent(
    operation="setup.write_preferences",
    reason="Modify feature preferences (features.* in config.local.yaml)",
    risk="moderate",
)
def apply_preference_updates(updates: dict[str, Any]) -> list[str]:
    """Apply a batch of feature-preference updates. Consent-gated.

    ``updates`` maps component_id -> {"wanted": bool|None, "reason": str} or bool/None.
    Returns the list of component_ids that were written.
    """
    from work_buddy.health.components import COMPONENT_CATALOG

    written: list[str] = []
    for comp_id, data in updates.items():
        if comp_id not in COMPONENT_CATALOG:
            continue
        if isinstance(data, dict):
            set_preference(
                comp_id,
                wanted=data.get("wanted"),
                reason=data.get("reason"),
            )
            written.append(comp_id)
        elif isinstance(data, bool) or data is None:
            set_preference(comp_id, wanted=data)
            written.append(comp_id)
    return written


def is_wanted(component_id: str) -> bool | None:
    """Quick check: is this component wanted?

    Returns True, False, or None (undecided).
    """
    return get_preference(component_id).wanted
