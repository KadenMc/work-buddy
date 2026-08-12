"""Journal capture orchestration over Sources and the compatibility adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol

from work_buddy.journal_capture.content_adapter import JournalContentAdapter, marker_for
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCapture,
    JournalCaptureError,
    JournalCaptureValidationError,
    JournalProjectionError,
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
class SmartCaptureResult:
    target: CaptureTarget
    summary: str
    effects: tuple[str, ...]
    producer_ref: str | None = None
    model_id: str | None = None
    disclosure_manifest_sha256: str | None = None


class SmartCaptureProcessor(Protocol):
    def process(self, *, capture: JournalCapture, exact_text: str) -> SmartCaptureResult: ...


class AuthoritativeLogWriter(Protocol):
    def __call__(self, entry, *, stated_at: str | None): ...


class SmartProcessingUnavailable:
    def process(self, *, capture: JournalCapture, exact_text: str) -> SmartCaptureResult:
        del capture, exact_text
        raise RuntimeError("smart_processing_unavailable")


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
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.smart_processor = smart_processor or SmartProcessingUnavailable()
        self.authoritative_log_writer = authoritative_log_writer

    @property
    def smart_processing_available(self) -> bool:
        """Whether this runtime has a real source-bound smart processor."""

        return not isinstance(self.smart_processor, SmartProcessingUnavailable)

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
    ) -> JournalCapture:
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
        if run_smart and mode is CaptureMode.SMART:
            self.process_smart(capture.capture_id, exact_text=exact_text)
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
            return capture
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
            result = self.smart_processor.process(capture=capture, exact_text=exact_text)
            if result.target is CaptureTarget.AUTO:
                raise RuntimeError("invalid_smart_target")
            if capture.requested_target is not CaptureTarget.AUTO:
                # Smart annotation must never silently reroute an explicit target.
                resolved_target = capture.requested_target
            else:
                resolved_target = result.target
                self._materialize(capture, exact_text=exact_text, target=resolved_target)
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
            self.store.finish_effect(
                capture_id,
                effect_type,
                succeeded=True,
            )
            return self.store.set_processing(
                capture_id,
                status=ProcessingState.SUCCEEDED,
                annotation=annotation,
                resolved_target=resolved_target,
            )
        except Exception as exc:
            code = getattr(exc, "code", None) or str(exc)
            if not isinstance(code, str) or not code or len(code) > 128:
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
