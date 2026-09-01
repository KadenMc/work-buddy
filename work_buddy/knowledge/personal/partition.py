"""Personal Knowledge SQLite authority adapter for consolidated search."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from work_buddy.index.model import (
    Document,
    ItemRef,
    PoolStrategy,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    make_doc_id,
)
from work_buddy.index.partition import register_partition
from work_buddy.knowledge.personal.store import PersonalKnowledgeStore, utcnow


_PARTITION = "personal_knowledge"


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class PersonalKnowledgePartition:
    """Index stable unit IDs while retaining mutable paths as metadata/aliases."""

    name = _PARTITION
    change_key = "hash"

    def __init__(self, store: PersonalKnowledgeStore | None = None) -> None:
        self._store = store or PersonalKnowledgeStore()

    def field_weights(self) -> dict[str, float]:
        return {"name": 3.0, "aliases": 2.0, "tags": 1.5, "body": 1.0}

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        return {
            "content": ProjectionSpec(kind=ProjectionKind.PASSAGE),
            "aliases": ProjectionSpec(kind=ProjectionKind.LABEL, pool=PoolStrategy.MAX),
        }

    def _native(self) -> bool:
        return (
            PersonalKnowledgeStore.existing_authority(self._store.db_path)
            == "sqlite"
        )

    def discover(self) -> Iterable[ItemRef]:
        if not self._native():
            return []
        connection = self._store.connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT u.unit_id,o.content_sha256
                FROM personal_units AS u
                JOIN personal_search_outbox AS o
                  ON o.unit_id=u.unit_id AND o.revision=u.current_revision
                WHERE u.lifecycle IN ('active','archived')
                ORDER BY u.unit_id
                """
            ).fetchall()
            return [
                ItemRef(
                    item_id=str(row["unit_id"]),
                    content_hash=str(row["content_sha256"]),
                )
                for row in rows
            ]
        finally:
            connection.close()

    def _record(self, item_id: str) -> dict[str, Any] | None:
        if not self._native():
            return None
        connection = self._store.connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT lifecycle FROM personal_units WHERE unit_id=?",
                (item_id,),
            ).fetchone()
            if row is None or str(row["lifecycle"]) == "tombstoned":
                return None
            return self._store._record(connection, item_id)
        finally:
            connection.close()

    def parse(self, item_id: str) -> list[Document]:
        record = self._record(item_id)
        if record is None:
            return []
        aliases = [str(value) for value in record["aliases"] if value]
        tags = [str(value) for value in record["tags"] if value]
        categories = [str(value) for value in record["categories"] if value]
        body_parts = [
            str(record.get("description") or ""),
            str(record.get("summary") or ""),
        ]
        if record.get("body_mode") == "plain" and record.get("body"):
            body_parts.append(str(record["body"]))
        searchable = "\n".join(
            value for value in [record["name"], *aliases, *tags, *body_parts] if value
        )
        projections: dict[str, Projection] = {
            "content": Projection(text=searchable)
        }
        if aliases:
            projections["aliases"] = Projection(text=aliases)
        return [
            Document(
                doc_id=make_doc_id(_PARTITION, item_id),
                partition=_PARTITION,
                fields={
                    "name": str(record["name"]),
                    "aliases": " ".join(aliases),
                    "tags": " ".join(tags),
                    "body": "\n".join(value for value in body_parts if value),
                },
                display_text=(
                    f"[{categories[0] if categories else 'personal'}] "
                    f"{record['current_path']}: {record['description']}"
                ),
                metadata={
                    "unitId": item_id,
                    "path": str(record["current_path"]),
                    "scope": "personal",
                    "kind": "personal",
                    "revision": int(record["current_revision"]),
                    "lifecycle": str(record["lifecycle"]),
                    "category": categories[0] if categories else "",
                    "categories": categories,
                    "severity": str(record.get("severity") or ""),
                    "privacyClass": str(record["privacy_class"]),
                    "disclosureClass": str(record["disclosure_class"]),
                    "bodyMode": str(record["body_mode"]),
                },
                projections=projections,
                content_hash=self._store._content_hash(record),
                timestamp=_timestamp(record.get("updated_at")),
            )
        ]

    def hydrate(self, hits: list, **_opts: Any) -> list[dict[str, Any]]:
        current = {ref.item_id: ref.content_hash for ref in self.discover()}
        hydrated: list[dict[str, Any]] = []
        for hit in hits:
            item_id = hit.doc_id.split(":", 1)[1] if ":" in hit.doc_id else hit.doc_id
            if item_id not in current:
                continue
            docs = self.parse(item_id)
            if not docs:
                continue
            doc = docs[0]
            if hit.metadata and hit.metadata.get("revision") != doc.metadata["revision"]:
                continue
            hydrated.append(
                {
                    "score": hit.score,
                    "docId": doc.doc_id,
                    "displayText": doc.display_text,
                    **doc.metadata,
                }
            )
        return hydrated

    def pending_search_events(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        if not self._native():
            return []
        return self._store.pending_outbox(limit=limit)

    def pending_search_event_count(self) -> int:
        if not self._native():
            return 0
        connection = self._store.connect(read_only=True)
        try:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM personal_search_outbox "
                    "WHERE delivered_at IS NULL"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def acknowledge_search_events(self, events: Sequence[Mapping[str, Any]]) -> None:
        ids = tuple(dict.fromkeys(str(event["event_id"]) for event in events))
        if not ids:
            return
        connection = self._store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"UPDATE personal_search_outbox SET delivered_at=? "
                f"WHERE delivered_at IS NULL AND event_id IN ({placeholders})",
                (utcnow(), *ids),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


register_partition(_PARTITION, PersonalKnowledgePartition)


__all__ = ["PersonalKnowledgePartition"]
