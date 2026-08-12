"""Source-side adapter for Agent Execution's disclosure manifest boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from work_buddy.sources.errors import (
    InvalidSourceRequest,
    SourceIntegrityFailure,
    SourceIdempotencyConflict,
    SourceNotFound,
    SourceRedacted,
    SourceUsageConflict,
)
from work_buddy.sources.models import (
    AccessBinding,
    ActorRef,
    AttributionAssertion,
    SourceRef,
    canonical_json,
    canonical_sha256,
    new_id,
    sha256_bytes,
    utc_now,
)
from work_buddy.sources.resolve import resolve_and_reserve_source
from work_buddy.sources.store import SourceStore


@dataclass(frozen=True, slots=True)
class CapturedDisclosureSource:
    source_ref: str
    representation_id: str
    content_sha256: str
    byte_length: int


class SourcesDisclosureService:
    """Duck-typed implementation of Agent Execution's Sources adapter.

    Agent Execution owns run/send state.  This service only retains dynamic
    bytes, grants the exact run principal a bounded use, resolves and reserves
    that use, and records a known sent/not-sent outcome.
    """

    def __init__(
        self,
        store: SourceStore,
        *,
        tenant_scope_id: str,
        issuer: ActorRef | None = None,
    ) -> None:
        self.store = store
        self.tenant_scope_id = tenant_scope_id
        self.issuer = issuer or ActorRef(
            issuer_authority_id=store.authority_id,
            subject="agent-execution-service",
            kind="service",
            tenant_scope_id=tenant_scope_id,
        )
        if self.issuer.tenant_scope_id != tenant_scope_id:
            raise InvalidSourceRequest()

    def _run_principal(self, run_id: str) -> ActorRef:
        if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
            raise InvalidSourceRequest()
        return ActorRef(
            issuer_authority_id=self.issuer.issuer_authority_id,
            subject=f"agent-run-{sha256_bytes(run_id.encode('utf-8'))[:32]}",
            kind="agent_run",
            tenant_scope_id=self.tenant_scope_id,
        )

    @staticmethod
    def _authorization_fingerprint(authorization_ref: str) -> str:
        if not isinstance(authorization_ref, str) or not authorization_ref:
            raise InvalidSourceRequest()
        return canonical_sha256({"authorization_ref": authorization_ref})

    @staticmethod
    def _attribution_request_value(
        assertion: AttributionAssertion,
    ) -> dict[str, Any]:
        return {
            "role": assertion.role,
            "actor": assertion.actor.to_dict() if assertion.actor else None,
            "state": assertion.state,
            "basis": assertion.basis,
            "assurance": assertion.assurance,
            "asserted_by": (
                assertion.asserted_by.to_dict() if assertion.asserted_by else None
            ),
            "selector": dict(assertion.selector) if assertion.selector else None,
            "observed_at": assertion.observed_at,
            "supersedes_id": assertion.supersedes_id,
        }

    def _capture_attribution(
        self,
        *,
        source_role: str,
        principal: ActorRef,
        source_attributions: Sequence[AttributionAssertion],
        source_producer: ActorRef | None,
    ) -> tuple[tuple[AttributionAssertion, ...], ActorRef]:
        """Keep capture mechanics distinct from authorship assertions.

        Only content produced by this run is attributed to the run principal.
        Retaining a document passage, fetched page, or derived context does not
        make the current agent its author.  Callers may carry forward known
        source attribution; otherwise the author remains explicitly unknown
        and the service is recorded only as the representation producer.
        """

        supplied = tuple(source_attributions)
        for assertion in supplied:
            if not isinstance(assertion, AttributionAssertion):
                raise InvalidSourceRequest()
            for actor in (assertion.actor, assertion.asserted_by):
                if actor is not None and actor.tenant_scope_id != self.tenant_scope_id:
                    raise InvalidSourceRequest()
        if (
            source_producer is not None
            and source_producer.tenant_scope_id != self.tenant_scope_id
        ):
            raise InvalidSourceRequest()

        if source_role == "agent_output":
            author = AttributionAssertion(
                role="author",
                actor=principal,
                basis="agent_execution_output",
                assurance="run_manifest",
                asserted_by=self.issuer,
            )
            producer = principal
        else:
            if source_producer == principal or any(
                assertion.role == "author" and assertion.actor == principal
                for assertion in supplied
            ):
                raise InvalidSourceRequest()
            author = AttributionAssertion(
                role="author",
                actor=None,
                state="unknown",
                basis="not_determined",
                assurance="unknown",
                asserted_by=self.issuer,
            )
            producer = source_producer or self.issuer

        values = list(supplied)
        if source_role == "agent_output":
            if not any(
                assertion.role == "author" and assertion.actor == principal
                for assertion in values
            ):
                values.append(author)
        elif not any(assertion.role == "author" for assertion in values):
            values.append(author)
        if not any(
            assertion.role == "issuer" and assertion.actor == self.issuer
            for assertion in values
        ):
            values.append(
                AttributionAssertion(
                    role="issuer",
                    actor=self.issuer,
                    basis="server_constructed_context",
                    assurance="trusted_component",
                    asserted_by=self.issuer,
                )
            )
        return tuple(values), producer

    def capture_for_disclosure(
        self,
        *,
        exact_content: bytes,
        source_role: str,
        run_id: str,
        tool_call_id: str,
        idempotency_key: str,
        direction: Any,
        purpose: str,
        authorization_ref: str,
        recipient: str,
        provider_id: str,
        model_id: str,
        derivation_ref: str | None = None,
        derivation_refs: Sequence[str] = (),
        input_manifest_sha256: str | None = None,
        media_type: str = "text/plain",
        encoding: str | None = "utf-8",
        source_attributions: Sequence[AttributionAssertion] = (),
        source_producer: ActorRef | None = None,
    ) -> CapturedDisclosureSource:
        """Retain a dynamic prompt/tool payload before the egress preflight."""

        if not isinstance(exact_content, bytes):
            raise InvalidSourceRequest()
        direction_value = getattr(direction, "value", direction)
        if direction_value not in {"inbound_to_model", "outbound_to_provider"}:
            raise InvalidSourceRequest()
        principal = self._run_principal(run_id)
        parsed_derivations: list[SourceRef] = []
        seen_derivations: set[str] = set()
        for candidate in (
            *((derivation_ref,) if derivation_ref is not None else ()),
            *tuple(derivation_refs),
        ):
            parsed = SourceRef.parse(candidate)
            if parsed.uri not in seen_derivations:
                seen_derivations.add(parsed.uri)
                parsed_derivations.append(parsed)
        attributions, producer = self._capture_attribution(
            source_role=source_role,
            principal=principal,
            source_attributions=source_attributions,
            source_producer=source_producer,
        )
        authorization_fingerprint = self._authorization_fingerprint(authorization_ref)
        request_hash = canonical_sha256(
            {
                "content_sha256": sha256_bytes(exact_content),
                "byte_length": len(exact_content),
                "source_role": source_role,
                "run_id_sha256": sha256_bytes(run_id.encode("utf-8")),
                "tool_call_id": tool_call_id,
                "direction": direction_value,
                "purpose": purpose,
                "authorization_fingerprint": authorization_fingerprint,
                "recipient": recipient,
                "provider_id": provider_id,
                "model_id": model_id,
                "derivation_refs": [ref.uri for ref in parsed_derivations],
                "input_manifest_sha256": input_manifest_sha256,
                "media_type": media_type,
                "encoding": encoding,
                "source_attributions": [
                    self._attribution_request_value(assertion)
                    for assertion in attributions
                ],
                "source_producer": producer.to_dict(),
            }
        )
        conn = self.store.connect()
        try:
            existing = self.store.idempotency_result(
                conn,
                tenant_scope_id=self.tenant_scope_id,
                issuer=self.issuer,
                principal=principal,
                client_mutation_id=idempotency_key,
                request_sha256=request_hash,
            )
        finally:
            conn.close()
        if existing is not None:
            return CapturedDisclosureSource(
                source_ref=SourceRef.from_dict(existing["source_ref"]).uri,
                representation_id=str(existing["representation_id"]),
                content_sha256=str(existing["content_sha256"]),
                byte_length=int(existing["byte_length"]),
            )
        with self.store.write_transaction() as conn:
            existing = self.store.idempotency_result(
                conn,
                tenant_scope_id=self.tenant_scope_id,
                issuer=self.issuer,
                principal=principal,
                client_mutation_id=idempotency_key,
                request_sha256=request_hash,
            )
            if existing is not None:
                return CapturedDisclosureSource(
                    source_ref=SourceRef.from_dict(existing["source_ref"]).uri,
                    representation_id=str(existing["representation_id"]),
                    content_sha256=str(existing["content_sha256"]),
                    byte_length=int(existing["byte_length"]),
                )
            staged = self.store._stage_if_needed(exact_content, conn=conn)
            item = self.store._capture_source(
                conn,
                content=exact_content,
                staged_blob=staged,
                source_role=source_role,
                tenant_scope_id=self.tenant_scope_id,
                originating_surface="agent_execution",
                media_type=media_type,
                representation_kind="decoded_text" if encoding else "raw_bytes",
                encoding=encoding,
                schema_type=None,
                origin_ref=None,
                native_revision=None,
                fidelity="exact_dynamic_payload",
                namespace=run_id,
                sensitivity_class="private",
                retention_class="run_source",
                occurred_at=None,
                provider_observed_at=None,
                received_at=utc_now(),
                attributions=attributions,
                producer=producer,
            )
            self.store._grant_access(
                conn,
                source_ref=item.source_ref,
                principal=principal,
                purpose=purpose,
                access_mode="content",
                authorization_fingerprint=authorization_fingerprint,
                scope={
                    "run_id_sha256": sha256_bytes(run_id.encode("utf-8")),
                    "tool_call_id": tool_call_id,
                    "direction": str(direction_value),
                },
                external_recipient=recipient,
                model_id=model_id,
                egress_class=str(direction_value),
                content_boundary={
                    "representation_id": item.primary_representation_id,
                    "max_bytes": len(exact_content),
                },
            )
            for input_ref in parsed_derivations:
                input_item = self.store._get_item(conn, input_ref)
                assert input_item is not None
                if input_item.tenant_scope_id != self.tenant_scope_id:
                    raise SourceNotFound()
                if input_item.lifecycle_state == "redacted":
                    raise SourceRedacted()
                if input_item.lifecycle_state != "active":
                    raise SourceNotFound()
                conn.execute(
                    "INSERT INTO source_derivations "
                    "(derivation_id, derived_authority_id, derived_item_id, "
                    " input_authority_id, input_item_id, relation, producer_ref_json, "
                    " activity_id, method_json, fidelity, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'quoted_from', ?, ?, ?, 'exact_embedded_copy', ?)",
                    (
                        new_id(),
                        item.source_ref.authority_id,
                        item.source_ref.item_id,
                        input_ref.authority_id,
                        input_ref.item_id,
                        canonical_json(producer.to_dict()),
                        f"agent-disclosure-{sha256_bytes(idempotency_key.encode('utf-8'))[:32]}",
                        canonical_json(
                            {
                                "input_manifest_sha256": input_manifest_sha256,
                                "direction": direction_value,
                                "boundary": "exact_dynamic_payload",
                            }
                        ),
                        utc_now(),
                    ),
                )
            result = {
                "source_ref": item.source_ref.to_dict(),
                "representation_id": item.primary_representation_id,
                "content_sha256": sha256_bytes(exact_content),
                "byte_length": len(exact_content),
            }
            self.store.record_idempotency(
                conn,
                tenant_scope_id=self.tenant_scope_id,
                issuer=self.issuer,
                principal=principal,
                client_mutation_id=idempotency_key,
                request_sha256=request_hash,
                result=result,
            )
            return CapturedDisclosureSource(
                source_ref=item.source_ref.uri,
                representation_id=item.primary_representation_id,
                content_sha256=result["content_sha256"],
                byte_length=result["byte_length"],
            )

    def grant_existing_source_for_disclosure(
        self,
        *,
        source_ref: str,
        representation_id: str,
        run_id: str,
        direction: Any,
        purpose: str,
        authorization_ref: str,
        recipient: str,
        provider_id: str,
        model_id: str,
        tool_call_id: str | None = None,
    ) -> AccessBinding:
        """Grant a run bounded access to an already-retained representation.

        This grant-only operation neither resolves content nor recaptures it.
        Agent Execution can subsequently reserve the exact SourceRef through
        the normal preflight boundary, keeping raw bytes out of its manifest.
        """

        parsed_ref = SourceRef.parse(source_ref)
        direction_value = getattr(direction, "value", direction)
        if direction_value not in {"inbound_to_model", "outbound_to_provider"}:
            raise InvalidSourceRequest()
        if any(
            not isinstance(value, str) or not value
            for value in (representation_id, recipient, provider_id, model_id)
        ):
            raise InvalidSourceRequest()
        principal = self._run_principal(run_id)
        scope = {
            "run_id_sha256": sha256_bytes(run_id.encode("utf-8")),
            "provider_id": provider_id,
            "direction": str(direction_value),
        }
        if tool_call_id is not None:
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise InvalidSourceRequest()
            scope["tool_call_id"] = tool_call_id

        with self.store.write_transaction() as conn:
            item = self.store._get_item(conn, parsed_ref, required=False)
            if item is None or item.tenant_scope_id != self.tenant_scope_id:
                raise SourceNotFound()
            if item.lifecycle_state != "active":
                if item.lifecycle_state == "redacted":
                    raise SourceRedacted()
                raise SourceNotFound()
            representation = self.store._representation_row(
                conn,
                parsed_ref,
                representation_id,
            )
            if representation["redacted_at"] is not None:
                raise SourceRedacted()
            if (
                representation["inline_content"] is None
                and representation["blob_sha256"] is None
            ):
                raise SourceIntegrityFailure()
            if representation["blob_sha256"] is not None and not self.store.blobs.path_for(
                str(representation["blob_sha256"])
            ).is_file():
                raise SourceIntegrityFailure()
            return self.store._grant_access(
                conn,
                source_ref=parsed_ref,
                principal=principal,
                purpose=purpose,
                access_mode="content",
                authorization_fingerprint=self._authorization_fingerprint(
                    authorization_ref
                ),
                scope=scope,
                external_recipient=recipient,
                model_id=model_id,
                egress_class=str(direction_value),
                content_boundary={
                    "representation_id": representation_id,
                    "max_bytes": int(representation["byte_length"]),
                },
            )

    def reserve_disclosure(
        self,
        preflight: Any,
        *,
        reservation_idempotency_key: str,
    ) -> Any:
        """Reserve and resolve the exact content described by a manifest preflight."""

        from work_buddy.agent_execution.disclosure import SourceDisclosureReservation

        source_ref = SourceRef.parse(preflight.source_ref)
        item = self.store.get_item(source_ref)
        if item is None or item.tenant_scope_id != self.tenant_scope_id:
            from work_buddy.sources.errors import SourceNotFound

            raise SourceNotFound()
        principal = self._run_principal(preflight.run_id)
        direction = getattr(preflight.direction, "value", preflight.direction)
        selector = preflight.selector.to_dict()
        resolution = resolve_and_reserve_source(
            self.store,
            source_ref=source_ref,
            representation_id=preflight.representation_id,
            principal=principal,
            purpose=preflight.purpose,
            consumer_domain="agent_execution",
            consumer_id=reservation_idempotency_key,
            use_kind=f"disclosure.{direction}",
            disclosure_kind=(
                "external_model_disclosure"
                if direction == "inbound_to_model"
                else "external_provider_disclosure"
            ),
            redaction_policy="invalidate",
            selector=selector,
            expected_digest=preflight.content_sha256,
            external_recipient=preflight.recipient,
            model_id=preflight.model_id,
            egress_class=direction,
        )
        if resolution.resolved.representation.byte_length != preflight.byte_length:
            raise SourceUsageConflict()
        return SourceDisclosureReservation(
            reservation_id=resolution.reservation.usage_id,
            redaction_epoch=resolution.reservation.redaction_epoch,
            content_sha256=resolution.resolved.representation.content_sha256,
            byte_length=resolution.resolved.representation.byte_length,
        )

    def acknowledge_disclosure(
        self,
        *,
        reservation_id: str,
        manifest_entry_id: str,
        outcome: Any,
        acknowledgement_idempotency_key: str,
    ) -> None:
        """Acknowledge sent or release proven-not-sent exactly once."""

        outcome_value = getattr(outcome, "value", outcome)
        if outcome_value not in {"sent", "not_sent"}:
            raise InvalidSourceRequest()
        request_hash = canonical_sha256(
            {
                "reservation_id": reservation_id,
                "manifest_entry_id": manifest_entry_id,
                "outcome": outcome_value,
            }
        )
        now = utc_now()
        with self.store.write_transaction() as conn:
            existing = conn.execute(
                "SELECT usage_id, request_sha256 FROM source_usage_ack_idempotency "
                "WHERE acknowledgement_key = ?",
                (acknowledgement_idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["usage_id"] != reservation_id
                    or existing["request_sha256"] != request_hash
                ):
                    raise SourceIdempotencyConflict()
                return
            usage = conn.execute(
                "SELECT u.*, i.lifecycle_state FROM source_usage_intents u "
                "JOIN source_items i ON i.authority_id = u.authority_id "
                "AND i.source_item_id = u.source_item_id WHERE u.usage_id = ?",
                (reservation_id,),
            ).fetchone()
            if usage is None:
                from work_buddy.sources.errors import SourceNotFound

                raise SourceNotFound()
            if outcome_value == "sent":
                if usage["status"] == "released":
                    raise SourceUsageConflict()
                maintenance = (
                    "pending_redaction"
                    if usage["lifecycle_state"] == "redacted"
                    else "clean"
                )
                conn.execute(
                    "UPDATE source_usage_intents SET status = 'acknowledged', "
                    "acknowledged_at = COALESCE(acknowledged_at, ?), maintenance_state = ? "
                    "WHERE usage_id = ?",
                    (now, maintenance, reservation_id),
                )
            else:
                if usage["status"] == "acknowledged":
                    raise SourceUsageConflict()
                conn.execute(
                    "UPDATE source_usage_intents SET status = 'released', "
                    "released_at = COALESCE(released_at, ?), maintenance_state = 'completed' "
                    "WHERE usage_id = ?",
                    (now, reservation_id),
                )
            conn.execute(
                "INSERT INTO source_usage_ack_idempotency "
                "(acknowledgement_key, usage_id, request_sha256, acknowledged_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    acknowledgement_idempotency_key,
                    reservation_id,
                    request_hash,
                    now,
                ),
            )

    def validate_disclosure_reservation(
        self,
        *,
        reservation_id: str,
        redaction_epoch: int,
    ) -> None:
        """Fail closed when a previously admitted disclosure was redacted.

        Agent Execution may idempotently replay a capability result that was
        already recorded as sent.  That replay still releases bytes across the
        trusted worker boundary, so it must not rely only on the historical
        manifest row.  Re-check the live Sources lifecycle and the exact epoch
        bound by the reservation before every release and output binding.

        Both ``reserved`` and ``acknowledged`` intents are valid here: the
        latter is the normal state after the first successful handoff.  A
        released intent, a redacted representation, or any epoch drift makes
        the old authorization unusable.
        """

        if (
            not isinstance(reservation_id, str)
            or not reservation_id
            or isinstance(redaction_epoch, bool)
            or not isinstance(redaction_epoch, int)
            or redaction_epoch < 0
        ):
            raise InvalidSourceRequest()
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT u.status, u.bound_redaction_epoch, "
                "i.lifecycle_state, i.redaction_epoch current_epoch, "
                "r.redacted_at, r.inline_content, r.blob_sha256 "
                "FROM source_usage_intents u "
                "JOIN source_items i ON i.authority_id = u.authority_id "
                "AND i.source_item_id = u.source_item_id "
                "JOIN source_representations r "
                "ON r.authority_id = u.authority_id "
                "AND r.source_item_id = u.source_item_id "
                "AND r.representation_id = u.representation_id "
                "WHERE u.usage_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise SourceNotFound()
            if (
                row["status"] not in {"reserved", "acknowledged"}
                or row["lifecycle_state"] != "active"
                or int(row["bound_redaction_epoch"]) != redaction_epoch
                or int(row["current_epoch"]) != redaction_epoch
                or row["redacted_at"] is not None
                or (
                    row["inline_content"] is None
                    and row["blob_sha256"] is None
                )
            ):
                raise SourceUsageConflict()
            if row["blob_sha256"] is not None and not self.store.blobs.path_for(
                str(row["blob_sha256"])
            ).is_file():
                raise SourceIntegrityFailure()
        finally:
            conn.close()
