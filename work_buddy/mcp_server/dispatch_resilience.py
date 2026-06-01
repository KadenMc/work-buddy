"""Resilience wiring for the MCP gateway's capability dispatch.

Every ``wb_run`` capability dispatch runs through the resilience framework's
``guarded_call`` so the gateway emits dispatch-timing telemetry under the
``wb_run:<capability>`` operation key. Listeners registered here record those
events (an in-process metrics ring) and log one grep-able line per dispatch.

Kept lightweight: only the resilience framework (stdlib-only) is imported at
module load, so this never pulls a heavy dependency onto the gateway boot path.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping

from work_buddy.consent import ConsentRequired
from work_buddy.resilience import (
    CircuitBreakerStrategy,
    Deadline,
    InMemoryMetrics,
    TimeoutStrategy,
    default_classify,
    register_listener,
)
from work_buddy.tools import ToolUnavailable
from work_buddy.resilience.telemetry import (
    CallCompleted,
    CircuitStateChanged,
    GuardEvent,
    LoadShed,
)

logger = logging.getLogger("work_buddy.mcp_server.dispatch")

# Default wall-time budget for a capability dispatch that declares none and is
# not self-managing. Local / leaf operations complete well within this.
DEFAULT_DISPATCH_TIMEOUT_S: float = 30.0

# Tool id whose dependent capabilities self-retry (via @bridge_retry) and own
# their own time budget — the gateway does not impose a timeout on them.
_OBSIDIAN_TOOL_ID = "obsidian"

# Cache of the bridge-backed tool family (obsidian + its in-Obsidian plugin
# tools: datacore, smart_connections, ...). Static from the probe graph for the
# process lifetime; recomputed on the first call after a module reload.
_BRIDGE_TOOLS: set[str] | None = None


def _bridge_tools() -> set[str]:
    global _BRIDGE_TOOLS
    if _BRIDGE_TOOLS is None:
        from work_buddy.tools import obsidian_backed_tools
        _BRIDGE_TOOLS = obsidian_backed_tools()
    return _BRIDGE_TOOLS

# One process-global circuit breaker shared by every Obsidian-bridge-dependent
# dispatch. The bridge is a single shared dependency, so one breaker models its
# health: after enough consecutive transient/timeout failures it opens and sheds
# bridge work (REJECTED) instead of hammering a struggling bridge, then admits a
# single probe after the cooldown and closes on success. Terminal failures
# (Obsidian not running, plugin missing) do not count toward the trip — those
# fail fast per call with an actionable error and recover the moment the bridge
# returns. Stateful and reused across calls; never rebuilt per dispatch.
_OBSIDIAN_BREAKER = CircuitBreakerStrategy(
    name="obsidian_bridge", failure_threshold=5, reset_timeout_s=30.0,
)

# Control-flow exceptions the gateway's dispatch loop handles itself. They must
# reach those handlers untouched (re-raised by the seam, not classified), and
# must never count as a bridge failure toward the circuit breaker.
_CONTROL_FLOW_PASSTHROUGH: tuple[type[BaseException], ...] = (
    ConsentRequired,
    ToolUnavailable,
    TypeError,
)


def _requires_bridge(entry: Any) -> bool:
    """True if the capability requires any bridge-backed tool (the Obsidian
    bridge itself or an in-Obsidian plugin that depends on it)."""
    requires = getattr(entry, "requires", None) or []
    return bool(set(requires) & _bridge_tools())

# In-process recorder of guarded-call telemetry. A status surface or a live
# verification script can snapshot it via :func:`get_dispatch_metrics`.
_METRICS = InMemoryMetrics()

_LISTENERS_READY = False


class _DispatchLogListener:
    """Logs guarded-call telemetry so every dispatch leaves a grep-able line."""

    def on_event(self, event: GuardEvent) -> None:
        if isinstance(event, CallCompleted):
            logger.info(
                "guard.call op=%s outcome=%s dur=%.3fs%s",
                event.operation_key,
                event.outcome.value,
                event.duration_s,
                f" error={event.error_type}" if event.error_type else "",
            )
        elif isinstance(event, CircuitStateChanged):
            logger.warning(
                "guard.circuit name=%s %s->%s op=%s",
                event.name, event.from_state, event.to_state,
                event.operation_key,
            )
        elif isinstance(event, LoadShed):
            logger.warning(
                "guard.shed name=%s reason=%s op=%s",
                event.name, event.reason, event.operation_key,
            )


def ensure_listeners_registered() -> None:
    """Register the gateway's telemetry listeners once (idempotent)."""
    global _LISTENERS_READY
    if _LISTENERS_READY:
        return
    register_listener(_METRICS)
    register_listener(_DispatchLogListener())
    _LISTENERS_READY = True


def get_dispatch_metrics() -> InMemoryMetrics:
    """The in-process recorder of guarded-call telemetry (status / tests)."""
    return _METRICS


