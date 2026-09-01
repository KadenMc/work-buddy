"""Contracts SQLite authority adapter for consolidated search."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence

from work_buddy.contracts_domain.store import ContractStore
from work_buddy.index.model import (
    Document,
    ItemRef,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    make_doc_id,
)
from work_buddy.index.partition import register_partition


_PARTITION = "contracts"


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class ContractsPartition:
    """Project current Contract revisions after the native cohort is sealed.

    Discovery reads the immutable search payload committed beside each domain
    revision.  It neither opens Markdown nor creates/migrates a missing store.
    """

    name = _PARTITION
    change_key = "hash"

    def __init__(self, store: ContractStore | None = None) -> None:
        self._store = store or ContractStore.default()

    def field_weights(self) -> dict[str, float]:
        return {
            "title": 3.0,
            "aliases": 2.0,
            "body": 1.5,
            "status": 0.5,
            "type": 0.5,
        }

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        return {"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def _native(self) -> bool:
        if not self._store.exists():
            return False
        connection = self._store.connect(migrate=False)
        try:
            row = connection.execute(
                "SELECT state FROM contract_authority WHERE singleton=1"
            ).fetchone()
            return row is not None and str(row["state"]) == "native"
        finally:
            connection.close()

    def discover(self) -> Iterable[ItemRef]:
        if not self._native():
            return []
        connection = self._store.connect(migrate=False)
        try:
            rows = connection.execute(
                """
                SELECT c.contract_id,o.content_sha256
                FROM contracts AS c
                JOIN contract_search_outbox AS o
                  ON o.contract_id=c.contract_id
                 AND o.revision=c.current_revision
                WHERE c.lifecycle!='tombstoned' AND o.event_kind='upsert'
                ORDER BY c.contract_id
                """
            ).fetchall()
            return [
                ItemRef(
                    item_id=str(row["contract_id"]),
                    content_hash=str(row["content_sha256"]),
                )
                for row in rows
            ]
        finally:
            connection.close()

    def parse(self, item_id: str) -> list[Document]:
        if not self._native():
            return []
        connection = self._store.connect(migrate=False)
        try:
            row = connection.execute(
                """
                SELECT c.contract_id,c.current_revision,c.lifecycle,c.updated_at,
                       o.content_sha256,o.payload_json
                FROM contracts AS c
                JOIN contract_search_outbox AS o
                  ON o.contract_id=c.contract_id
                 AND o.revision=c.current_revision
                WHERE c.contract_id=? AND c.lifecycle!='tombstoned'
                  AND o.event_kind='upsert'
                """,
                (item_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return []
        payload = json.loads(str(row["payload_json"]))
        aliases = [str(value) for value in payload.get("aliases", []) if value]
        bodies = [str(value) for value in payload.get("plain_bodies", []) if value]
        title = str(payload.get("title") or item_id)
        searchable = "\n".join([title, *aliases, *bodies])
        return [
            Document(
                doc_id=make_doc_id(_PARTITION, item_id),
                partition=_PARTITION,
                fields={
                    "title": title,
                    "aliases": " ".join(aliases),
                    "body": "\n".join(bodies),
                    "status": str(payload.get("status") or ""),
                    "type": str(payload.get("type") or ""),
                },
                display_text=f"{title} · {payload.get('status') or ''}",
                metadata={
                    "contractId": item_id,
                    "revision": int(row["current_revision"]),
                    "lifecycle": str(row["lifecycle"]),
                    "privacyClass": str(payload.get("privacy_class") or "private"),
                },
                projections={"content": Projection(text=searchable)},
                content_hash=str(row["content_sha256"]),
                timestamp=_timestamp(row["updated_at"]),
            )
        ]

    def hydrate(self, hits: list, **_opts: Any) -> list[dict[str, Any]]:
        """Drop stale revisions before returning a Contract hit."""

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
        connection = self._store.connect(migrate=False)
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM contract_search_outbox WHERE delivered_at IS NULL "
                    "ORDER BY committed_at,event_id LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            ]
        finally:
            connection.close()

    def pending_search_event_count(self) -> int:
        if not self._native():
            return 0
        connection = self._store.connect(migrate=False)
        try:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM contract_search_outbox "
                    "WHERE delivered_at IS NULL"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def acknowledge_search_events(self, events: Sequence[Mapping[str, Any]]) -> None:
        if not events:
            return
        with self._store.write_transaction() as connection:
            for event in events:
                row = connection.execute(
                    "SELECT content_sha256,delivered_at FROM contract_search_outbox "
                    "WHERE event_id=?",
                    (str(event["event_id"]),),
                ).fetchone()
                if row is None:
                    raise RuntimeError("contract search event disappeared during delivery")
                if str(row["content_sha256"]) != str(event["content_sha256"]):
                    raise RuntimeError("contract search event digest changed during delivery")
                if row["delivered_at"] is None:
                    connection.execute(
                        "UPDATE contract_search_outbox SET delivered_at=? WHERE event_id=?",
                        (datetime.now(UTC).isoformat(), str(event["event_id"])),
                    )


register_partition(_PARTITION, ContractsPartition)


__all__ = ["ContractsPartition"]
