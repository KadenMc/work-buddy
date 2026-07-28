"""Provider protocol shared by the execution registry."""

from __future__ import annotations

from typing import Protocol

from .models import (
    AgentExecutionSelection,
    AgentSpawnOutcome,
    AgentSpawnRequest,
    ProviderDescriptor,
)


class AgentExecutionProvider(Protocol):
    """Authentication/runtime route for one family of agent models."""

    provider_id: str
    label: str
    auth_mode: str
    default_model_id: str

    def probe(self, *, refresh: bool = False) -> ProviderDescriptor:
        """Return a redacted installation, auth, and model projection."""

    def validate_selection(
        self,
        selection: AgentExecutionSelection,
        *,
        refresh: bool = False,
    ) -> AgentExecutionSelection:
        """Validate IDs and return trusted labels."""

    def start_detached(self, request: AgentSpawnRequest) -> AgentSpawnOutcome:
        """Start one already-validated detached driver."""