# ---------------------------------------------------------------------------
# Timeout budget — operation-derived, param-aware
# ---------------------------------------------------------------------------


def _domain_default(entry: Any) -> float:
    """The budget for a capability that declares no ``timeout_seconds``.

    The gateway timeout is **opt-in**: an undeclared budget is unbounded
    (``math.inf``), so the gateway imposes no wall-time cap unless a capability
    explicitly asks for one. A flat default is deliberately NOT auto-applied —
    a too-low default silently breaks the whole class of capabilities that
    legitimately run long: human-in-the-loop prompts that poll for a response
    (``request_send`` / ``request_poll``), synchronous retry wrappers
    (``obsidian_retry`` / ``retry``), and LLM submission (``llm_submit``). A
    real default must be calibrated from observed dispatch p99 (the telemetry
    this module records) AND paired with explicit exemptions for those
    long-runners. ``DEFAULT_DISPATCH_TIMEOUT_S`` is the candidate value for that
    future calibration; it is intentionally not applied yet. An un-capped
    capability that hangs leaks only a worker thread — it does not stall the
    gateway event loop — so opt-in is safe.
    """
    return math.inf


def _coerce_budget(value: Any, *, fallback: float) -> float:
    """Coerce a declared/derived budget to seconds (``inf`` = unbounded).

    ``None`` → unbounded; a non-positive or non-numeric value → ``fallback``
    (a non-positive timeout is meaningless and ``TimeoutStrategy`` rejects it).
    """
    if value is None:
        return math.inf
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return fallback
    if seconds <= 0 or math.isnan(seconds):
        return math.inf
    return seconds


def resolve_timeout_budget(entry: Any, params: Mapping[str, Any]) -> float:
    """Resolve the wall-time budget (seconds; ``inf`` = no gateway timeout).

    Operation-derived and opt-in: a ``timeout_seconds`` policy callable derives
    the budget from the actual params; a scalar is a fixed ceiling; ``None``
    (unset) is unbounded (the gateway imposes no timeout — see
    ``_domain_default``). Never raises — a policy that raises falls back to
    unbounded.
    """
    declared = getattr(entry, "timeout_seconds", None)
    domain_default = _domain_default(entry)
    if callable(declared):
        try:
            derived = declared(dict(params))
        except Exception:  # noqa: BLE001 - a bad policy must never break dispatch
            logger.warning(
                "timeout policy for %r raised; using domain default",
                getattr(entry, "name", "?"), exc_info=True,
            )
            return domain_default
        return _coerce_budget(derived, fallback=domain_default)
    if declared is not None:
        return _coerce_budget(declared, fallback=domain_default)
    return domain_default


def build_dispatch_deadline(budget: float) -> Deadline:
    """The propagating deadline for a dispatch (``inf`` → never)."""
    return Deadline.never() if budget == math.inf else Deadline.after(budget)


def build_dispatch_strategies(entry: Any, budget: float) -> list:
    """The resilience strategy chain for one capability dispatch.

    Canonical order (outermost first): ``TimeoutStrategy`` (only when the
    budget is bounded) then the shared Obsidian ``CircuitBreakerStrategy``
    (only for bridge-dependent capabilities). No retry strategy is added here
    — the one-retry-layer rule reserves retry for the inner chain
    (``@bridge_retry``-decorated capabilities).
    """
    strategies: list = []
    if budget != math.inf:
        strategies.append(TimeoutStrategy(budget))
    if _requires_bridge(entry):
        strategies.append(_OBSIDIAN_BREAKER)
    return strategies


def dispatch_classifiers(entry: Any):
    """The (exception classifier, result classifier) for a dispatch.

    Obsidian-bridge capabilities use the bridge classifiers so a raised
    ``ObsidianError`` and a legacy ``bridge_failure`` return-dict both map onto
    the outcome taxonomy that the circuit breaker counts. Everything else uses
    the framework default (and has no result classifier).
    """
    if _requires_bridge(entry):
        from work_buddy.obsidian.resilient_bridge import (
            classify_bridge_result,
            classify_obsidian_error,
        )
        return classify_obsidian_error, classify_bridge_result
    return default_classify, None


def dispatch_passthrough(entry: Any) -> tuple[type[BaseException], ...]:
    """Exceptions the seam must re-raise untouched (not classify).

    The gateway's dispatch loop owns the consent-retry / param-error / tool-
    unavailable / post-write-verify control flow, so those exceptions pass
    through the seam to its handlers and never count toward the breaker.
    """
    passthrough = _CONTROL_FLOW_PASSTHROUGH
    if _requires_bridge(entry):
        from work_buddy.obsidian.resilient_bridge import OBSIDIAN_PASSTHROUGH
        passthrough = passthrough + OBSIDIAN_PASSTHROUGH
    return passthrough


def obsidian_breaker_state() -> str:
    """Current state of the shared Obsidian bridge breaker (status / tests)."""
    return _OBSIDIAN_BREAKER.state.value
