"""Journal domain adapter for the consolidated search index.

Authority is hydrated from Journal SQLite.  Legacy entry bridges read prose from
``journal_entries``; native rows read their own plain value.  Search policy and
the day-pinned composition digest participate in change detection, so changing
tomorrow's selected profile cannot churn historical embeddings.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Iterable, Sequence

from work_buddy.index.model import (
    Document,
    ItemRef,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    make_doc_id,
)
from work_buddy.index.partition import register_partition
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.store import JournalCaptureStore


_PARTITION = "journal"


def _hash(*parts: Any) -> str:
    return hashlib.sha256(
        "\x00".join("" if part is None else str(part) for part in parts).encode("utf-8")
    ).hexdigest()


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class JournalPartition:
    name = _PARTITION
    change_key = "hash"

    def __init__(self, store: JournalCaptureStore | None = None) -> None:
        self._store = store or JournalCaptureStore()
        self._domain = JournalDomainService(self._store)

    def field_weights(self) -> dict[str, float]:
        return {"title": 2.5, "tags": 1.5, "body": 1.0}

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        return {"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def pending_search_events(self, *, limit: int = 1000):
        """Expose Journal's authority-gated transactional outbox to the index."""

        return self._domain.pending_search_events(limit=limit)

    def pending_search_event_count(self) -> int:
        """Count only events whose cohort is currently publishable."""

        with self._store._connect() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM journal_search_outbox AS event
                    WHERE event.state IN ('pending','failed')
                      AND (
                        event.visibility_cohort_id IS NULL
                        OR (
                          EXISTS(
                            SELECT 1 FROM journal_import_cohorts AS cohort
                            WHERE cohort.cohort_id=event.visibility_cohort_id
                              AND cohort.state='sealed'
                          )
                          AND EXISTS(
                            SELECT 1 FROM journal_authority_control AS authority
                            WHERE authority.singleton=1
                              AND authority.mode='database_only'
                          )
                        )
                      )
                    """
                ).fetchone()[0]
            )

    def acknowledge_search_events(self, events: Sequence[Any]) -> None:
        self._domain.mark_search_events_delivered(
            getattr(event, "event_id", "") for event in events
        )

    def discover(self) -> Iterable[ItemRef]:
        refs: list[ItemRef] = []
        with self._store._connect() as conn:
            relation_tokens: dict[str, list[str]] = {}
            for relation in conn.execute(
                "SELECT source_item_id,relation_id,relation_kind,target_domain,"
                "target_id,target_revision,revision FROM journal_relations "
                "WHERE lifecycle='current' ORDER BY source_item_id,relation_id"
            ).fetchall():
                relation_tokens.setdefault(str(relation["source_item_id"]), []).append(
                    _hash(
                        relation["relation_id"],
                        relation["relation_kind"],
                        relation["target_domain"],
                        relation["target_id"],
                        relation["target_revision"],
                        relation["revision"],
                    )
                )
            item_rows = conn.execute(
                """
                SELECT i.item_id,i.authority_kind,i.current_content_sha256,
                       i.current_revision,i.search_mode,i.lifecycle,
                       e.content_sha256 AS legacy_content_sha256,
                       e.version AS legacy_version,
                       s.composition_digest,s.search_recipe_version
                FROM journal_items AS i
                LEFT JOIN journal_entries AS e ON e.entry_id=i.legacy_entry_id
                LEFT JOIN journal_days AS d ON d.local_date=i.local_date
                LEFT JOIN journal_day_composition_snapshots AS s ON s.day_id=d.day_id
                WHERE i.lifecycle NOT IN ('tombstoned','superseded')
                  AND i.search_mode NOT IN ('structured_only','excluded')
                  AND (
                    i.import_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=i.import_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                """
            ).fetchall()
            for row in item_rows:
                content = (
                    row["legacy_content_sha256"]
                    if row["authority_kind"] == "legacy_entry"
                    else row["current_content_sha256"]
                )
                revision = (
                    row["legacy_version"]
                    if row["authority_kind"] == "legacy_entry"
                    else row["current_revision"]
                )
                if content is None:
                    continue
                refs.append(
                    ItemRef(
                        item_id=f"item:{row['item_id']}",
                        content_hash=_hash(
                            content,
                            revision,
                            row["search_mode"],
                            row["composition_digest"],
                            row["search_recipe_version"] or 1,
                            *relation_tokens.get(str(row["item_id"]), ()),
                        ),
                    )
                )
            value_rows = conn.execute(
                """
                SELECT v.value_id,v.current_revision,v.lifecycle,
                       f.search_mode,f.definition_sha256,
                       r.value_sha256,s.composition_digest,s.search_recipe_version
                FROM journal_field_values AS v
                JOIN journal_field_definition_versions AS f
                  ON f.field_id=v.field_id
                 AND f.definition_version=v.field_definition_version
                JOIN journal_field_value_revisions AS r
                  ON r.value_id=v.value_id AND r.revision=v.current_revision
                LEFT JOIN journal_days AS d ON d.local_date=v.local_date
                LEFT JOIN journal_day_composition_snapshots AS s ON s.day_id=d.day_id
                WHERE v.lifecycle != 'tombstoned'
                  AND f.search_mode NOT IN ('structured_only','excluded')
                  AND (
                    v.import_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=v.import_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                """
            ).fetchall()
            for row in value_rows:
                refs.append(
                    ItemRef(
                        item_id=f"field:{row['value_id']}",
                        content_hash=_hash(
                            row["value_sha256"],
                            row["current_revision"],
                            row["definition_sha256"],
                            row["search_mode"],
                            row["composition_digest"],
                            row["search_recipe_version"] or 1,
                        ),
                    )
                )
            result_rows = conn.execute(
                """
                SELECT v.variant_id,v.current_revision,v.result_content_sha256,
                       v.lifecycle,i.local_date,i.result_search_mode,
                       s.composition_digest,s.search_recipe_version
                FROM journal_prompt_result_variants AS v
                JOIN journal_prompt_interactions AS i ON i.interaction_id=v.interaction_id
                LEFT JOIN journal_days AS d ON d.local_date=i.local_date
                LEFT JOIN journal_day_composition_snapshots AS s ON s.day_id=d.day_id
                WHERE v.lifecycle != 'archived' AND i.result_search_mode='content'
                """
            ).fetchall()
            for row in result_rows:
                refs.append(
                    ItemRef(
                        item_id=f"prompt_result:{row['variant_id']}",
                        content_hash=_hash(
                            row["result_content_sha256"],
                            row["current_revision"],
                            row["composition_digest"],
                            row["search_recipe_version"] or 1,
                        ),
                    )
                )
        return refs

    def parse(self, item_id: str) -> list[Document]:
        kind, separator, stable_id = item_id.partition(":")
        if not separator or not stable_id:
            return []
        if kind == "item":
            return self._parse_item(stable_id)
        if kind == "field":
            return self._parse_field(stable_id)
        if kind == "prompt_result":
            return self._parse_prompt_result(stable_id)
        return []

    def _parse_item(self, item_id: str) -> list[Document]:
        with self._store._connect() as conn:
            row = conn.execute(
                """
                SELECT i.*,
                       COALESCE(i.current_plain_value,e.markdown) AS plain_text,
                       COALESCE(i.current_content_sha256,e.content_sha256) AS content_sha,
                       COALESCE(e.version,i.current_revision) AS authority_revision
                FROM journal_items AS i
                LEFT JOIN journal_entries AS e ON e.entry_id=i.legacy_entry_id
                WHERE i.item_id=?
                  AND (
                    i.import_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=i.import_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                """,
                (item_id,),
            ).fetchone()
            relation_rows = conn.execute(
                "SELECT relation_id,relation_kind,target_domain,target_id,"
                "target_revision,revision FROM journal_relations "
                "WHERE source_item_id=? AND lifecycle='current' "
                "ORDER BY relation_id",
                (item_id,),
            ).fetchall()
        if (
            row is None
            or row["lifecycle"] in {"tombstoned", "superseded"}
            or row["search_mode"] in {"structured_only", "excluded"}
            or row["plain_text"] is None
        ):
            return []
        text = str(row["plain_text"])
        title = "Running note" if row["item_kind"] == "running_note" else "Journal record"
        relations = [
            {
                "relationId": str(relation["relation_id"]),
                "kind": str(relation["relation_kind"]),
                "targetDomain": str(relation["target_domain"]),
                "targetId": str(relation["target_id"]),
                "targetRevision": relation["target_revision"],
                "revision": int(relation["revision"]),
            }
            for relation in relation_rows
        ]
        relation_text = " ".join(
            f"{item['kind']} {item['targetDomain']} {item['targetId']}"
            for item in relations
        )
        projections = (
            {"content": Projection(text=text)}
            if row["search_mode"] in {"dense", "lexical_dense"}
            else {}
        )
        stable = f"item:{item_id}"
        return [
            Document(
                doc_id=make_doc_id(_PARTITION, stable),
                partition=_PARTITION,
                fields={
                    "title": title,
                    "body": text,
                    "tags": " ".join(
                        part
                        for part in (str(row["item_kind"]), relation_text)
                        if part
                    ),
                },
                display_text=f"{row['local_date']} · {title}",
                metadata={
                    "entityKind": "item",
                    "itemId": item_id,
                    "localDate": row["local_date"],
                    "itemKind": row["item_kind"],
                    "revision": int(row["authority_revision"]),
                    "lifecycle": row["lifecycle"],
                    "privacyClass": row["privacy_class"],
                    "sourceRef": row["source_ref"],
                    "relations": relations,
                },
                projections=projections,
                content_hash=_hash(row["content_sha"], row["authority_revision"]),
                timestamp=_timestamp(row["updated_at"]),
            )
        ]

    def _parse_field(self, value_id: str) -> list[Document]:
        with self._store._connect() as conn:
            row = conn.execute(
                """
                SELECT v.*,f.label,f.search_mode,f.privacy_class
                FROM journal_field_values AS v
                JOIN journal_field_definition_versions AS f
                  ON f.field_id=v.field_id
                 AND f.definition_version=v.field_definition_version
                WHERE v.value_id=?
                  AND (
                    v.import_cohort_id IS NULL
                    OR (
                      EXISTS(
                        SELECT 1 FROM journal_import_cohorts AS cohort
                        WHERE cohort.cohort_id=v.import_cohort_id
                          AND cohort.state='sealed'
                      )
                      AND EXISTS(
                        SELECT 1 FROM journal_authority_control AS authority
                        WHERE authority.singleton=1 AND authority.mode='database_only'
                      )
                    )
                  )
                """,
                (value_id,),
            ).fetchone()
        if (
            row is None
            or row["lifecycle"] == "tombstoned"
            or row["search_mode"] in {"structured_only", "excluded"}
        ):
            return []
        value = self._domain.get_field_value(value_id)
        if value.disposition is not None:
            body = value.disposition.value
        elif isinstance(value.value, list):
            body = " ".join(
                item if isinstance(item, str) else f"{item.get('kind','')} {item.get('id','')}"
                for item in value.value
            )
        else:
            body = str(value.value)
        projections = (
            {"content": Projection(text=f"{row['label']}\n{body}")}
            if row["search_mode"] in {"dense", "lexical_dense"}
            else {}
        )
        stable = f"field:{value_id}"
        return [
            Document(
                doc_id=make_doc_id(_PARTITION, stable),
                partition=_PARTITION,
                fields={"title": str(row["label"]), "body": body, "tags": str(row["value_kind"])},
                display_text=f"{row['local_date']} · {row['label']}: {body}",
                metadata={
                    "entityKind": "field_value",
                    "valueId": value_id,
                    "fieldId": row["field_id"],
                    "localDate": row["local_date"],
                    "revision": int(row["current_revision"]),
                    "lifecycle": row["lifecycle"],
                    "privacyClass": row["privacy_class"],
                },
                projections=projections,
                content_hash=_hash(row["field_id"], row["current_revision"], body),
                timestamp=_timestamp(row["updated_at"]),
            )
        ]

    def _parse_prompt_result(self, variant_id: str) -> list[Document]:
        with self._store._connect() as conn:
            row = conn.execute(
                """
                SELECT v.*,i.local_date,i.result_search_mode
                FROM journal_prompt_result_variants AS v
                JOIN journal_prompt_interactions AS i ON i.interaction_id=v.interaction_id
                WHERE v.variant_id=?
                """,
                (variant_id,),
            ).fetchone()
        if (
            row is None
            or row["result_search_mode"] != "content"
            or row["lifecycle"] == "archived"
            or row["result_text"] is None
        ):
            return []
        text = str(row["result_text"])
        stable = f"prompt_result:{variant_id}"
        return [
            Document(
                doc_id=make_doc_id(_PARTITION, stable),
                partition=_PARTITION,
                fields={"title": "Generated Journal result", "body": text, "tags": "prompt result"},
                display_text=f"{row['local_date']} · Generated Journal result",
                metadata={
                    "entityKind": "prompt_result",
                    "variantId": variant_id,
                    "localDate": row["local_date"],
                    "revision": int(row["current_revision"]),
                    "lifecycle": row["lifecycle"],
                    "privacyClass": "private",
                },
                projections={"content": Projection(text=text)},
                content_hash=_hash(row["result_content_sha256"], row["current_revision"]),
                timestamp=_timestamp(row["updated_at"]),
            )
        ]

    def hydrate(self, hits: list, **_opts: Any) -> list[dict[str, Any]]:
        """Revalidate current revision/visibility before returning search results."""

        hydrated: list[dict[str, Any]] = []
        refs = {ref.item_id: ref.content_hash for ref in self.discover()}
        for hit in hits:
            stable = hit.doc_id.split(":", 1)[1] if ":" in hit.doc_id else hit.doc_id
            if stable not in refs:
                continue
            docs = self.parse(stable)
            if not docs:
                continue
            doc = docs[0]
            indexed_revision = hit.metadata.get("revision") if hit.metadata else None
            if indexed_revision is not None and indexed_revision != doc.metadata.get("revision"):
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


register_partition(_PARTITION, lambda: JournalPartition())
