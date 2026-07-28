"""Typed contracts for long-running agent execution providers.

The execution profile is intentionally distinct from ``LLMRunner``.  It
selects a local authenticated agent host (Claude Code or Codex) and one model
that host advertised, rather than a one-shot completion backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def is_safe_session_id(value: str) -> bool:
    """Return whether ``value`` is safe for env, CLI, and HTTP-header use."""

    return bool(_SESSION_ID_RE.fullmatch(value))


class ProviderAvailability(str, Enum):
    """User-safe provider readiness states."""

    READY = "ready"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AgentExecutionSelection:
    """One server-validated provider/model pair.

    Labels are projections from the trusted provider catalog.  Callers may
    construct an ID-only value as input, but must use
    :func:`ProviderRegistry.validate_selection` before persisting or spawning
    it.
    """

    provider_id: str
    model_id: str
    provider_label: str = ""
    model_label: str = ""

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if not self.model_id.strip():
            raise ValueError("model_id must not be empty")

    def to_dict(self) -> dict[str, str]:
        """Return the stable public JSON projection."""

        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "provider_label": self.provider_label,
            "model_label": self.model_label,
        }


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """One model advertised by an execution provider."""

    id: str
    label: str
    available: bool = True
    description: str = ""
    unavailable_reason: str = ""
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "available": self.available,
            "description": self.description,
            "unavailable_reason": self.unavailable_reason,
            "is_default": self.is_default,
        }


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Redacted provider probe and model catalog."""

    id: str
    label: str
    availability: ProviderAvailability
    auth_mode: str
    models: tuple[ModelDescriptor, ...] = ()
    description: str = ""
    unavailable_reason: str = ""
    state_key: str = ""

    @property
    def available(self) -> bool:
        return self.availability is ProviderAvailability.READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "available": self.available,
            "availability": self.availability.value,
            "auth_mode": self.auth_mode,
            "description": self.description,
            "models": [model.to_dict() for model in self.models],
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class AgentExecutionCatalog:
    """All known providers plus a deterministic default selection."""

    providers: tuple[ProviderDescriptor, ...]
    default_selection: AgentExecutionSelection

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": [provider.to_dict() for provider in self.providers],
            "default_selection": self.default_selection.to_dict(),
        }


def default_working_directory() -> Path:
    """Return a neutral existing directory for provider host validation."""

    from tempfile import gettempdir

    return Path(gettempdir()).resolve()


@dataclass(frozen=True, slots=True)
class AgentSpawnRequest:
    """Validated input for starting one detached agent driver."""

    name: str
    prompt: str
    selection: AgentExecutionSelection
    session_id: str
    working_directory: Path = field(default_factory=default_working_directory)
    max_budget_usd: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("name must not be empty")
        if not self.prompt:
            raise ValueError("prompt must not be empty")
        if not is_safe_session_id(self.session_id):
            raise ValueError("session_id must be a safe nonempty backend identity")

    def with_selection(
        self, selection: AgentExecutionSelection
    ) -> "AgentSpawnRequest":
        return replace(self, selection=selection)


@dataclass(frozen=True, slots=True)
class AgentSpawnOutcome:
    """Provider-neutral detached-process launch result."""

    status: str
    selection: AgentExecutionSelection
    pid: int | None = None
    session_id: str = ""
    error_code: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.pid is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selection": self.selection.to_dict(),
            "pid": self.pid,
            "session_id": self.session_id,
            "error_code": self.error_code,
            "error": self.error,
        }


class AgentExecutionError(ValueError):
    """Base class for safe execution-profile validation errors."""

    error_code = "agent_execution_error"


class UnknownProviderError(AgentExecutionError):
    error_code = "unknown_provider"


class UnknownModelError(AgentExecutionError):
    error_code = "unknown_model"


class ProviderUnavailableError(AgentExecutionError):
    error_code = "provider_unavailable"
