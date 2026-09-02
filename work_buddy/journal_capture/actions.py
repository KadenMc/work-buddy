"""Source-bound public Journal item and prompt/result actions.

The HTTP layer commits human text (or receives an identified agent-output
Source) before calling this coordinator.  It closes the cross-database gap
between a Sources usage reservation and the corresponding Journal revision.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Mapping

from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalNativeItem,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import ActorRef, SourceRef, SourceStore


ITEM_ACTION_PURPOSE = "journal.item_revision"
PROMPT_INPUT_PURPOSE = "journal.prompt_input"
PROMPT_RESULT_PURPOSE = "journal.prompt_result"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stable(prefix: str, mutation_id: str) -> str:
    return prefix + _sha({"schema": prefix, "clientMutationId": mutation_id})[:32]


class JournalActionSourceService:
    def __init__(self, journal: JournalCaptureStore, sources: SourceStore) -> None:
        if journal.read_only:
            raise JournalCaptureConflict("The Journal action coordinator is read-only.")
        self.journal = journal
        self.sources = sources
        self.domain = JournalDomainService(journal)

    def update_item(
        self,
        *,
        source_ref: SourceRef,
        representation_id: str,
        service_principal: ActorRef,
        item_id: str,
        expected_revision: int,
        operation: str,
        plain_value: str,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        authorship: str = "human",
        review_state: str = "not_applicable",
    ) -> JournalNativeItem:
        dependency_id = _stable("jirsd_", client_mutation_id)
        consumer_id = f"journal-item-revision:{dependency_id}"
        source_uri = source_ref.uri
        content_sha = _sha(plain_value)
        request_sha = _sha(
            {
                "schema": "wb.journal-item-revision-source/v1",
                "clientMutationId": client_mutation_id,
                "sourceRef": source_uri,
                "representationId": representation_id,
                "itemId": item_id,
                "expectedRevision": expected_revision,
                "operation": operation,
                "contentSha256": content_sha,
                "actor": dict(actor),
                "authorship": authorship,
                "reviewState": review_state,
            }
        )
        now = _now()
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_item_revision_source_dependencies "
                "WHERE dependency_id=? OR client_mutation_id=?",
                (dependency_id, client_mutation_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO journal_item_revision_source_dependencies("
                    "dependency_id,client_mutation_id,request_sha256,"
                    "source_usage_consumer_id,source_ref,representation_id,purpose,"
                    "item_id,expected_revision,operation_kind,content_sha256,state,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?, ?,?,?,?,'prepared',?,?)",
                    (
                        dependency_id,
                        client_mutation_id,
                        request_sha,
                        consumer_id,
                        source_uri,
                        representation_id,
                        ITEM_ACTION_PURPOSE,
                        item_id,
                        expected_revision,
                        operation,
                        content_sha,
                        now,
                        now,
                    ),
                )
            elif (
                str(row["request_sha256"]) != request_sha
                or str(row["source_usage_consumer_id"]) != consumer_id
            ):
                raise JournalCaptureConflict(
                    "That Journal action mutation is already bound differently."
                )
            elif str(row["state"]) in {
                "redaction_committed",
                "released",
                "aborted",
            }:
                raise JournalCaptureConflict(
                    "The retained Source for that Journal action was removed."
                )
            elif str(row["state"]) == "acknowledged":
                return self.domain.get_native_item(item_id)

        reservation = self.sources.reserve_usage(
            source_ref=source_ref,
            representation_id=representation_id,
            principal=service_principal,
            purpose=ITEM_ACTION_PURPOSE,
            consumer_domain="journal",
            consumer_id=consumer_id,
            use_kind="journal_item_revision",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
        )
        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
        elif reservation.status != "acknowledged":
            raise JournalCaptureConflict("The Journal action Source is unavailable.")
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_item_revision_source_dependencies "
                "WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or str(row["request_sha256"]) != request_sha:
                raise JournalCaptureConflict("The Journal action Source dependency changed.")
            if str(row["state"]) == "prepared":
                conn.execute(
                    "UPDATE journal_item_revision_source_dependencies SET "
                    "source_usage_id=?,state='reserved',updated_at=? "
                    "WHERE dependency_id=? AND state='prepared'",
                    (reservation.usage_id, _now(), dependency_id),
                )
            elif row["source_usage_id"] != reservation.usage_id:
                raise JournalCaptureConflict("The Journal action Source use changed.")
        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
        item = self.domain.update_native_item(
            item_id=item_id,
            expected_revision=expected_revision,
            plain_value=plain_value,
            client_mutation_id=client_mutation_id,
            actor=actor,
            source_ref=source_uri,
            authorship=authorship,
            review_state=review_state,
            operation=operation,
            source_dependency_id=dependency_id,
        )
        self.sources.acknowledge_usage(reservation.usage_id)
        with self.journal.transaction() as conn:
            conn.execute(
                "UPDATE journal_item_revision_source_dependencies SET "
                "state='acknowledged',updated_at=? WHERE dependency_id=? "
                "AND state='bound' AND item_revision=?",
                (_now(), dependency_id, item.current_revision),
            )
        return item

    def create_prompt_interaction(
        self,
        *,
        source_ref: SourceRef,
        representation_id: str,
        service_principal: ActorRef,
        interaction_id: str,
        local_date: str,
        module_instance_id: str,
        module_instance_version: int,
        prompt_id: str,
        prompt_version: int,
        input_text: str,
        result_retention: str,
        result_search_mode: str,
        client_mutation_id: str,
        day_id: str | None = None,
        composition_snapshot_id: str | None = None,
    ) -> Mapping[str, Any]:
        dependency_id = _stable("jpisd_", client_mutation_id)
        consumer_id = f"journal-prompt-input:{dependency_id}"
        request_sha = _sha(
            {
                "schema": "wb.journal-prompt-input-source/v1",
                "interactionId": interaction_id,
                "sourceRef": source_ref.uri,
                "representationId": representation_id,
                "inputSha256": _sha(input_text),
                "localDate": local_date,
                "module": [module_instance_id, module_instance_version],
                "prompt": [prompt_id, prompt_version],
                "resultRetention": result_retention,
                "resultSearchMode": result_search_mode,
                "dayId": day_id,
                "compositionSnapshotId": composition_snapshot_id,
            }
        )
        existing = None
        with self.journal._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM journal_prompt_input_source_dependencies "
                "WHERE dependency_id=? OR client_mutation_id=?",
                (dependency_id, client_mutation_id),
            ).fetchone()
        if existing is not None:
            if str(existing["request_sha256"]) != request_sha:
                raise JournalCaptureConflict(
                    "That prompt input mutation is already bound differently."
                )
            if str(existing["state"]) == "acknowledged":
                return self.domain.get_prompt_interaction(interaction_id)
        reservation = self.sources.reserve_usage(
            source_ref=source_ref,
            representation_id=representation_id,
            principal=service_principal,
            purpose=PROMPT_INPUT_PURPOSE,
            consumer_domain="journal",
            consumer_id=consumer_id,
            use_kind="journal_prompt_input",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
        )
        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
        elif reservation.status != "acknowledged":
            raise JournalCaptureConflict("The prompt input Source is unavailable.")
        self.domain.create_prompt_interaction(
            interaction_id=interaction_id,
            local_date=local_date,
            module_instance_id=module_instance_id,
            module_instance_version=module_instance_version,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            input_text=input_text,
            source_ref=source_ref.uri,
            result_retention=result_retention,
            result_search_mode=result_search_mode,
            day_id=day_id,
            composition_snapshot_id=composition_snapshot_id,
            client_mutation_id=client_mutation_id,
            source_dependency={
                "dependency_id": dependency_id,
                "client_mutation_id": client_mutation_id,
                "request_sha256": request_sha,
                "source_usage_consumer_id": consumer_id,
                "representation_id": representation_id,
                "source_usage_id": reservation.usage_id,
                "purpose": PROMPT_INPUT_PURPOSE,
            } if existing is None else None,
        )
        self.sources.acknowledge_usage(reservation.usage_id)
        with self.journal.transaction() as conn:
            conn.execute(
                "UPDATE journal_prompt_input_source_dependencies SET "
                "state='acknowledged',updated_at=? WHERE dependency_id=? "
                "AND state='bound'",
                (_now(), dependency_id),
            )
        return self.domain.get_prompt_interaction(interaction_id)

    def record_prompt_result(
        self,
        *,
        source_ref: SourceRef,
        representation_id: str,
        service_principal: ActorRef,
        interaction_id: str,
        expected_revision: int,
        client_mutation_id: str,
        producer_id: str,
        context_manifest_sha256: str,
        generation_receipt: Mapping[str, Any],
        result_text: str,
        generation_request_id: str,
        lease_token: str,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> str:
        dependency_id = _stable("jprsd_", client_mutation_id)
        consumer_id = f"journal-prompt-result:{dependency_id}"
        request_sha = _sha(
            {
                "schema": "wb.journal-prompt-result-source/v1",
                "interactionId": interaction_id,
                "generationRequestId": generation_request_id,
                "sourceRef": source_ref.uri,
                "representationId": representation_id,
                "resultSha256": _sha(result_text),
                "producerId": producer_id,
                "providerId": provider_id,
                "modelId": model_id,
            }
        )
        now = _now()
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_prompt_result_source_dependencies "
                "WHERE dependency_id=? OR client_mutation_id=?",
                (dependency_id, client_mutation_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO journal_prompt_result_source_dependencies("
                    "dependency_id,client_mutation_id,request_sha256,"
                    "source_usage_consumer_id,source_ref,representation_id,purpose,"
                    "generation_request_id,interaction_id,result_sha256,state,"
                    "created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                    (
                        dependency_id,
                        client_mutation_id,
                        request_sha,
                        consumer_id,
                        source_ref.uri,
                        representation_id,
                        PROMPT_RESULT_PURPOSE,
                        generation_request_id,
                        interaction_id,
                        _sha(result_text),
                        now,
                        now,
                    ),
                )
            elif str(row["request_sha256"]) != request_sha:
                raise JournalCaptureConflict(
                    "That prompt result mutation is already bound differently."
                )
            elif str(row["state"]) == "acknowledged":
                return str(row["variant_id"])
        reservation = self.sources.reserve_usage(
            source_ref=source_ref,
            representation_id=representation_id,
            principal=service_principal,
            purpose=PROMPT_RESULT_PURPOSE,
            consumer_domain="journal",
            consumer_id=consumer_id,
            use_kind="journal_prompt_result",
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
        )
        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
        elif reservation.status != "acknowledged":
            raise JournalCaptureConflict("The prompt result Source is unavailable.")
        with self.journal.transaction() as conn:
            conn.execute(
                "UPDATE journal_prompt_result_source_dependencies SET "
                "source_usage_id=?,state='reserved',updated_at=? "
                "WHERE dependency_id=? AND state='prepared'",
                (reservation.usage_id, _now(), dependency_id),
            )
        variant_id = self.domain.record_prompt_result(
            interaction_id=interaction_id,
            expected_revision=expected_revision,
            client_mutation_id=client_mutation_id,
            producer_id=producer_id,
            context_manifest_sha256=context_manifest_sha256,
            generation_receipt=generation_receipt,
            result_text=result_text,
            provider_id=provider_id,
            model_id=model_id,
            generation_request_id=generation_request_id,
            lease_token=lease_token,
            source_ref=source_ref.uri,
            source_dependency_id=dependency_id,
        )
        self.sources.acknowledge_usage(reservation.usage_id)
        with self.journal.transaction() as conn:
            conn.execute(
                "UPDATE journal_prompt_result_source_dependencies SET "
                "state='acknowledged',updated_at=? WHERE dependency_id=? "
                "AND state='bound' AND variant_id=?",
                (_now(), dependency_id, variant_id),
            )
        return variant_id


__all__ = [
    "ITEM_ACTION_PURPOSE",
    "JournalActionSourceService",
    "PROMPT_INPUT_PURPOSE",
    "PROMPT_RESULT_PURPOSE",
]
