"""Truthful execution and data-sharing disclosure for Co-work Verify.

The runtime still owns launch authorization and dispatch.  This module owns
the immutable, user-facing description of that runtime so projections do not
turn a requested launch budget into a provider guarantee or blur the local
checker together with account-backed coordination.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from work_buddy.agent_execution.models import AgentExecutionSelection
from work_buddy.cowork.verify_jobs import MAX_VERIFY_JOB_BUDGET_USD


VERIFY_EXECUTION_PLAN_SCHEMA = (
    "work-buddy.cowork-verify-execution-disclosure/v1"
)
# A single click may launch one account-backed specialist per selected personal
# check before the coordinator.  Keep that fan-out server-bounded even for
# providers whose per-worker spend ceiling cannot be enforced.
MAX_VERIFY_SPECIALIST_CHECKS_PER_RUN = 5

CostEnforcementClass = Literal["hard_ceiling", "estimate", "unavailable"]


@dataclass(frozen=True, slots=True)
class ProviderCostControl:
    """One provider's actual per-worker-session cost-control semantics."""

    provider_id: str
    enforcement_class: CostEnforcementClass
    ceiling_usd_per_worker_session: float | None
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "enforcement_class": self.enforcement_class,
            "ceiling_usd_per_worker_session": (
                self.ceiling_usd_per_worker_session
            ),
            "basis": self.basis,
        }


_PROVIDER_COST_CONTROLS = (
    ProviderCostControl(
        provider_id="claude-code",
        enforcement_class="hard_ceiling",
        ceiling_usd_per_worker_session=MAX_VERIFY_JOB_BUDGET_USD,
        basis="claude_code_max_budget_usd",
    ),
    ProviderCostControl(
        provider_id="codex",
        enforcement_class="unavailable",
        ceiling_usd_per_worker_session=None,
        basis="codex_worker_has_no_budget_enforcement",
    ),
)


def provider_cost_control(provider_id: str) -> ProviderCostControl:
    """Return conservative cost semantics for an exact provider."""

    for control in _PROVIDER_COST_CONTROLS:
        if control.provider_id == provider_id:
            return control
    return ProviderCostControl(
        provider_id=provider_id,
        enforcement_class="unavailable",
        ceiling_usd_per_worker_session=None,
        basis="provider_cost_enforcement_not_attested",
    )


@dataclass(frozen=True, slots=True)
class VerifyExecutionDisclosurePlan:
    """Immutable disclosure for the currently implemented Verify route."""

    selection: AgentExecutionSelection | None = None
    specialist_worker_sessions: int = 0

    def to_dict(self) -> dict[str, Any]:
        if (
            isinstance(self.specialist_worker_sessions, bool)
            or not isinstance(self.specialist_worker_sessions, int)
            or self.specialist_worker_sessions < 0
            or self.specialist_worker_sessions
            > MAX_VERIFY_SPECIALIST_CHECKS_PER_RUN
        ):
            raise ValueError(
                "specialist_worker_sessions must be an integer between 0 and "
                f"{MAX_VERIFY_SPECIALIST_CHECKS_PER_RUN}"
            )
        selected_cost = (
            None
            if self.selection is None
            else provider_cost_control(self.selection.provider_id).to_dict()
        )
        worker_sessions: dict[str, Any] = {
            "initial": 1 + self.specialist_worker_sessions,
            "maximum": 3 + self.specialist_worker_sessions,
            "conditional_roles": [
                "reviser",
                "post_revision_coordinator",
            ],
        }
        if self.specialist_worker_sessions:
            worker_sessions["specialist_checks"] = self.specialist_worker_sessions
        plan = {
            "schema": VERIFY_EXECUTION_PLAN_SCHEMA,
            "authoritative": True,
            "checker": {
                "execution_class": "in_process",
                "mechanism": "deterministic_exact_match",
                "model_call": False,
                "external_egress": False,
                "content_boundary": "captured_target",
            },
            "coordination": {
                "execution_class": "account_backed_agent",
                "selection": {
                    "mode": "explicit_at_run_start",
                    "provider_id": (
                        None
                        if self.selection is None
                        else self.selection.provider_id
                    ),
                    "model_id": (
                        None
                        if self.selection is None
                        else self.selection.model_id
                    ),
                    "provider_label": (
                        None
                        if self.selection is None
                        else self.selection.provider_label
                    ),
                    "model_label": (
                        None
                        if self.selection is None
                        else self.selection.model_label
                    ),
                },
                "content_boundary": "entire_frozen_document",
                "external_egress": True,
                "fallback": {
                    "provider_model_fallback": False,
                    "failure_mode": "fail_closed",
                },
                "worker_sessions": worker_sessions,
                "cost_control": selected_cost,
                "provider_cost_controls": [
                    control.to_dict() for control in _PROVIDER_COST_CONTROLS
                ],
            },
        }
        if self.specialist_worker_sessions:
            plan["specialist_checks"] = {
                "execution_class": "account_backed_agent",
                "mechanism": "instruction_based_model_evaluation",
                "worker_sessions": self.specialist_worker_sessions,
                "content_boundary": "captured_target",
                "external_egress": True,
                "selection": "same_explicit_provider_and_model_as_verify_run",
                "fallback": {
                    "provider_model_fallback": False,
                    "failure_mode": "fail_closed",
                },
                "cost_control": selected_cost,
            }
        return plan


def verify_execution_disclosure_plan(
    selection: AgentExecutionSelection | None = None,
    *,
    specialist_worker_sessions: int = 0,
) -> dict[str, Any]:
    """Project the immutable plan as wire-ready JSON data."""

    return VerifyExecutionDisclosurePlan(
        selection=selection,
        specialist_worker_sessions=specialist_worker_sessions,
    ).to_dict()


__all__ = [
    "CostEnforcementClass",
    "MAX_VERIFY_SPECIALIST_CHECKS_PER_RUN",
    "ProviderCostControl",
    "VERIFY_EXECUTION_PLAN_SCHEMA",
    "VerifyExecutionDisclosurePlan",
    "provider_cost_control",
    "verify_execution_disclosure_plan",
]
