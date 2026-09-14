"""Transport-neutral facts used to decide whether a Workflow may run."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class InvocationContext(str, Enum):
    """Semantic caller context used by Skill and Workflow policy."""

    AGENT_CONVERSATION = "agent_conversation"
    AGENT_AUTONOMOUS = "agent_autonomous"
    FSM_INTERNAL = "fsm_internal"
    ACTION_PROPOSAL = "action_proposal"
    USER_INVOCATION = "user_invocation"


class InvocationChannel(str, Enum):
    """Transport or host that requested a Workflow invocation."""

    MCP = "mcp"
    SIDECAR = "sidecar"
    DASHBOARD = "dashboard"
    INTERNAL = "internal"


class WorkflowEntrySurface(str, Enum):
    """User-meaningful surface through which a Workflow was addressed."""

    SKILL_GATEWAY = "skill_gateway"
    SCHEDULER = "scheduler"
    ACTION_CATALOG = "action_catalog"
    DASHBOARD = "dashboard"
    INTERNAL = "internal"


class InteractionAttendance(str, Enum):
    """Whether a user is present to participate in the invocation."""

    ATTENDED = "attended"
    UNATTENDED = "unattended"


class ExecutorFacility(str, Enum):
    """Executor facilities an invocation environment can actually provide."""

    PROGRAM = "program"
    CALLING_AGENT = "calling_agent"
    MODEL = "model"
    SUBAGENT = "subagent"
    USER = "user"
    WORKFLOW = "workflow"


@dataclass(frozen=True, slots=True)
class FeaturePreferenceContext:
    """Last-mile feature preference facts for one invocation."""

    available: bool = True
    opted_out: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    """Authorization result supplied by the driving adapter."""

    allowed: bool = True
    denied_by: str | None = None
    message: str | None = None
    allowed_sample: tuple[str, ...] = ()
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowInvocationContext:
    """Orthogonal policy facts for a Workflow invocation.

    Runtime-dependent facts may be omitted. ``WorkflowAdmission`` resolves
    feature preferences and component probes at evaluation time when adapters
    do not provide an explicit snapshot.
    """

    channel: InvocationChannel
    entry_surface: WorkflowEntrySurface
    invocation_context: InvocationContext
    principal: str | None = None
    session_id: str | None = None
    active_modes: frozenset[str] = field(default_factory=frozenset)
    attendance: InteractionAttendance = InteractionAttendance.UNATTENDED
    executor_facilities: frozenset[ExecutorFacility] = field(default_factory=frozenset)
    feature_preferences: FeaturePreferenceContext | None = None
    component_availability: Mapping[str, bool] | None = None
    authorization: AuthorizationContext = field(default_factory=AuthorizationContext)
    skill_surface_eligible: bool | None = None

    def __post_init__(self) -> None:
        if self.component_availability is not None and not isinstance(
            self.component_availability, MappingProxyType
        ):
            object.__setattr__(
                self,
                "component_availability",
                MappingProxyType(dict(self.component_availability)),
            )


__all__ = [
    "AuthorizationContext",
    "ExecutorFacility",
    "FeaturePreferenceContext",
    "InteractionAttendance",
    "InvocationChannel",
    "InvocationContext",
    "WorkflowEntrySurface",
    "WorkflowInvocationContext",
]
