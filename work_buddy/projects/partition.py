"""Stable-ID Projects adapter for the consolidated search index."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Sequence

from work_buddy.index.model import (
    Document,
    ItemRef,
    Projection,
    ProjectionKind,
    ProjectionSpec,
    make_doc_id,
)
from work_buddy.index.partition import register_partition
from work_buddy.projects import store


_PARTITION = "projects"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class ProjectsPartition:
    name = _PARTITION
    change_key = "hash"

    def __init__(
        self,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        # The injected seam lets the cutover certifier use SQLite ``mode=ro``
        # without calling Projects' normal migrate-on-connect runtime helper.
        self._connection_factory = connection_factory or store.get_connection

    def _connect(self):
        return self._connection_factory()

    def field_weights(self) -> dict[str, float]:
        return {
            "project_name": 2.0,
            "content": 1.5,
            "aliases": 1.0,
            "status": 0.5,
        }

    def projection_schema(self) -> dict[str, ProjectionSpec]:
        return {"content": ProjectionSpec(kind=ProjectionKind.PASSAGE)}

    def pending_search_events(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Return the durable Project revisions not yet reconciled to search."""

        conn = self._connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM project_outbox WHERE delivered_at IS NULL "
                    "ORDER BY committed_at,event_id LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            ]
        finally:
            conn.close()

    def pending_search_event_count(self) -> int:
        conn = self._connect()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM project_outbox WHERE delivered_at IS NULL"
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def acknowledge_search_events(self, events: Sequence[dict[str, Any]]) -> None:
        """Idempotently acknowledge only the exact pre-build event snapshot."""

        ids = tuple(dict.fromkeys(str(event["event_id"]) for event in events))
        if not ids:
            return
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE project_outbox SET delivered_at=? "
                f"WHERE delivered_at IS NULL AND event_id IN ({placeholders})",
                (datetime.now(UTC).isoformat(), *ids),
            )
            conn.commit()
        finally:
            conn.close()

    def discover(self) -> Iterable[ItemRef]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT p.id,p.slug,p.name,p.status,p.description,p.updated_at,
                       MAX(r.id) AS revision_id,b.body_mode,b.document_binding_id,
                       b.interaction_contract_id,b.interaction_contract_version,
                       b.privacy_class
                FROM projects AS p
                JOIN project_revisions AS r ON r.project_id=p.id
                LEFT JOIN project_body_roles AS b
                  ON b.project_id=p.id AND b.role='description'
                WHERE p.status != 'deleted'
                GROUP BY p.id,p.slug,p.name,p.status,p.description,p.updated_at,
                         b.body_mode,b.document_binding_id,
                         b.interaction_contract_id,b.interaction_contract_version,
                         b.privacy_class
                ORDER BY p.id
                """
            ).fetchall()
            return [
                ItemRef(
                    item_id=str(row["id"]),
                    content_hash=_sha(
                        {
                            "revision": row["revision_id"],
                            "slug": row["slug"],
                            "name": row["name"],
                            "status": row["status"],
                            "description": row["description"],
                            "bodyMode": row["body_mode"],
                            "bindingId": row["document_binding_id"],
                            "interaction": [
                                row["interaction_contract_id"],
                                row["interaction_contract_version"],
                            ],
                            "privacy": row["privacy_class"],
                        }
                    ),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def parse(self, item_id: str) -> list[Document]:
        try:
            project_id = int(item_id)
        except ValueError:
            return []
        # Keep every read on the injected connection.  The cutover certifier
        # injects an immutable ``mode=ro`` factory; falling back to the normal
        # store lookup here would silently open a second migrate-on-connect
        # connection while producing what is promised to be read-only evidence.
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT p.*,(SELECT MAX(r.id) FROM project_revisions AS r "
                "WHERE r.project_id=p.id) AS current_revision_id "
                "FROM projects AS p WHERE p.id=? AND p.status != 'deleted'",
                (project_id,),
            ).fetchone()
            if row is None:
                return []
            project = dict(row)
            roles = {
                str(role["role"]): dict(role)
                for role in conn.execute(
                    "SELECT role,body_mode,document_binding_id,"
                    "interaction_contract_id,interaction_contract_version,"
                    "revision_id,privacy_class,updated_at "
                    "FROM project_body_roles WHERE project_id=?",
                    (project_id,),
                ).fetchall()
            }
            aliases = [
                str(alias["alias"])
                for alias in conn.execute(
                    "SELECT alias FROM project_aliases WHERE project_id=? "
                    "ORDER BY alias_norm",
                    (project_id,),
                ).fetchall()
            ]
        finally:
            conn.close()
        description_role = roles.get("description") or {}
        description = project.get("description")
        # Canonical document text is indexed by the document partition at its
        # durable head.  Projects indexes only its current plain body.
        if description_role.get("body_mode", "plain") != "plain" or not description:
            return []
        timestamp = None
        try:
            timestamp = datetime.fromisoformat(project["updated_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            pass
        stable = f"project:{project_id}"
        return [
            Document(
                doc_id=make_doc_id(_PARTITION, stable),
                partition=_PARTITION,
                fields={
                    "project_name": project["name"],
                    "content": description,
                    "aliases": " ".join(aliases),
                    "status": project["status"],
                },
                display_text=f"[{project['slug']}] {description[:120]}",
                metadata={
                    "projectId": project_id,
                    "projectSlug": project["slug"],
                    "revision": project["current_revision_id"],
                    "lifecycle_state": project["status"],
                    "bodyMode": "plain",
                    "privacyClass": description_role.get("privacy_class", "private"),
                },
                projections={
                    "content": Projection(
                        text=f"{project['name']} — {description}"
                    )
                },
                timestamp=timestamp,
            )
        ]


register_partition(_PARTITION, ProjectsPartition)
