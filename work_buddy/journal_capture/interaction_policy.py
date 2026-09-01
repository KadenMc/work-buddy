"""Small, shared admission rules for data-defined Journal interactions."""

from __future__ import annotations

from typing import Any, Mapping


AI_CONTRIBUTION_MODES = frozenset({"allowed", "suggestion_only"})


def ai_contribution_allowed(definition: Mapping[str, Any]) -> bool:
    """Return whether a trusted behavior permits durable AI contribution."""

    return definition.get("aiContribution") in AI_CONTRIBUTION_MODES


def module_requires_ai_contribution(module_type_id: str) -> bool:
    """Module families whose product contract includes an AI write path."""

    return module_type_id == "prompt_result"


__all__ = [
    "AI_CONTRIBUTION_MODES",
    "ai_contribution_allowed",
    "module_requires_ai_contribution",
]
