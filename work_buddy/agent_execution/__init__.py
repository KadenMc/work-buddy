"""Long-running local agent execution providers."""

from .models import (
    AgentExecutionCatalog,
    AgentExecutionError,
    AgentExecutionSelection,
    AgentSpawnOutcome,
    AgentSpawnRequest,
    ModelDescriptor,
    ProviderAvailability,
    ProviderDescriptor,
    ProviderUnavailableError,
    UnknownModelError,
    UnknownProviderError,
    default_working_directory,
)
from .identity import prompt_with_execution_identity
from .registry import (
    ProviderRegistry,
    clear_probe_cache,
    default_selection,
    get_catalog,
    get_registry,
    start_detached,
    validate_selection,
)

__all__ = [
    "AgentExecutionCatalog",
    "AgentExecutionError",
    "AgentExecutionSelection",
    "AgentSpawnOutcome",
    "AgentSpawnRequest",
    "ModelDescriptor",
    "ProviderAvailability",
    "ProviderDescriptor",
    "ProviderRegistry",
    "ProviderUnavailableError",
    "UnknownModelError",
    "UnknownProviderError",
    "clear_probe_cache",
    "default_selection",
    "default_working_directory",
    "get_catalog",
    "get_registry",
    "prompt_with_execution_identity",
    "start_detached",
    "validate_selection",
]
