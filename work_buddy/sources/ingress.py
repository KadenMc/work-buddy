"""Trusted first-party ingress that persists exact input before effects."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
from typing import Any, Mapping

from work_buddy.sources.errors import InvalidSourceRequest
from work_buddy.sources.models import (
    ActorRef,
    AttributionAssertion,
    SourceRef,
    canonical_json,
    canonical_sha256,
    new_id,
    sha256_bytes,
    utc_now,
    validate_sha256,
)
from work_buddy.sources.store import INPUT_MODES, SourceStore, _actor_json


_FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "content",
        "exact_text",
        "text",
        "raw",
        "raw_text",
        "body",
        "prompt",
        "excerpt",
        "quote",
        "bytes",
    }
)


def _assert_reference_only(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise InvalidSourceRequest()
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise InvalidSourceRequest()
        return
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise InvalidSourceRequest()
        for key, item in value.items():
            if not isinstance(key, str) or key in _FORBIDDEN_CONTENT_KEYS:
                raise InvalidSourceRequest()
            _assert_reference_only(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise InvalidSourceRequest()
        for item in value:
            _assert_reference_only(item, depth=depth + 1)
        return
    raise InvalidSourceRequest()


@dataclass(frozen=True, slots=True)
class DomainCommand:
    """A content-free, versioned command atomically paired with an ingress."""

    schema: str
    target_domain: str
    command_type: str
    parameters: Mapping[str, Any]
    authorization_fingerprint: str
    authorization_expires_at: str | None = None

    def __post_init__(self) -> None:
        for value in (self.schema, self.target_domain, self.command_type):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise InvalidSourceRequest()
        validate_sha256(self.authorization_fingerprint)
        _assert_reference_only(self.parameters)
        if len(canonical_json(dict(self.parameters)).encode("utf-8")) > 64 * 1024:
            raise InvalidSourceRequest()


@dataclass(frozen=True, slots=True)
class TrustedIngressContext:
    """Server-constructed identity and authorization context.

    This value establishes the submitting principal and surface at the stated
    assurance.  It intentionally contains no field that lets the request call
    its content human-authored.
    """

    issuer: ActorRef
    issuer_version: str
    inputter: ActorRef
    service_principal: ActorRef
    tenant_scope_id: str
    surface: str
    namespace: str | None
    sensitivity_class: str
    retention_class: str
    inputter_assurance: str
    authorization_fingerprint: str
    permitted_purposes: tuple[str, ...] = ()
    gesture_receipt_id: str | None = None
    gesture_context_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            self.issuer.tenant_scope_id != self.tenant_scope_id
            or self.inputter.tenant_scope_id != self.tenant_scope_id
            or self.service_principal.tenant_scope_id != self.tenant_scope_id
        ):
            raise InvalidSourceRequest()
        for value in (
            self.issuer_version,
            self.tenant_scope_id,
            self.surface,
            self.sensitivity_class,
            self.retention_class,
            self.inputter_assurance,
        ):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise InvalidSourceRequest()
        validate_sha256(self.authorization_fingerprint)
        if self.gesture_context_sha256 is not None:
            validate_sha256(self.gesture_context_sha256)
        if len(self.permitted_purposes) > 32 or any(
            not isinstance(item, str) or not item or len(item) > 128
            for item in self.permitted_purposes
        ):
            raise InvalidSourceRequest()


@dataclass(frozen=True, slots=True)
class HumanInputRequest:
    exact_content: str | bytes
    client_mutation_id: str
    input_mode: str
    media_type: str = "text/plain"
    occurred_at: str | None = None
    command: DomainCommand | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.exact_content, (str, bytes)):
            raise InvalidSourceRequest()
        if (
            not isinstance(self.client_mutation_id, str)
            or not (8 <= len(self.client_mutation_id) <= 256)
            or any(ord(ch) < 0x20 for ch in self.client_mutation_id)
        ):
            raise InvalidSourceRequest()
        if self.input_mode not in INPUT_MODES:
            raise InvalidSourceRequest()
        if not isinstance(self.media_type, str) or not self.media_type:
            raise InvalidSourceRequest()


@dataclass(frozen=True, slots=True)
class HumanInputCommit:
    source_ref: SourceRef
    representation_id: str
    submission_id: str
    command_id: str | None
    effect_id: str | None
    persisted_at: str
    deduplicated: bool


class TrustedIngressService:
    """Persist exact first-party input and a requested effect in one DB commit."""

    def __init__(self, store: SourceStore) -> None:
        self.store = store

    def commit_human_input(
        self,
        context: TrustedIngressContext,
        request: HumanInputRequest,
    ) -> HumanInputCommit:
        content, encoding, representation_kind = self.store._normalize_content(
            request.exact_content,
            encoding="utf-8" if isinstance(request.exact_content, str) else None,
        )
        if request.command is not None:
            serialized_parameters = canonical_json(dict(request.command.parameters))
            # The command is reference-only.  Key validation above catches the
            # usual content fields; these comparisons catch content hidden
            # under a misleading key without rejecting tiny incidental tokens.
            if len(content) >= 8:
                candidates = {
                    base64.b64encode(content).decode("ascii"),
                    content.hex(),
                }
                try:
                    candidates.add(content.decode("utf-8"))
                except UnicodeDecodeError:
                    pass
                if any(candidate and candidate in serialized_parameters for candidate in candidates):
                    raise InvalidSourceRequest()
        request_hash = canonical_sha256(
            {
                "content_sha256": sha256_bytes(content),
                "byte_length": len(content),
                "encoding": encoding,
                "media_type": request.media_type,
                "input_mode": request.input_mode,
                "occurred_at": request.occurred_at,
                "issuer": context.issuer.to_dict(),
                "issuer_version": context.issuer_version,
                "inputter": context.inputter.to_dict(),
                "service_principal": context.service_principal.to_dict(),
                "tenant_scope_id": context.tenant_scope_id,
                "surface": context.surface,
                "namespace": context.namespace,
                "sensitivity_class": context.sensitivity_class,
                "retention_class": context.retention_class,
                "inputter_assurance": context.inputter_assurance,
                "permitted_purposes": list(context.permitted_purposes),
                "command": self._semantic_command_payload(request.command),
            }
        )

        # Avoid writing a new staged blob for an already committed retry.  The
        # second check inside BEGIN IMMEDIATE closes the concurrent-writer race.
        conn = self.store.connect()
        try:
            existing = self.store.idempotency_result(
                conn,
                tenant_scope_id=context.tenant_scope_id,
                issuer=context.issuer,
                principal=context.inputter,
                client_mutation_id=request.client_mutation_id,
                request_sha256=request_hash,
            )
        finally:
            conn.close()
        if existing is not None:
            return self._commit_from_result(existing, deduplicated=True)

        with self.store.write_transaction() as conn:
            existing = self.store.idempotency_result(
                conn,
                tenant_scope_id=context.tenant_scope_id,
                issuer=context.issuer,
                principal=context.inputter,
                client_mutation_id=request.client_mutation_id,
                request_sha256=request_hash,
            )
            if existing is not None:
                return self._commit_from_result(existing, deduplicated=True)

            staged = self.store._stage_if_needed(content, conn=conn)
            now = utc_now()
            attributions = (
                AttributionAssertion(
                    role="inputter",
                    actor=context.inputter,
                    basis="trusted_ingress",
                    assurance=context.inputter_assurance,
                    asserted_by=context.issuer,
                    observed_at=now,
                ),
                AttributionAssertion(
                    role="issuer",
                    actor=context.issuer,
                    basis="server_constructed_context",
                    assurance="trusted_component",
                    asserted_by=context.issuer,
                    observed_at=now,
                ),
                AttributionAssertion(
                    role="author",
                    actor=None,
                    state="unknown",
                    basis="not_determined",
                    assurance="unknown",
                    asserted_by=context.issuer,
                    observed_at=now,
                ),
            )
            item = self.store._capture_source(
                conn,
                content=content,
                staged_blob=staged,
                source_role="human_input",
                tenant_scope_id=context.tenant_scope_id,
                originating_surface=context.surface,
                media_type=request.media_type,
                representation_kind=representation_kind,
                encoding=encoding,
                schema_type=None,
                origin_ref=None,
                native_revision=None,
                fidelity="exact_submitted_payload",
                namespace=context.namespace,
                sensitivity_class=context.sensitivity_class,
                retention_class=context.retention_class,
                occurred_at=request.occurred_at,
                provider_observed_at=None,
                received_at=now,
                attributions=attributions,
                producer=context.issuer,
            )
            submission_id = new_id()
            conn.execute(
                "INSERT INTO ingress_submissions "
                "(submission_id, authority_id, source_item_id, representation_id, "
                " issuer_ref_json, inputter_ref_json, input_mode, gesture_receipt_id, "
                " authorization_fingerprint, occurred_at, received_at, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    submission_id,
                    item.source_ref.authority_id,
                    item.source_ref.item_id,
                    item.primary_representation_id,
                    _actor_json(context.issuer),
                    _actor_json(context.inputter),
                    request.input_mode,
                    context.gesture_receipt_id,
                    context.authorization_fingerprint,
                    request.occurred_at,
                    now,
                    item.committed_at,
                ),
            )
            for purpose in context.permitted_purposes:
                self.store._grant_access(
                    conn,
                    source_ref=item.source_ref,
                    principal=context.service_principal,
                    purpose=purpose,
                    access_mode="content",
                    authorization_fingerprint=context.authorization_fingerprint,
                    scope={
                        "tenant_scope_id": context.tenant_scope_id,
                        "surface": context.surface,
                    },
                    trusted_service_id=context.issuer.subject,
                    gesture_receipt_id=context.gesture_receipt_id,
                )

            command_id: str | None = None
            effect_id: str | None = None
            if request.command is not None:
                command_id = new_id()
                effect_id = new_id()
                parameters = dict(request.command.parameters)
                parameters_json = canonical_json(parameters)
                parameters_hash = sha256_bytes(parameters_json.encode("utf-8"))
                conn.execute(
                    "INSERT INTO source_commands "
                    "(command_id, submission_id, command_schema, target_domain, "
                    " command_type, parameters_json, parameters_sha256, "
                    " authorization_fingerprint, authorization_expires_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        command_id,
                        submission_id,
                        request.command.schema,
                        request.command.target_domain,
                        request.command.command_type,
                        parameters_json,
                        parameters_hash,
                        request.command.authorization_fingerprint,
                        request.command.authorization_expires_at,
                        now,
                    ),
                )
                effect_payload = {
                    "schema": request.command.schema,
                    "source_ref": item.source_ref.to_dict(),
                    "representation_id": item.primary_representation_id,
                    "submission_id": submission_id,
                    "command_id": command_id,
                    "parameters": parameters,
                    "parameters_sha256": parameters_hash,
                }
                effect_json = canonical_json(effect_payload)
                conn.execute(
                    "INSERT INTO source_outbox "
                    "(effect_id, command_id, target_domain, effect_type, payload_json, "
                    " payload_sha256, authorization_fingerprint, authorization_expires_at, "
                    " status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        effect_id,
                        command_id,
                        request.command.target_domain,
                        request.command.command_type,
                        effect_json,
                        sha256_bytes(effect_json.encode("utf-8")),
                        request.command.authorization_fingerprint,
                        request.command.authorization_expires_at,
                        now,
                        now,
                    ),
                )
            result = {
                "source_ref": item.source_ref.to_dict(),
                "representation_id": item.primary_representation_id,
                "submission_id": submission_id,
                "command_id": command_id,
                "effect_id": effect_id,
                "persisted_at": item.committed_at,
            }
            self.store.record_idempotency(
                conn,
                tenant_scope_id=context.tenant_scope_id,
                issuer=context.issuer,
                principal=context.inputter,
                client_mutation_id=request.client_mutation_id,
                request_sha256=request_hash,
                result=result,
            )
            return self._commit_from_result(result, deduplicated=False)

    @staticmethod
    def _semantic_command_payload(
        command: DomainCommand | None,
    ) -> Mapping[str, Any] | None:
        """Return only command meaning, excluding per-attempt authorization."""

        if command is None:
            return None
        return {
            "schema": command.schema,
            "target_domain": command.target_domain,
            "command_type": command.command_type,
            "parameters": dict(command.parameters),
        }

    @staticmethod
    def _commit_from_result(
        result: Mapping[str, Any], *, deduplicated: bool
    ) -> HumanInputCommit:
        source = result.get("source_ref")
        if not isinstance(source, Mapping):
            raise InvalidSourceRequest()
        return HumanInputCommit(
            source_ref=SourceRef.from_dict(source),
            representation_id=str(result["representation_id"]),
            submission_id=str(result["submission_id"]),
            command_id=(None if result.get("command_id") is None else str(result["command_id"])),
            effect_id=(None if result.get("effect_id") is None else str(result["effect_id"])),
            persisted_at=str(result["persisted_at"]),
            deduplicated=deduplicated,
        )
