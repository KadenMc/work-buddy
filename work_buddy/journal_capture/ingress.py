"""Reusable Source-first ingress for Journal capture surfaces.

HTTP, Telegram, and MCP adapters all arrive here after authenticating their
own surface.  The caller supplies a server-constructed
``TrustedIngressContext``; exact content is committed to Sources before the
reference-only Journal command is dispatched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from typing import Any

from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.models import (
    CaptureMode,
    CaptureTarget,
    JournalCapture,
    JournalCaptureError,
    JournalCaptureValidationError,
    JournalCutoverPaused,
)
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.security.actors import ActorRef
from work_buddy.sources.dispatch import SourceOutbox
from work_buddy.sources.ingress import (
    AgentOutputRequest,
    DomainCommand,
    HumanInputCommit,
    HumanInputRequest,
    TrustedIngressContext,
    TrustedIngressService,
)
from work_buddy.sources.models import canonical_sha256
from work_buddy.sources.store import SourceStore


@dataclass(frozen=True, slots=True)
class JournalIngressResult:
    """Durable receipt for one exact Journal submission."""

    commit: HumanInputCommit
    capture: JournalCapture


class JournalIngressQueued(JournalCaptureError):
    """The exact Source command is durable but cutover holds delivery."""

    code = "journal_capture_queued_for_cutover"
    retryable = True

    def __init__(self, commit: HumanInputCommit) -> None:
        super().__init__(
            "The capture is saved and queued while Journal cutover maintenance finishes.",
            retryable=True,
        )
        self.commit = commit


class JournalCaptureIngress:
    """Commit exact input and synchronously settle its Journal command."""

    def __init__(
        self,
        sources: SourceStore,
        service: JournalCaptureService,
        *,
        service_principal: ActorRef,
        worker_id: str,
    ) -> None:
        self.sources = sources
        self.service = service
        self.service_principal = service_principal
        self.worker_id = worker_id

    def submit(
        self,
        *,
        trusted: TrustedIngressContext,
        exact_text: str,
        client_mutation_id: str,
        day_id: str,
        target: CaptureTarget,
        mode: CaptureMode,
        input_mode: str,
        stated_at: str | None = None,
        authorization_expires_at: str | None = None,
        follow_up_action: str | None = None,
        smart_disclosure_sha256: str | None = None,
    ) -> JournalIngressResult:
        # Reject malformed requests before retaining their content.  The same
        # validation runs again at delivery so replay cannot bypass it.
        self.service.validate(
            client_mutation_id=client_mutation_id,
            day_id=day_id,
            target=target,
            mode=mode,
            exact_text=exact_text,
            input_mode=input_mode,
            stated_at=stated_at,
        )
        if follow_up_action is not None and (
            follow_up_action != "task_proposal"
            or mode is not CaptureMode.DUMB
            or target is not CaptureTarget.RUNNING_NOTES
        ):
            raise JournalCaptureValidationError(
                "Save and propose task uses Running Notes without a model."
            )

        self.service.refresh_smart_availability()
        command = DomainCommand(
            schema="wb.journal-capture/v1",
            target_domain="journal",
            command_type="journal.capture.materialize",
            parameters={
                "client_mutation_id": client_mutation_id,
                "day_id": day_id,
                "target_id": target.value,
                "mode": mode.value,
                "input_mode": input_mode,
                "stated_at": stated_at,
                **(
                    {"follow_up_action": follow_up_action}
                    if follow_up_action
                    else {}
                ),
                **(
                    {"smart_disclosure_sha256": smart_disclosure_sha256}
                    if smart_disclosure_sha256
                    else {}
                ),
            },
            authorization_fingerprint=trusted.authorization_fingerprint,
            authorization_expires_at=authorization_expires_at,
        )
        commit = TrustedIngressService(self.sources).commit_human_input(
            trusted,
            HumanInputRequest(
                exact_content=exact_text,
                client_mutation_id=client_mutation_id,
                input_mode=input_mode,
                occurred_at=stated_at,
                command=command,
            ),
        )
        if commit.command_id is None or commit.effect_id is None:
            raise RuntimeError("journal_source_command_missing")

        current_effect = SourceOutbox(self.sources).get(commit.effect_id)
        if current_effect is not None and current_effect.status in {
            "pending",
            "retryable",
            "paused",
        }:
            SourceOutbox(self.sources).reauthorize(
                commit.effect_id,
                authorization_fingerprint=trusted.authorization_fingerprint,
                authorization_expires_at=authorization_expires_at,
            )
        try:
            capture_id = JournalSourceDispatcher(
                self.sources,
                self.service,
                service_principal=self.service_principal,
                worker_id=self.worker_id,
            ).deliver_exact(commit.effect_id)
        except JournalCutoverPaused as exc:
            raise JournalIngressQueued(commit) from exc
        capture = self.service.store.get_capture(capture_id)
        if capture is None:
            raise RuntimeError("journal_capture_receipt_missing")
        return JournalIngressResult(commit=commit, capture=capture)


@dataclass(frozen=True, slots=True)
class TelegramIngressIdentity:
    """Content-free identity for one allowlisted Telegram message."""

    trusted: TrustedIngressContext
    client_mutation_id: str
    service_principal: ActorRef


@dataclass(frozen=True, slots=True)
class AgentOutputIngressIdentity:
    """Content-free identity for an MCP-authored Journal value."""

    trusted: TrustedIngressContext
    client_mutation_id: str
    agent: ActorRef
    service_principal: ActorRef


@dataclass(frozen=True, slots=True)
class HumanFieldIngressIdentity:
    """Content-free identity for a user value relayed by an agent session."""

    trusted: TrustedIngressContext
    client_mutation_id: str
    inputter: ActorRef
    service_principal: ActorRef


def telegram_ingress_identity(
    *,
    enrolled_actor: ActorRef,
    chat_id: int,
    message_id: int,
    update_id: int,
    user_id: int | None,
) -> TelegramIngressIdentity:
    """Bind an allowlisted Telegram delivery to the enrolled local actor.

    Telegram proves the transport identity and allowlist membership, not who
    authored the words.  Sources therefore records the enrolled actor as the
    trusted inputter while its existing attribution contract leaves authorship
    unknown.
    """

    message_identity: dict[str, Any] = {
        "schema": "wb.telegram-journal-gesture/v1",
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "update_id": int(update_id),
        "user_id": None if user_id is None else int(user_id),
    }
    digest = canonical_sha256(message_identity)
    issuer = ActorRef(
        issuer_authority_id=enrolled_actor.issuer_authority_id,
        subject="work-buddy-telegram",
        kind="service",
        tenant_scope_id=enrolled_actor.tenant_scope_id,
    )
    service_principal = ActorRef(
        issuer_authority_id=enrolled_actor.issuer_authority_id,
        subject="work-buddy-journal-service",
        kind="service",
        tenant_scope_id=enrolled_actor.tenant_scope_id,
    )
    fingerprint = canonical_sha256(
        {
            **message_identity,
            "inputter": enrolled_actor.to_dict(),
            "issuer": issuer.to_dict(),
            "assurance": "allowlisted_telegram_chat",
        }
    )
    trusted = TrustedIngressContext(
        issuer=issuer,
        issuer_version="telegram-bot/v1",
        inputter=enrolled_actor,
        service_principal=service_principal,
        tenant_scope_id=enrolled_actor.tenant_scope_id,
        surface="work-buddy-telegram",
        namespace="journal-running-notes",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="allowlisted_telegram_chat",
        authorization_fingerprint=fingerprint,
        permitted_purposes=("journal.materialize",),
        gesture_receipt_id=f"telegram-message-{digest[:32]}",
        gesture_context_sha256=digest,
    )
    return TelegramIngressIdentity(
        trusted=trusted,
        client_mutation_id=f"telegram:{digest}",
        service_principal=service_principal,
    )


def agent_output_ingress_identity(
    *,
    enrolled_actor: ActorRef,
    session_id: str,
    operation: str,
    semantic_request: dict[str, Any],
) -> AgentOutputIngressIdentity:
    """Create stable provenance for an agent-run Journal mutation."""

    if not session_id or not operation:
        raise ValueError("agent Journal output requires session and operation identity")
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    agent = ActorRef(
        issuer_authority_id=enrolled_actor.issuer_authority_id,
        subject=f"agent-run-{session_digest[:32]}",
        kind="agent_run",
        tenant_scope_id=enrolled_actor.tenant_scope_id,
    )
    issuer = ActorRef(
        issuer_authority_id=enrolled_actor.issuer_authority_id,
        subject="work-buddy-mcp-gateway",
        kind="service",
        tenant_scope_id=enrolled_actor.tenant_scope_id,
    )
    service_principal = ActorRef(
        issuer_authority_id=enrolled_actor.issuer_authority_id,
        subject="work-buddy-journal-service",
        kind="service",
        tenant_scope_id=enrolled_actor.tenant_scope_id,
    )
    request_identity = {
        "schema": "wb.journal-agent-output/v1",
        "agent": agent.to_dict(),
        "operation": operation,
        "request": semantic_request,
    }
    request_digest = canonical_sha256(request_identity)
    trusted = TrustedIngressContext(
        issuer=issuer,
        issuer_version="mcp-gateway/v1",
        inputter=agent,
        service_principal=service_principal,
        tenant_scope_id=enrolled_actor.tenant_scope_id,
        surface="work-buddy-mcp",
        namespace="journal-agent-output",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="session_attributed_agent_call",
        authorization_fingerprint=canonical_sha256(
            {
                **request_identity,
                "issuer": issuer.to_dict(),
                "assurance": "session_attributed_agent_call",
            }
        ),
        permitted_purposes=("journal.native_item", "journal.field_value"),
        gesture_context_sha256=request_digest,
    )
    return AgentOutputIngressIdentity(
        trusted=trusted,
        client_mutation_id=f"journal-agent:{request_digest}",
        agent=agent,
        service_principal=service_principal,
    )


def human_field_ingress_identity(
    *,
    enrolled_actor: ActorRef,
    session_id: str,
    semantic_request: dict[str, Any],
) -> HumanFieldIngressIdentity:
    """Bind an MCP sign-in value to the enrolled user and relaying session.

    The capability contract treats ``write_fields`` as user-supplied Journal
    input.  The gateway remains the issuer, and the session digest records the
    relay without claiming that the agent authored the value.
    """

    if not session_id:
        raise ValueError("Journal field input requires an attributed agent session")
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    issuer = ActorRef(
        issuer_authority_id=enrolled_actor.issuer_authority_id,
        subject="work-buddy-mcp-gateway",
        kind="service",
        tenant_scope_id=enrolled_actor.tenant_scope_id,
    )
    service_principal = ActorRef(
        issuer_authority_id=enrolled_actor.issuer_authority_id,
        subject="work-buddy-journal-service",
        kind="service",
        tenant_scope_id=enrolled_actor.tenant_scope_id,
    )
    request_identity = {
        "schema": "wb.journal-human-field/v1",
        "inputter": enrolled_actor.to_dict(),
        "relaySessionSha256": session_digest,
        "request": semantic_request,
    }
    request_digest = canonical_sha256(request_identity)
    trusted = TrustedIngressContext(
        issuer=issuer,
        issuer_version="mcp-gateway/v1",
        inputter=enrolled_actor,
        service_principal=service_principal,
        tenant_scope_id=enrolled_actor.tenant_scope_id,
        surface="work-buddy-mcp",
        namespace="journal-field-input",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="session_attributed_human_field",
        authorization_fingerprint=canonical_sha256(
            {
                **request_identity,
                "issuer": issuer.to_dict(),
                "assurance": "session_attributed_human_field",
            }
        ),
        permitted_purposes=("journal.field_value",),
        gesture_context_sha256=request_digest,
    )
    return HumanFieldIngressIdentity(
        trusted=trusted,
        client_mutation_id=f"journal-field:{request_digest}",
        inputter=enrolled_actor,
        service_principal=service_principal,
    )


def commit_human_field_source(
    sources: SourceStore,
    *,
    identity: HumanFieldIngressIdentity,
    exact_value: str,
    occurred_at: str | None,
) -> HumanInputCommit:
    """Idempotently retain one exact user field representation."""

    return TrustedIngressService(sources).commit_human_input(
        identity.trusted,
        HumanInputRequest(
            exact_content=exact_value,
            client_mutation_id=identity.client_mutation_id,
            input_mode="direct_entry",
            occurred_at=occurred_at,
        ),
    )


def commit_agent_output_source(
    sources: SourceStore,
    *,
    identity: AgentOutputIngressIdentity,
    exact_text: str,
    occurred_at: str | None,
) -> HumanInputCommit:
    """Idempotently retain one exact agent output before Journal mutation."""

    return TrustedIngressService(sources).commit_agent_output(
        identity.trusted,
        AgentOutputRequest(
            exact_content=exact_text,
            client_mutation_id=identity.client_mutation_id,
            input_mode="automation",
            occurred_at=occurred_at,
        ),
    )


def telegram_message_time(value: datetime | None) -> str | None:
    """Normalize Telegram's optional message timestamp without reading prose."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


__all__ = [
    "AgentOutputIngressIdentity",
    "HumanFieldIngressIdentity",
    "JournalCaptureIngress",
    "JournalIngressQueued",
    "JournalIngressResult",
    "TelegramIngressIdentity",
    "agent_output_ingress_identity",
    "commit_agent_output_source",
    "commit_human_field_source",
    "human_field_ingress_identity",
    "telegram_ingress_identity",
    "telegram_message_time",
]
