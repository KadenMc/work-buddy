"""Server-authoritative registry for agent execution providers."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from work_buddy.config import load_config

from .base import AgentExecutionProvider
from .claude_code import ClaudeCodeProvider
from .codex import CodexProvider
from .models import (
    AgentExecutionCatalog,
    AgentExecutionSelection,
    AgentSpawnOutcome,
    AgentSpawnRequest,
    ProviderAvailability,
    ProviderDescriptor,
    UnknownProviderError,
)

logger = logging.getLogger(__name__)

_DEFAULT_SELECTION = AgentExecutionSelection(
    provider_id="claude-code",
    model_id="sonnet",
    provider_label="Claude Code",
    model_label="Sonnet",
)
_CLAUDE_DEFAULT_SELECTIONS = {
    "sonnet": _DEFAULT_SELECTION,
    "opus": AgentExecutionSelection(
        provider_id="claude-code",
        model_id="opus",
        provider_label="Claude Code",
        model_label="Opus",
    ),
}


def _configured_default_selection() -> AgentExecutionSelection:
    """Preserve the supported Claude alias configured for legacy agent spawns."""

    try:
        configured = (
            load_config()
            .get("sidecar", {})
            .get("agent_spawn", {})
            .get("model", _DEFAULT_SELECTION.model_id)
        )
    except Exception:
        logger.warning(
            "Could not read the configured Co-work execution default; "
            "using Claude Sonnet.",
            exc_info=True,
        )
        return _DEFAULT_SELECTION
    model_id = (
        configured.strip().casefold()
        if isinstance(configured, str)
        else _DEFAULT_SELECTION.model_id
    )
    selected = _CLAUDE_DEFAULT_SELECTIONS.get(model_id)
    if selected is not None:
        return selected
    logger.warning(
        "Configured agent-spawn model is not a supported Co-work default; "
        "using Claude Sonnet."
    )
    return _DEFAULT_SELECTION


def configured_default_selection() -> AgentExecutionSelection:
    """Probe-free legacy config seed; not the live Dashboard AI default."""
    return _configured_default_selection()


def _settings_default_selection() -> AgentExecutionSelection:
    from work_buddy.settings.broker import get_dashboard_chat_execution_default

    value = get_dashboard_chat_execution_default()
    provider_id, model_id = value["provider_id"], value["model_id"]
    if provider_id == "claude-code" and model_id in _CLAUDE_DEFAULT_SELECTIONS:
        return _CLAUDE_DEFAULT_SELECTIONS[model_id]
    try:
        provider_label = get_registry().get_provider(provider_id).label
    except UnknownProviderError:
        # Keep an unavailable saved identity visible; never substitute a model.
        provider_label = provider_id
    return AgentExecutionSelection(provider_id, model_id, provider_label, model_id)


class ProviderRegistry:
    """Validated provider lookup, catalog projection, and dispatch."""

    def __init__(
        self,
        providers: Iterable[AgentExecutionProvider],
        *,
        default_selection: AgentExecutionSelection = _DEFAULT_SELECTION,
        default_resolver: Callable[[], AgentExecutionSelection] | None = None,
    ) -> None:
        ordered = tuple(providers)
        by_id: dict[str, AgentExecutionProvider] = {}
        for provider in ordered:
            if provider.provider_id in by_id:
                raise ValueError(
                    f"Duplicate agent execution provider: {provider.provider_id}"
                )
            by_id[provider.provider_id] = provider
        if default_selection.provider_id not in by_id:
            raise ValueError("Default provider is not registered")
        self._providers = ordered
        self._by_id = by_id
        self._default_selection = default_selection
        self._default_resolver = default_resolver

    @property
    def default_selection(self) -> AgentExecutionSelection:
        """Return the deterministic default without performing a probe."""

        return self._default_resolver() if self._default_resolver is not None else self._default_selection

    def get_provider(self, provider_id: str) -> AgentExecutionProvider:
        try:
            return self._by_id[provider_id]
        except KeyError as exc:
            raise UnknownProviderError(
                f"Unknown agent execution provider: {provider_id}"
            ) from exc

    def get_providers(self, *, refresh: bool = False) -> tuple[ProviderDescriptor, ...]:
        """Discover providers independently of a possibly invalid global default."""
        def probe_safely(
            provider: AgentExecutionProvider,
        ) -> ProviderDescriptor:
            try:
                return provider.probe(refresh=refresh)
            except Exception:
                # Catalog reads must remain useful when one local runtime is
                # broken. Never project exception text: SDK/CLI failures may
                # carry paths, account data, or subprocess diagnostics.
                return ProviderDescriptor(
                    id=provider.provider_id,
                    label=provider.label,
                    availability=ProviderAvailability.UNKNOWN,
                    auth_mode=provider.auth_mode,
                    unavailable_reason=(
                        f"{provider.label} couldn't be checked."
                    ),
                )

        with ThreadPoolExecutor(
            max_workers=len(self._providers),
            thread_name_prefix="agent-provider-probe",
        ) as pool:
            # executor.map preserves the deterministic registry order while
            # bounding cold-catalog latency to the slowest individual probe.
            descriptors = tuple(pool.map(probe_safely, self._providers))
        return descriptors

    def get_catalog(self, *, refresh: bool = False) -> AgentExecutionCatalog:
        return AgentExecutionCatalog(
            providers=self.get_providers(refresh=refresh),
            default_selection=self.default_selection,
        )

    def validate_selection(
        self,
        selection: AgentExecutionSelection,
        *,
        refresh: bool = False,
    ) -> AgentExecutionSelection:
        provider = self.get_provider(selection.provider_id)
        return provider.validate_selection(selection, refresh=refresh)

    def start_detached(self, request: AgentSpawnRequest) -> AgentSpawnOutcome:
        """Re-probe, validate, then launch the exact confirmed pair."""

        provider = self.get_provider(request.selection.provider_id)
        selection = provider.validate_selection(
            request.selection,
            refresh=True,
        )
        return provider.start_detached(request.with_selection(selection))

    def clear_probe_cache(self) -> None:
        for provider in self._providers:
            clear = getattr(provider, "clear_probe_cache", None)
            if callable(clear):
                clear()


_registry: ProviderRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ProviderRegistry(
                    (ClaudeCodeProvider(), CodexProvider()),
                    default_resolver=_settings_default_selection,
                )
    return _registry


def get_catalog(*, refresh: bool = False) -> AgentExecutionCatalog:
    return get_registry().get_catalog(refresh=refresh)


def get_providers(*, refresh: bool = False) -> tuple[ProviderDescriptor, ...]:
    return get_registry().get_providers(refresh=refresh)


def default_selection() -> AgentExecutionSelection:
    return get_registry().default_selection


def validate_selection(
    selection: AgentExecutionSelection,
    *,
    refresh: bool = False,
) -> AgentExecutionSelection:
    return get_registry().validate_selection(selection, refresh=refresh)


def start_detached(request: AgentSpawnRequest) -> AgentSpawnOutcome:
    return get_registry().start_detached(request)


def clear_probe_cache() -> None:
    get_registry().clear_probe_cache()
