"""Source-backed publication for generic native Journal items.

The coordinator closes the cross-database crash/race boundaries around a
retained Source, its usage reservation, and the Journal item transaction.
Callers supply explicit stores and an already trusted service principal; this
module performs no configured-store lookup and exposes no public migration
capability.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Mapping

from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalFieldValue,
    JournalNativeItem,
)
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import ActorRef, SourceRef, SourceStore


NATIVE_SOURCE_PURPOSE = "journal.native_item"
NATIVE_SOURCE_USE_KIND = "journal_native_item"
FIELD_SOURCE_PURPOSE = "journal.field_value"
FIELD_SOURCE_USE_KIND = "journal_field_value"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    if not isinstance(value, str):
        value = _canonical(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dependency_id(client_mutation_id: str) -> str:
    return "jnsd_" + _sha(
        {
            "schema": "wb.journal-native-source-dependency-id/v1",
            "clientMutationId": client_mutation_id,
        }
    )[:32]


def _consumer_id(dependency_id: str) -> str:
    return f"journal-native-item:{dependency_id}"


def _field_dependency_id(client_mutation_id: str) -> str:
    return "jfsd_" + _sha(
        {
            "schema": "wb.journal-field-source-dependency-id/v1",
            "clientMutationId": client_mutation_id,
        }
    )[:32]


def _field_consumer_id(dependency_id: str) -> str:
    return f"journal-field-value:{dependency_id}"


class JournalNativeSourceService:
    """Atomically bind a generic native item to an acknowledged Source use."""

    def __init__(
        self,
        journal_store: JournalCaptureStore,
        source_store: SourceStore,
    ) -> None:
        if journal_store.read_only:
            raise JournalCaptureConflict("The Journal Source coordinator is read-only.")
        self.journal = journal_store
        self.sources = source_store
        self.domain = JournalDomainService(journal_store)

    def create_item(
        self,
        *,
        source_ref: SourceRef,
        representation_id: str,
        service_principal: ActorRef,
        local_date: str,
        item_kind: str,
        plain_value: str,
        interaction_behavior_id: str,
        interaction_behavior_version: int,
        client_mutation_id: str,
        actor: Mapping[str, Any],
        module_instance_id: str | None = None,
        module_instance_version: int | None = None,
        privacy_class: str = "private",
        search_mode: str = "lexical_dense",
        authorship: str = "human",
        review_state: str = "not_applicable",
        purpose: str = NATIVE_SOURCE_PURPOSE,
    ) -> JournalNativeItem:
        dependency_id = _dependency_id(client_mutation_id)
        consumer_id = _consumer_id(dependency_id)
        source_uri = source_ref.uri
        content_sha = _sha(plain_value)
        request_sha = _sha(
            {
                "schema": "wb.journal-native-source-dependency/v1",
                "clientMutationId": client_mutation_id,
                "sourceRef": source_uri,
                "representationId": representation_id,
                "purpose": purpose,
                "contentSha256": content_sha,
                "localDate": local_date,
                "itemKind": item_kind,
                "behavior": [interaction_behavior_id, interaction_behavior_version],
                "module": [module_instance_id, module_instance_version],
                "privacyClass": privacy_class,
                "searchMode": search_mode,
                "authorship": authorship,
                "reviewState": review_state,
                "actor": dict(actor),
            }
        )
        now = _now()
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_native_source_dependencies "
                "WHERE dependency_id=? OR client_mutation_id=?",
                (dependency_id, client_mutation_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO journal_native_source_dependencies("
                    "dependency_id,client_mutation_id,request_sha256,"
                    "source_usage_consumer_id,source_ref,representation_id,purpose,"
                    "content_sha256,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'prepared',?,?)",
                    (
                        dependency_id,
                        client_mutation_id,
                        request_sha,
                        consumer_id,
                        source_uri,
                        representation_id,
                        purpose,
                        content_sha,
                        now,
                        now,
                    ),
                )
            else:
                if (
                    str(row["dependency_id"]) != dependency_id
                    or str(row["request_sha256"]) != request_sha
                    or str(row["source_usage_consumer_id"]) != consumer_id
                ):
                    raise JournalCaptureConflict(
                        "That native Journal mutation is already bound differently."
                    )
                state = str(row["state"])
                if state in {"redaction_committed", "released", "aborted"}:
                    raise JournalCaptureConflict(
                        "The retained Source for that Journal item was removed."
                    )
                if state == "acknowledged" and row["item_id"] is not None:
                    return self.domain.get_native_item(str(row["item_id"]))

        reservation = self.sources.reserve_usage(
            source_ref=source_ref,
            representation_id=representation_id,
            principal=service_principal,
            purpose=purpose,
            consumer_domain="journal",
            consumer_id=consumer_id,
            use_kind=NATIVE_SOURCE_USE_KIND,
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
        )
        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
        elif reservation.status != "acknowledged":
            raise JournalCaptureConflict("The native Journal Source use is unavailable.")

        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_native_source_dependencies WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or str(row["request_sha256"]) != request_sha:
                raise JournalCaptureConflict("The native Journal Source dependency changed.")
            state = str(row["state"])
            if state in {"redaction_committed", "released", "aborted"}:
                raise JournalCaptureConflict(
                    "The retained Source was removed before Journal publication."
                )
            if row["source_usage_id"] is not None and str(
                row["source_usage_id"]
            ) != reservation.usage_id:
                raise JournalCaptureConflict("The native Journal Source use changed.")
            if state == "prepared":
                conn.execute(
                    "UPDATE journal_native_source_dependencies SET source_usage_id=?,"
                    "state='reserved',updated_at=? WHERE dependency_id=? AND state='prepared'",
                    (reservation.usage_id, _now(), dependency_id),
                )

        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
        item = self.domain.create_native_item(
            local_date=local_date,
            item_kind=item_kind,
            plain_value=plain_value,
            source_ref=source_uri,
            interaction_behavior_id=interaction_behavior_id,
            interaction_behavior_version=interaction_behavior_version,
            client_mutation_id=client_mutation_id,
            actor=actor,
            module_instance_id=module_instance_id,
            module_instance_version=module_instance_version,
            privacy_class=privacy_class,
            search_mode=search_mode,
            authorship=authorship,
            review_state=review_state,
            source_dependency_id=dependency_id,
        )
        self.sources.acknowledge_usage(reservation.usage_id)
        with self.journal.transaction() as conn:
            changed = conn.execute(
                "UPDATE journal_native_source_dependencies SET state='acknowledged',"
                "updated_at=? WHERE dependency_id=? AND item_id=? AND state='bound'",
                (_now(), dependency_id, item.item_id),
            ).rowcount
            row = conn.execute(
                "SELECT state,item_id FROM journal_native_source_dependencies "
                "WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or (
                changed == 0
                and not (
                    str(row["state"]) == "acknowledged"
                    and str(row["item_id"]) == item.item_id
                )
            ):
                raise JournalCaptureConflict(
                    "The native Journal Source dependency changed during acknowledgement."
                )
        return item

    def put_field_value(
        self,
        *,
        source_ref: SourceRef,
        representation_id: str,
        service_principal: ActorRef,
        value_id: str,
        local_date: str,
        module_instance_id: str,
        module_instance_version: int,
        field_id: str,
        field_definition_version: int,
        client_mutation_id: str,
        expected_revision: int,
        actor: Mapping[str, Any],
        value: Any = None,
        disposition: str | None = None,
        composition_slot_id: str | None = None,
        prompt_id: str | None = None,
        prompt_version: int | None = None,
        authorship: str = "human",
        review_state: str = "not_applicable",
        observed_at: str | None = None,
        stated_at: str | None = None,
        purpose: str = FIELD_SOURCE_PURPOSE,
    ) -> JournalFieldValue:
        dependency_id = _field_dependency_id(client_mutation_id)
        consumer_id = _field_consumer_id(dependency_id)
        source_uri = source_ref.uri
        request_sha = _sha(
            {
                "schema": "wb.journal-field-source-dependency/v1",
                "clientMutationId": client_mutation_id,
                "sourceRef": source_uri,
                "representationId": representation_id,
                "purpose": purpose,
                "valueId": value_id,
                "localDate": local_date,
                "module": [module_instance_id, module_instance_version],
                "field": [field_id, field_definition_version],
                "prompt": [prompt_id, prompt_version],
                "compositionSlotId": composition_slot_id,
                "expectedRevision": expected_revision,
                "value": value,
                "disposition": disposition,
                "authorship": authorship,
                "reviewState": review_state,
                "observedAt": observed_at,
                "statedAt": stated_at,
                "actor": dict(actor),
            }
        )
        now = _now()
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_field_source_dependencies "
                "WHERE dependency_id=? OR client_mutation_id=?",
                (dependency_id, client_mutation_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO journal_field_source_dependencies("
                    "dependency_id,client_mutation_id,request_sha256,"
                    "source_usage_consumer_id,source_ref,representation_id,purpose,"
                    "value_id,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'prepared',?,?)",
                    (
                        dependency_id,
                        client_mutation_id,
                        request_sha,
                        consumer_id,
                        source_uri,
                        representation_id,
                        purpose,
                        value_id,
                        now,
                        now,
                    ),
                )
            else:
                if (
                    str(row["dependency_id"]) != dependency_id
                    or str(row["request_sha256"]) != request_sha
                    or str(row["source_usage_consumer_id"]) != consumer_id
                ):
                    raise JournalCaptureConflict(
                        "That Journal field mutation is already bound differently."
                    )
                state = str(row["state"])
                if state in {"redaction_committed", "released", "aborted"}:
                    raise JournalCaptureConflict(
                        "The retained Source for that Journal field was removed."
                    )
                if state == "acknowledged" and row["value_revision"] is not None:
                    return self.domain.get_field_value(value_id)

        reservation = self.sources.reserve_usage(
            source_ref=source_ref,
            representation_id=representation_id,
            principal=service_principal,
            purpose=purpose,
            consumer_domain="journal",
            consumer_id=consumer_id,
            use_kind=FIELD_SOURCE_USE_KIND,
            disclosure_kind="exact_readable_copy",
            redaction_policy="scrub",
            selector={"kind": "whole"},
        )
        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
        elif reservation.status != "acknowledged":
            raise JournalCaptureConflict("The Journal field Source use is unavailable.")

        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM journal_field_source_dependencies WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or str(row["request_sha256"]) != request_sha:
                raise JournalCaptureConflict("The Journal field Source dependency changed.")
            state = str(row["state"])
            if state in {"redaction_committed", "released", "aborted"}:
                raise JournalCaptureConflict(
                    "The retained Source was removed before Journal field publication."
                )
            if row["source_usage_id"] is not None and str(
                row["source_usage_id"]
            ) != reservation.usage_id:
                raise JournalCaptureConflict("The Journal field Source use changed.")
            if state == "prepared":
                conn.execute(
                    "UPDATE journal_field_source_dependencies SET source_usage_id=?,"
                    "state='reserved',updated_at=? WHERE dependency_id=? AND state='prepared'",
                    (reservation.usage_id, _now(), dependency_id),
                )

        if reservation.status == "reserved":
            self.sources.precommit_recheck_usage(reservation.usage_id)
        field_value = self.domain.put_field_value(
            value_id=value_id,
            local_date=local_date,
            module_instance_id=module_instance_id,
            module_instance_version=module_instance_version,
            field_id=field_id,
            field_definition_version=field_definition_version,
            client_mutation_id=client_mutation_id,
            expected_revision=expected_revision,
            actor=actor,
            value=value,
            disposition=disposition,
            composition_slot_id=composition_slot_id,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            source_ref=source_uri,
            authorship=authorship,
            review_state=review_state,
            observed_at=observed_at,
            stated_at=stated_at,
            source_dependency_id=dependency_id,
        )
        self.sources.acknowledge_usage(reservation.usage_id)
        with self.journal.transaction() as conn:
            changed = conn.execute(
                "UPDATE journal_field_source_dependencies SET state='acknowledged',"
                "updated_at=? WHERE dependency_id=? AND value_id=? "
                "AND value_revision=? AND state='bound'",
                (
                    _now(),
                    dependency_id,
                    value_id,
                    field_value.current_revision,
                ),
            ).rowcount
            row = conn.execute(
                "SELECT state,value_id,value_revision "
                "FROM journal_field_source_dependencies WHERE dependency_id=?",
                (dependency_id,),
            ).fetchone()
            if row is None or (
                changed == 0
                and not (
                    str(row["state"]) == "acknowledged"
                    and str(row["value_id"]) == value_id
                    and int(row["value_revision"]) == field_value.current_revision
                )
            ):
                raise JournalCaptureConflict(
                    "The Journal field Source dependency changed during acknowledgement."
                )
        return field_value


__all__ = [
    "FIELD_SOURCE_PURPOSE",
    "FIELD_SOURCE_USE_KIND",
    "JournalNativeSourceService",
    "NATIVE_SOURCE_PURPOSE",
    "NATIVE_SOURCE_USE_KIND",
]
