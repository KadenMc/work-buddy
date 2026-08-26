"""Journal capture orchestration over Sources and the compatibility adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol

from work_buddy.journal_capture.content_adapter import JournalContentAdapter, marker_for
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCapture,
    JournalCaptureError,
    JournalCaptureValidationError,
    JournalProjectionError,
    JournalSmartAvailability,
    ProcessingState,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.settings import get_journal_day_window


logger = logging.getLogger(__name__)
_DAY_ID_PREFIX = "journal-day:"
_MAX_CAPTURE_BYTES = 1_048_576
_VALID_INPUT_MODES = {
    "direct_entry",
    "paste",
    "import",
    "dictation",
    "automation",
    "unknown",
}


@dataclass(frozen=True)
class CommittedIngress:
    source_ref: str
    representation_id: str
    submission_id: str
    command_id: str
    effect_id: str
    authorization_fingerprint: str
    authorization_expires_at: str | None = None
    usage_id: str | None = None


@dataclass(frozen=True)
class TaskProposalFollowUp:
    task_text: str
    rationale: str


@dataclass(frozen=True)
class SmartCaptureResult:
    target: CaptureTarget
    summary: str
    effects: tuple[str, ...]
    producer_ref: str | None = None
    model_id: str | None = None
    disclosure_manifest_sha256: str | None = None
    follow_up: TaskProposalFollowUp | None = None


class SmartCaptureProcessor(Protocol):
    def process(self, *, capture: JournalCapture, exact_text: str) -> SmartCaptureResult: ...


class TaskProposalIngress(Protocol):
    def create_task_proposal(self, *, client_mutation_id: str, parameters: dict,
                             origin: dict, actor: str) -> Mapping[str, Any]: ...

    def get(self, thread_id: str) -> Mapping[str, Any]: ...


class AuthoritativeLogWriter(Protocol):
    def __call__(self, entry, *, stated_at: str | None): ...


class SmartProcessingUnavailable:
    def process(self, *, capture: JournalCapture, exact_text: str) -> SmartCaptureResult:
        del capture, exact_text
        raise RuntimeError("smart_processing_unavailable")


class SmartDisclosureChanged(JournalCaptureError):
    code = "smart_disclosure_changed"


class JournalCaptureService:
    """Accept reference-bound source commands and materialize Journal state.

    The call that commits Sources happens outside this service.  ``accept`` is
    idempotent, so the source-owned outbox can replay it after a crash without
    duplicating the Journal entry or its Markdown projection.
    """

    def __init__(
        self,
        store: JournalCaptureStore,
        adapter: JournalContentAdapter,
        *,
        smart_processor: SmartCaptureProcessor | None = None,
        authoritative_log_writer: AuthoritativeLogWriter | None = None,
        proposal_service: TaskProposalIngress | None = None,
        smart_availability: JournalSmartAvailability | None = None,
        smart_configuration: Callable[[], tuple[SmartCaptureProcessor | None, JournalSmartAvailability]] | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.adapter.journal_store_path = store.path
        self.smart_processor = smart_processor or SmartProcessingUnavailable()
        self.authoritative_log_writer = authoritative_log_writer
        self.proposal_service = proposal_service
        self.smart_availability = smart_availability or (
            JournalSmartAvailability(state="ready", code="ready", reason="Smart is ready.")
            if smart_processor is not None else JournalSmartAvailability()
        )
        self.smart_configuration = smart_configuration
        self._smart_lock = threading.RLock()

    def refresh_smart_availability(self) -> None:
        if self.smart_configuration is not None:
            processor, availability = self.smart_configuration()
            with self._smart_lock:
                self.smart_availability = availability
                self.smart_processor = processor or SmartProcessingUnavailable()

    @property
    def smart_disclosure_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.smart_availability.as_dict()["disclosure"],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    @property
    def smart_processing_available(self) -> bool:
        """Whether this runtime has a real source-bound smart processor."""

        return self.smart_availability.state == "ready" and not isinstance(self.smart_processor, SmartProcessingUnavailable)

    @property
    def smart_processing_disclosure(self) -> str | None:
        if not self.smart_processing_available:
            return None
        value = getattr(self.smart_processor, "disclosure_summary", None)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def request_sha256(
        *,
        client_mutation_id: str,
        day_id: str,
        target: CaptureTarget,
        mode: CaptureMode,
        exact_text: str,
        input_mode: str,
        stated_at: str | None,
        follow_up_action: str | None = None,
        smart_disclosure_sha256: str | None = None,
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "client_mutation_id": client_mutation_id,
                    "day_id": day_id,
                    "target_id": target.value,
                    "mode": mode.value,
                    "exact_text": exact_text,
                    "input_mode": input_mode,
                    "stated_at": stated_at,
                    **({"follow_up_action": follow_up_action} if follow_up_action else {}),
                    **({"smart_disclosure_sha256": smart_disclosure_sha256} if smart_disclosure_sha256 else {}),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def validate(
        *,
        client_mutation_id: str,
        day_id: str,
        target: CaptureTarget,
        mode: CaptureMode,
        exact_text: str,
        input_mode: str,
        stated_at: str | None,
    ) -> str:
        if not client_mutation_id or len(client_mutation_id) > 128:
            raise JournalCaptureValidationError("A stable capture key is required.")
        if not isinstance(exact_text, str) or not exact_text:
            raise JournalCaptureValidationError("Enter something to save.")
        if len(exact_text.encode("utf-8")) > _MAX_CAPTURE_BYTES:
            raise JournalCaptureValidationError("That capture is too large to save at once.")
        if "\x00" in exact_text:
            raise JournalCaptureValidationError("That capture contains unsupported bytes.")
        if target is CaptureTarget.AUTO and mode is not CaptureMode.SMART:
            raise JournalCaptureValidationError("Automatic routing requires smart processing.")
        if input_mode not in _VALID_INPUT_MODES:
            raise JournalCaptureValidationError("The input mode is not supported.")
        if stated_at is not None:
            try:
                datetime.fromisoformat(stated_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise JournalCaptureValidationError("The capture time is invalid.") from exc
        return resolve_day_id(day_id)

    def accept(
        self,
        *,
        ingress: CommittedIngress,
        client_mutation_id: str,
        day_id: str,
        target: CaptureTarget,
        mode: CaptureMode,
        exact_text: str,
        input_mode: str,
        stated_at: str | None,
        submitted_at: str | None = None,
        run_smart: bool = False,
        follow_up_action: str | None = None,
        smart_disclosure_sha256: str | None = None,
    ) -> JournalCapture:
        if follow_up_action is not None and (
            follow_up_action != "task_proposal" or mode is not CaptureMode.DUMB
            or target is not CaptureTarget.RUNNING_NOTES
        ):
            raise JournalCaptureValidationError("Save and propose task uses Running Notes without a model.")
        local_date = self.validate(
            client_mutation_id=client_mutation_id,
            day_id=day_id,
            target=target,
            mode=mode,
            exact_text=exact_text,
            input_mode=input_mode,
            stated_at=stated_at,
        )
        request_sha = self.request_sha256(
            client_mutation_id=client_mutation_id,
            day_id=day_id,
            target=target,
            mode=mode,
            exact_text=exact_text,
            input_mode=input_mode,
            stated_at=stated_at,
            follow_up_action=follow_up_action,
            smart_disclosure_sha256=smart_disclosure_sha256,
        )
        capture = self.store.create_capture(
            client_mutation_id=client_mutation_id,
            request_sha256=request_sha,
            source_ref=ingress.source_ref,
            representation_id=ingress.representation_id,
            submission_id=ingress.submission_id,
            command_id=ingress.command_id,
            source_effect_id=ingress.effect_id,
            source_usage_id=ingress.usage_id,
            day_id=local_date,
            requested_target=target,
            mode=mode,
            input_mode=input_mode,
            stated_at=stated_at,
            submitted_at=submitted_at or datetime.now(UTC).isoformat(),
            authorization_fingerprint=ingress.authorization_fingerprint,
            authorization_expires_at=ingress.authorization_expires_at,
        )
        if target is not CaptureTarget.AUTO:
            self._materialize(capture, exact_text=exact_text, target=target)
        if mode is CaptureMode.SMART and smart_disclosure_sha256 is not None:
            self.store.bind_smart_disclosure(capture.capture_id, smart_disclosure_sha256)
        if follow_up_action == "task_proposal":
            self.store.enqueue_proposal(
                capture.capture_id,
                self._proposal_payload(capture, TaskProposalFollowUp(
                    task_text=exact_text.strip().split("\n", 1)[0][:500] or "Review saved Journal note",
                    rationale="Proposed explicitly from a saved Journal capture; no model was used.",
                )),
                authorization_fingerprint=ingress.authorization_fingerprint,
                authorization_expires_at=ingress.authorization_expires_at,
            )
        if run_smart and mode is CaptureMode.SMART:
            self.process_smart(capture.capture_id, exact_text=exact_text)
        else:
            self.deliver_proposal(capture.capture_id)
        latest = self.store.get_capture(capture.capture_id)
        assert latest is not None
        return latest

    def process_smart(self, capture_id: str, *, exact_text: str) -> JournalCapture:
        capture = self.store.get_capture(capture_id)
        if capture is None:
            raise KeyError("journal_capture_not_found")
        if capture.mode is not CaptureMode.SMART:
            return capture
        if capture.processing_status is ProcessingState.SUCCEEDED:
            # The domain result and its disclosure-manifest binding are
            # already durable. Reconciliation/retry must not send the same
            # private source to a model again.
            if capture.resolved_target is not None:
                self._materialize(capture, exact_text=exact_text, target=capture.resolved_target)
            self.deliver_proposal(capture_id)
            return self.store.get_capture(capture_id) or capture
        effect_type = (
            "auto_route"
            if capture.requested_target is CaptureTarget.AUTO
            else "smart_annotate"
        )
        effect = next(
            (
                item
                for item in self.store.effects_for_capture(capture_id)
                if item.effect_type == effect_type
            ),
            None,
        )
        if effect is None:
            raise KeyError("journal_effect_not_found")
        owner = f"journal-smart:{uuid.uuid4().hex}"
        leased = self.store.lease_effect(effect.effect_id, owner=owner)
        if leased is None:
            current = next(
                (
                    item
                    for item in self.store.effects_for_capture(capture_id)
                    if item.effect_type == effect_type
                ),
                None,
            )
            if current is not None and current.error_code == "journal_authorization_expired":
                return self.store.set_processing(
                    capture_id,
                    status=ProcessingState.FAILED,
                    error_code="journal_authorization_expired",
                )
            if current is not None and current.state.value == "succeeded":
                latest = self.store.get_capture(capture_id)
                assert latest is not None
                return latest
            raise JournalCaptureError(
                "That Journal action is already running.", retryable=True
            )
        self.store.set_processing(capture_id, status=ProcessingState.RUNNING)
        try:
            expected_disclosure = (leased.payload or {}).get("smart_disclosure_sha256")
            with self._smart_lock:
                processor = self.smart_processor
                current_disclosure = self.smart_disclosure_sha256
            if self.smart_configuration is not None and expected_disclosure != current_disclosure:
                raise SmartDisclosureChanged("The Smart provider changed. Review the current disclosure and retry.")
            result = processor.process(capture=capture, exact_text=exact_text)
            if result.target is CaptureTarget.AUTO:
                raise RuntimeError("invalid_smart_target")
            if capture.requested_target is not CaptureTarget.AUTO:
                # Smart annotation must never silently reroute an explicit target.
                resolved_target = capture.requested_target
            else:
                resolved_target = CaptureTarget.RUNNING_NOTES if result.follow_up else result.target
            annotation: dict[str, Any] = {
                "summary": result.summary,
                "effects": list(result.effects),
            }
            if result.producer_ref:
                annotation["producer_ref"] = result.producer_ref
            if result.model_id:
                annotation["model_id"] = result.model_id
            if result.disclosure_manifest_sha256:
                annotation["disclosure_manifest_sha256"] = result.disclosure_manifest_sha256
            settled = self.store.settle_smart(
                capture_id,
                effect_type=effect_type,
                annotation=annotation,
                resolved_target=resolved_target,
                proposal_payload=(self._proposal_payload(capture, result.follow_up) if result.follow_up else None),
            )
            self._materialize(settled, exact_text=exact_text, target=resolved_target)
            self.deliver_proposal(capture_id)
            return self.store.get_capture(capture_id) or settled
        except Exception as exc:
            latest = self.store.get_capture(capture_id)
            if latest is not None and latest.processing_status is ProcessingState.SUCCEEDED:
                # Model output/outbox committed. A later domain-effect failure
                # must never reopen the inference boundary on replay.
                return latest
            code = getattr(exc, "code", None)
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
                code = "smart_processing_failed"
            # Never persist/log the captured content or provider response.
            logger.warning("Journal smart capture failed (%s)", code)
            self.store.finish_effect(
                capture_id,
                effect_type,
                succeeded=False,
                error_code=code,
            )
            return self.store.set_processing(
                capture_id,
                status=ProcessingState.FAILED,
                error_code=code,
            )

    @staticmethod
    def _proposal_payload(capture: JournalCapture, follow_up: TaskProposalFollowUp) -> dict[str, Any]:
        if (not isinstance(follow_up.task_text, str) or not follow_up.task_text.strip()
                or len(follow_up.task_text) > 500 or not isinstance(follow_up.rationale, str)
                or len(follow_up.rationale) > 1000):
            raise JournalCaptureValidationError("The task proposal could not be interpreted safely.")
        return {
            "schema": "wb.journal-task-proposal/v1",
            "client_mutation_id": f"journal-task-proposal:{capture.capture_id}:v1",
            "parameters": {"task_text": follow_up.task_text, "summary": follow_up.rationale},
            "origin": {"kind": "journal_capture", "id": capture.capture_id,
                       "source_ref": capture.source_ref, "sha256": capture.request_sha256,
                       "label": "Journal Quick Capture"},
        }

    def deliver_proposal(self, capture_id: str) -> None:
        """Replay one local delivery effect through Threads' hash-bound ingress."""

        effect = next((item for item in self.store.effects_for_capture(capture_id)
                       if item.effect_type == "task_proposal"), None)
        if effect is None or effect.state.value == "succeeded" or effect.error_code == "journal_proposal_source_withdrawn":
            return
        owner = f"journal-proposal:{uuid.uuid4().hex}"
        leased = self.store.lease_effect(effect.effect_id, owner=owner)
        if leased is None:
            return
        try:
            if self.proposal_service is None:
                raise RuntimeError("proposal_service_unavailable")
            current = self.store.proposal_effect_for_delivery(effect.effect_id, owner=owner)
            if current is None:
                return
            payload = current.payload or {}
            if payload.get("schema") != "wb.journal-task-proposal/v1":
                raise ValueError("invalid_proposal_effect")
            reply = self.proposal_service.create_task_proposal(
                client_mutation_id=str(payload["client_mutation_id"]),
                parameters=dict(payload["parameters"]), origin=dict(payload["origin"]),
                actor="journal_capture",
            )
            proposal = reply.get("proposal")
            thread_id = proposal.get("thread_id") if isinstance(proposal, Mapping) else None
            if reply.get("ok") is not True or not isinstance(thread_id, str) or not re.fullmatch(r"th-[0-9a-f]{8}", thread_id):
                raise ValueError("invalid_proposal_receipt")
            self.store.finish_effect(capture_id, "task_proposal", succeeded=True,
                                     result={"thread_id": thread_id}, lease_owner=owner)
        except Exception:
            # Never copy provider output, exact source text, or arbitrary exception
            # text into a Journal error. The source save is already successful.
            self.store.finish_effect(capture_id, "task_proposal", succeeded=False,
                                     error_code="journal_proposal_delivery_failed", lease_owner=owner)

    def reconcile_proposals(self, *, limit: int = 100) -> dict[str, int]:
        """Explicit maintenance: deliver ingress, then reconcile real task receipts.

        View projections must not invoke this method: a read can show Threads'
        latest link immediately, while canonical note resolution converges here.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Proposal reconciliation limit must be between 1 and 100.")
        report = {"delivery_checked": 0, "resolution_checked": 0, "resolution_synced": 0}
        for effect in self.store.pending_effects(limit=limit, effect_type="task_proposal"):
            self.deliver_proposal(effect.capture_id)
            report["delivery_checked"] += 1
        if self.proposal_service is None:
            return report
        for effect in self.store.proposal_resolution_effects(limit=limit):
            report["resolution_checked"] += 1
            thread_id = (effect.result or {}).get("thread_id")
            if not isinstance(thread_id, str) or not re.fullmatch(r"th-[0-9a-f]{8}", thread_id):
                continue
            proposal = None
            try:
                reply = self.proposal_service.get(thread_id)
                proposal = reply.get("proposal") if reply.get("ok") is True else None
            except Exception:
                pass
            realization = self._proposal_realization(proposal, thread_id)
            status = "realized" if realization else (
                "rejected" if isinstance(proposal, Mapping) and proposal.get("status") == "rejected" else None
            )
            self.store.record_proposal_resolution(effect.capture_id, thread_id=thread_id,
                                                 terminal_status=status, realization=realization)
            report["resolution_synced"] += int(status is not None)
        return report

    @staticmethod
    def _proposal_realization(proposal: Any, thread_id: str) -> dict[str, Any] | None:
        if not isinstance(proposal, Mapping) or proposal.get("thread_id") != thread_id or proposal.get("status") != "realized":
            return None
        receipt = proposal.get("realization")
        if not isinstance(receipt, Mapping):
            return None
        task_id, receipt_id, revision = receipt.get("task_id"), receipt.get("receipt_id"), receipt.get("task_revision")
        if (not isinstance(task_id, str) or not re.fullmatch(r"t-[0-9a-f]{8}", task_id)
                or not isinstance(receipt_id, str) or not receipt_id
                or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1):
            return None
        return {"task_id": task_id, "receipt_id": receipt_id, "task_revision": revision}

    def proposal_follow_ups(self, capture_id: str) -> list[dict[str, Any]]:
        effect = next((item for item in self.store.effects_for_capture(capture_id)
                       if item.effect_type == "task_proposal"), None)
        if effect is None:
            return []
        if effect.error_code == "journal_proposal_source_withdrawn":
            return [{"kind": "status", "status": "failed", "label": "Source removed; the unsent task proposal was canceled."}]
        if effect.state.value != "succeeded":
            return [{"kind": "status", "status": "failed" if effect.state.value in {"failed", "paused"} else "pending",
                     "label": "Saved; task proposal needs another try." if effect.state.value in {"failed", "paused"} else "Preparing task proposal…"}]
        thread_id = (effect.result or {}).get("thread_id")
        if not isinstance(thread_id, str) or not re.fullmatch(r"th-[0-9a-f]{8}", thread_id):
            return []
        href = f"/app/tasks?proposal={thread_id}"
        description = "Task proposal saved. Open Tasks to check its current status."
        if self.proposal_service is not None:
            try:
                reply = self.proposal_service.get(thread_id)
                proposal = reply.get("proposal") if reply.get("ok") is True else None
                realization = self._proposal_realization(proposal, thread_id)
                if realization is not None:
                    href = f"/app/tasks?task={realization['task_id']}"
                    description = "Task created from this proposal."
                elif isinstance(proposal, Mapping) and proposal.get("status") in {"rejected", "dismissed"}:
                    description = "Task proposal dismissed; this Journal note remains open."
                elif isinstance(proposal, Mapping) and proposal.get("status") == "ready":
                    description = "Task proposal ready — no task has been created."
                elif isinstance(proposal, Mapping) and proposal.get("status") == "executing":
                    description = "Task creation is being confirmed. Review progress in Tasks."
                elif isinstance(proposal, Mapping) and proposal.get("status") == "needs_attention":
                    description = "The task proposal needs attention. Review it in Tasks."
            except Exception:
                # The durable proposal reference survives a temporarily unavailable projection.
                pass
        return [{"kind": "app_link", "referenceId": thread_id,
                 "label": "Review in Tasks", "description": description, "href": href}]

    def retry_materialization(self, capture_id: str, *, exact_text: str) -> JournalCapture:
        capture = self.store.get_capture(capture_id)
        if capture is None:
            raise KeyError("journal_capture_not_found")
        target = capture.resolved_target or capture.requested_target
        if target is CaptureTarget.AUTO:
            return self.process_smart(capture_id, exact_text=exact_text)
        self._materialize(capture, exact_text=exact_text, target=target)
        latest = self.store.get_capture(capture_id)
        assert latest is not None
        return latest

    def _materialize(
        self,
        capture: JournalCapture,
        *,
        exact_text: str,
        target: CaptureTarget,
    ) -> None:
        digest = hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
        entry_id = hashlib.sha256(
            f"journal-entry:{capture.capture_id}".encode("utf-8")
        ).hexdigest()[:32]
        entry = self.store.ensure_entry(
            capture_id=capture.capture_id,
            entry_kind=target,
            markdown=exact_text,
            content_sha256=digest,
            projection_marker=marker_for(entry_id, digest),
            created_at=capture.stated_at or capture.submitted_at,
        )
        try:
            if target is CaptureTarget.LOG and self.authoritative_log_writer is not None:
                cursor = self.authoritative_log_writer(
                    entry,
                    stated_at=capture.stated_at,
                )
                if cursor is not None:
                    file_sha = cursor.file_sha256 or cursor.section_sha256
                    if file_sha is None:
                        raise JournalProjectionError(
                            "The authoritative Journal Log projection is incomplete."
                        )
                    self.store.mark_projection_committed(
                        entry.entry_id,
                        base_sha256=file_sha,
                        result_sha256=file_sha,
                    )
                    return
            path = self.adapter.journal_path(capture.day_id)
            base_content = (
                self.adapter.read_day(capture.day_id) if path.is_file() else ""
            )
            base_sha = hashlib.sha256(base_content.encode("utf-8")).hexdigest()
            self.store.mark_projection_prepared(entry.entry_id, base_sha256=base_sha)
            result = self.adapter.append(entry, stated_at=capture.stated_at)
            self.store.mark_projection_committed(
                entry.entry_id,
                base_sha256=result.base_sha256,
                result_sha256=result.result_sha256,
            )
        except JournalProjectionError as exc:
            self.store.mark_projection_failed(entry.entry_id, error_code=exc.code)


def resolve_day_id(day_id: str) -> str:
    if not isinstance(day_id, str) or not day_id.startswith(_DAY_ID_PREFIX):
        raise JournalCaptureValidationError("The Journal day is invalid.")
    body = day_id[len(_DAY_ID_PREFIX) :]
    try:
        local_date, remainder = body.split(":", 1)
        timezone_name, hour, minute = remainder.rsplit(":", 2)
        boundary = f"{hour}:{minute}"
        datetime.fromisoformat(local_date)
    except (ValueError, TypeError) as exc:
        raise JournalCaptureValidationError("The Journal day is invalid.") from exc
    window = get_journal_day_window(local_date)
    expected = f"{_DAY_ID_PREFIX}{local_date}:{window.timezone}:{window.boundary}"
    if day_id != expected or timezone_name != window.timezone or boundary != window.boundary:
        raise JournalCaptureValidationError("The Journal day policy changed; refresh and try again.")
    return local_date
