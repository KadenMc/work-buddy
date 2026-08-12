"""Content-free operator facade for Journal content migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_buddy.journal_capture.migration import JournalMigrationService


class JournalMigrationOperator:
    def __init__(self, service: JournalMigrationService) -> None:
        self.service = service

    def inventory(self) -> dict[str, Any]:
        return self.service.inventory()

    def select(
        self,
        *,
        entity_kind: str,
        day_id: str,
        entity_id: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        if entity_kind == "logical_day_log":
            record = self.service.select_log(day_id)
        elif entity_kind == "running_note" and entity_id is not None:
            record = self.service.select_managed_running_note(day_id, entity_id)
        elif (
            entity_kind == "running_note"
            and start_line is not None
            and end_line is not None
        ):
            record = self.service.assign_running_note(
                day_id, start_line=start_line, end_line=end_line
            )
        else:
            raise ValueError(
                "running_note selection requires a managed entity_id or start_line/end_line"
            )
        return self._record("wb.journal-content-selection/v1", record)

    def shadow_import(self, entity_kind: str, entity_id: str) -> dict[str, Any]:
        return self._record(
            "wb.journal-content-shadow/v1",
            self.service.shadow_import(entity_kind, entity_id),
        )

    def cutover(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        rollback_deadline: str,
    ) -> dict[str, Any]:
        return self._record(
            "wb.journal-content-cutover/v1",
            self.service.cutover(
                entity_kind, entity_id, rollback_deadline=rollback_deadline
            ),
        )

    def rollback(self, entity_kind: str, entity_id: str) -> dict[str, Any]:
        return self._record(
            "wb.journal-content-rollback/v1",
            self.service.rollback(entity_kind, entity_id),
        )

    def reconcile(
        self, entity_kind: str | None = None, entity_id: str | None = None
    ) -> dict[str, Any]:
        records = (
            [self.service.journal.get_migration(entity_kind, entity_id)]
            if entity_kind is not None and entity_id is not None
            else list(self.service.journal.list_migrations())
        )
        settled = []
        for record in records:
            if record is None:
                continue
            settled.append(
                self._record(
                    "wb.journal-content-reconcile-item/v1",
                    self.service.reconcile(record.entity_kind, record.entity_id),
                )
            )
        return {"schema": "wb.journal-content-reconcile/v1", "entities": settled}

    def certify_exit(self) -> dict[str, Any]:
        return self.service.certify_exit()

    @staticmethod
    def _record(schema: str, record) -> dict[str, Any]:
        return {
            "schema": schema,
            "entityKind": record.entity_kind,
            "entityId": record.entity_id,
            "dayId": record.day_id,
            "state": record.mirrored_state.value,
            "authorityEpoch": record.mirrored_authority_epoch,
            "comparison": record.comparison_state.value,
            "projection": record.projection_state,
            "bound": bool(record.binding_id and record.document_id),
            "rollbackDeadline": record.rollback_deadline,
            "errorCode": record.error_code,
        }


__all__ = ["JournalMigrationOperator"]
