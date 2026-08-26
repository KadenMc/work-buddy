"""Deterministic interactive driver for the disposable dashboard browser host.

Only the provider process is replaced. The real registry, session broker,
conversation/form capabilities, Sources accounting, leases and HTTP routes run.
This module is never imported by a production service.
"""

from __future__ import annotations

import re
import threading

from work_buddy.agent_execution import registry
from work_buddy.agent_execution.models import (
    AgentExecutionSelection,
    AgentSpawnOutcome,
    ModelDescriptor,
    ProviderAvailability,
    ProviderDescriptor,
    ProviderUnavailableError,
    UnknownModelError,
)
from work_buddy.dashboard.assistance import service
from work_buddy.dashboard.assistance.runner import HostedAssistanceRunner
from work_buddy.mcp_server import op_registry


def install_assistance_fixture(state: dict, state_lock: threading.RLock) -> None:
    workers: dict[int, tuple[threading.Event, str]] = {}
    state.update(assistance_starts=0, assistance_errors=[], assistance_executions=[])

    def run_driver(request, stopped):
        bindings = dict(re.findall(r"^- ([a-z_]+): ([^\n]+)$", request.prompt, re.MULTILINE))
        scope = {
            "conversation_id": bindings["conversation_id"],
            "consumer": bindings["consumer"],
            "generation": bindings["generation"],
            "agent_session_id": request.session_id,
        }
        form_scope = {**scope, "assistant_session_id": bindings["assistant_session_id"]}

        def call(name, **kwargs):
            operation = op_registry.get_op(f"op.wb.{name}")
            result = operation(**kwargs)
            if result.get("status") in {
                "lease_lost", "invalid_request", "assistance_start_required",
                "assistance_disabled", "assistance_session_ended",
            } or "error" in result:
                raise RuntimeError(result.get("status") or "fixture_tool_error")
            return result

        try:
            context = call(
                "assisted_draft_context_get", **form_scope,
                message_id=bindings["initial_snapshot_message_id"],
            )
            values = context["snapshot"]["snapshot"]
            job = any(field["path"] == ["schedule"] for field in context["form"]["fields"])
            title = values.get("name" if job else "title") or ("this job" if job else "this task")
            if not context["greeting_sent"]:
                call(
                    "conversation_send", **scope,
                    message_id=context["greeting_message_id"],
                    message=(
                        f"I can help with {title}. I have the current fields and can "
                        "suggest a clearer next step or fill in missing details. "
                        "What would you like to refine? You will review and submit the form."
                    ),
                )
            while not stopped.wait(0.1):
                turn = call("conversation_receive", **scope, timeout_seconds=0)
                if turn.get("status") != "message":
                    continue
                message = turn["message"]
                context = call(
                    "assisted_draft_context_get", **form_scope,
                    message_id=message["message_id"],
                )
                if context.get("reply_message_id"):
                    call("conversation_ack", **scope, message_id=message["message_id"])
                    continue
                with state_lock:
                    state["job_assistance_calls" if job else "task_assistance_calls"] += 1
                while not stopped.is_set():
                    with state_lock:
                        paused = state.get("pause_assistance", False)
                    if not paused:
                        break
                    stopped.wait(0.1)
                if stopped.is_set():
                    break
                text = message["content"]
                reply_id = f"assist-reply-{message['message_id']}"
                if "choices" in text.casefold():
                    choices = (
                        [{"key": "weekdays", "label": "Weekdays"}, {"key": "daily", "label": "Every day"}]
                        if job else
                        [{"key": "low", "label": "Low urgency"}, {"key": "high", "label": "High urgency"}]
                    )
                    call(
                        "conversation_ask", **scope, message_id=reply_id,
                        question="Which timing works?" if job else "How urgent is this task?",
                        response_type="choice", choices=choices,
                    )
                else:
                    values = context["snapshot"]["snapshot"]
                    if job:
                        operations = [
                            {"op": "set", "path": ["name"], "value": values.get("name") or "assisted-draft-live-job"},
                            {"op": "set", "path": ["schedule"], "value": "0 9 * * *" if text == "daily" else "0 9 * * 1-5"},
                            {"op": "set", "path": ["job_type"], "value": "prompt"},
                            {"op": "set", "path": ["prompt"], "value": values.get("prompt") or text[:12000]},
                        ]
                    elif text in {"low", "high"}:
                        operations = [{"op": "set", "path": ["urgency"], "value": text}]
                    else:
                        operations = [
                            {"op": "set", "path": ["title"], "value": values.get("title") or "Review the captured task"},
                            {"op": "set", "path": ["summary"], "value": text[:1000]},
                            {"op": "set", "path": ["next_action"], "value": "List the concrete steps and identify the first one."},
                        ]
                    call(
                        "assisted_draft_propose_patch", **form_scope,
                        message_id=message["message_id"],
                        consumption_receipt_id=context["consumption_receipt_id"],
                        proposal_id=f"fixture-{message['message_id']}", operations=operations,
                    )
                    call(
                        "conversation_send", **scope, message_id=reply_id,
                        message="I suggested those fields for review. Nothing has been submitted; you can edit or undo them.",
                    )
                call("conversation_ack", **scope, message_id=message["message_id"])
        except Exception as exc:  # noqa: BLE001 - record every isolated driver failure for the browser assertion surface
            with state_lock:
                state["assistance_errors"].append({"type": type(exc).__name__, "code": str(exc)[:160]})
            stopped.set()

    class FixtureProvider:
        auth_mode = "fixture_only"

        def __init__(self, provider_id, label, models):
            self.provider_id = provider_id
            self.label = label
            self.models = models
            self.default_model_id = models[0].id

        def probe(self, *, refresh=False):
            with state_lock:
                failed = state["assistance_provider_failed"]
            return ProviderDescriptor(
                id=self.provider_id, label=self.label, auth_mode=self.auth_mode,
                availability=ProviderAvailability.UNAVAILABLE if failed else ProviderAvailability.READY,
                models=self.models, unavailable_reason="Fixture provider unavailable." if failed else "",
            )

        def validate_selection(self, selection, *, refresh=False):
            descriptor = self.probe(refresh=refresh)
            if not descriptor.available:
                raise ProviderUnavailableError("Fixture provider unavailable")
            model = next((item for item in self.models if item.id == selection.model_id), None)
            if model is None:
                raise UnknownModelError("Unknown fixture model")
            return AgentExecutionSelection(self.provider_id, model.id, self.label, model.label)

        def start_detached(self, request):
            with state_lock:
                state["assistance_starts"] += 1
                state["assistance_executions"].append(request.selection.to_dict())
                pid = 700000 + state["assistance_starts"]
                stopped = threading.Event()
                workers[pid] = (stopped, request.session_id)
            threading.Thread(
                target=run_driver, args=(request, stopped), daemon=True,
                name=f"fixture-assisted-driver-{pid}",
            ).start()
            return AgentSpawnOutcome(
                status="ok", selection=request.selection, pid=pid,
                session_id=request.session_id,
            )

    class FixtureRunner(HostedAssistanceRunner):
        def is_alive(self, pid):
            with state_lock:
                worker = workers.get(pid)
                return worker is not None and not worker[0].is_set()

        def terminate(self, pid, generation):
            with state_lock:
                worker = workers.get(pid)
                if worker is not None and worker[1] == f"{generation}-assisted-draft":
                    worker[0].set()

    registry._registry = registry.ProviderRegistry(
        (
            FixtureProvider("claude-code", "Claude Code", (
                ModelDescriptor("sonnet", "Sonnet", is_default=True),
                ModelDescriptor("opus", "Opus"),
            )),
            FixtureProvider("codex", "Codex", (
                ModelDescriptor("fixture-codex", "Fixture Codex", is_default=True),
            )),
        ),
        default_resolver=registry._settings_default_selection,
    )
    service._default_broker = service.AssistanceBroker(runner=FixtureRunner())
    op_registry.load_builtin_ops()
