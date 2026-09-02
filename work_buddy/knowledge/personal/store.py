"""Transactional SQLite store for personal knowledge.

Logical paths are mutable aliases.  ``unit_id`` is the durable identity used
by revisions, observations, edges, and the search outbox.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from work_buddy.cutover_maintenance import require_mutations_open
from work_buddy.knowledge.personal.migrations import PERSONAL_KNOWLEDGE_MIGRATIONS
from work_buddy.installed_authority import require_domain_store_open
from work_buddy.paths import resolve


_PLACEHOLDER_RE = re.compile(r"<<wb:([^>\s]+)(?:\s+[^>]*)?>>")
VALID_PRIVACY_CLASSES = frozenset({"private", "restricted", "public"})
VALID_DISCLOSURE_CLASSES = frozenset(
    {"local_only", "consent_required", "shareable"}
)
VALID_BODY_MODES = frozenset({"plain", "document"})
VALID_LIFECYCLES = frozenset({"active", "archived", "tombstoned"})


class PersonalKnowledgeError(RuntimeError):
    """Base personal-knowledge authority error."""


class PersonalKnowledgeNotFound(PersonalKnowledgeError):
    pass


class PersonalKnowledgeConflict(PersonalKnowledgeError):
    pass


class PersonalKnowledgeRevisionConflict(PersonalKnowledgeConflict):
    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__(f"expected revision {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


class PersonalKnowledgeIdempotencyConflict(PersonalKnowledgeConflict):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_logical_path(value: str) -> str:
    """Return a canonical ``personal/<path>`` alias."""

    raw = str(value or "").strip().replace("\\", "/").strip("/")
    if not raw:
        raise ValueError("logical_path is required")
    if raw == "personal":
        raise ValueError("logical_path must identify a unit below personal/")
    if not raw.startswith("personal/"):
        raw = f"personal/{raw}"
    parts = raw.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("logical_path contains an invalid segment")
    return "/".join(parts)


def _ordered_unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        norm = text.casefold()
        if text and norm not in seen:
            seen.add(norm)
            result.append(text)
    return result


def _as_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _ordered_unique(part.strip() for part in value.split(","))
    return _ordered_unique(value)


def _append_evidence(body: str, evidence: str, observed_at: str) -> str:
    line = f"* {observed_at[:10]} - {evidence}"
    if "## Evidence" in body:
        return body.replace("## Evidence\n", f"## Evidence\n{line}\n", 1)
    return body.rstrip() + f"\n\n## Evidence\n{line}\n"


class PersonalKnowledgeStore:
    """Owns all reads and writes for one personal-knowledge database."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else resolve(
            "db/personal-knowledge"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def existing_authority(db_path: str | Path | None = None) -> str:
        """Inspect an existing authority DB without creating or migrating it.

        A missing database is the intentional pre-cutover state.  An existing
        but unreadable/invalid database is surfaced instead of silently
        falling back to Markdown after a possible seal.
        """

        path = Path(db_path) if db_path is not None else resolve("db/personal-knowledge")
        require_domain_store_open("personal_knowledge", path)
        if not path.is_file():
            return "legacy_markdown"
        try:
            conn = sqlite3.connect(
                f"file:{path.resolve()}?mode=ro", uri=True, timeout=10
            )
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT authority FROM personal_knowledge_authority WHERE singleton=1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise PersonalKnowledgeError(
                "existing personal knowledge authority database is invalid"
            ) from exc
        finally:
            if "conn" in locals():
                conn.close()
        if row is None or row["authority"] not in {"legacy_markdown", "sqlite"}:
            raise PersonalKnowledgeError("personal knowledge authority state is invalid")
        return str(row["authority"])

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        require_domain_store_open("personal_knowledge", self.db_path)
        if read_only:
            conn = sqlite3.connect(
                f"file:{self.db_path.resolve()}?mode=ro",
                uri=True,
                timeout=10,
            )
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        if not read_only:
            conn.execute("PRAGMA journal_mode = WAL")
            PERSONAL_KNOWLEDGE_MIGRATIONS.run(conn)
        return conn

    def schema_version(self) -> int:
        conn = self.connect()
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    @staticmethod
    def validate_connection(conn: sqlite3.Connection) -> None:
        """Validate authority invariants after restore or during diagnostics."""

        conn.row_factory = sqlite3.Row
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise PersonalKnowledgeError("personal knowledge foreign-key check failed")
        missing_revision = conn.execute(
            "SELECT u.unit_id FROM personal_units u LEFT JOIN personal_unit_revisions r "
            "ON r.unit_id=u.unit_id AND r.revision=u.current_revision "
            "WHERE r.unit_id IS NULL LIMIT 1"
        ).fetchone()
        if missing_revision is not None:
            raise PersonalKnowledgeError("personal unit current revision is not retained")
        missing_path = conn.execute(
            "SELECT u.unit_id FROM personal_units u LEFT JOIN personal_unit_paths p "
            "ON p.unit_id=u.unit_id AND p.logical_path=u.current_path AND p.is_current=1 "
            "WHERE p.unit_id IS NULL LIMIT 1"
        ).fetchone()
        if missing_path is not None:
            raise PersonalKnowledgeError("personal unit current path alias is inconsistent")
        missing_outbox = conn.execute(
            "SELECT u.unit_id FROM personal_units u LEFT JOIN personal_search_outbox o "
            "ON o.unit_id=u.unit_id AND o.revision=u.current_revision "
            "WHERE o.unit_id IS NULL LIMIT 1"
        ).fetchone()
        if missing_outbox is not None:
            raise PersonalKnowledgeError("personal unit current revision lacks an outbox event")
        for row in conn.execute(
            "SELECT snapshot_json,snapshot_sha256 FROM personal_unit_revisions"
        ):
            actual = hashlib.sha256(row["snapshot_json"].encode("utf-8")).hexdigest()
            if actual != row["snapshot_sha256"]:
                raise PersonalKnowledgeError("personal unit revision digest mismatch")
        for row in conn.execute(
            "SELECT result_json,result_sha256 FROM personal_import_receipts"
        ):
            actual = hashlib.sha256(row["result_json"].encode("utf-8")).hexdigest()
            if actual != row["result_sha256"]:
                raise PersonalKnowledgeError("personal import receipt digest mismatch")

    def validate(self) -> None:
        conn = self.connect()
        try:
            self.validate_connection(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public reads
    # ------------------------------------------------------------------

    def authority_status(self) -> dict[str, Any]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM personal_knowledge_authority WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise PersonalKnowledgeError("personal knowledge authority is missing")
            return dict(row)
        finally:
            conn.close()

    def resolve_unit_id(self, identity: str, *, conn: sqlite3.Connection | None = None) -> str:
        owned = conn is None
        db = conn or self.connect()
        try:
            direct = db.execute(
                "SELECT unit_id FROM personal_units WHERE unit_id=?", (identity,)
            ).fetchone()
            if direct is not None:
                return str(direct["unit_id"])
            try:
                logical_path = normalize_logical_path(identity)
            except ValueError:
                raise PersonalKnowledgeNotFound(identity) from None
            row = db.execute(
                "SELECT unit_id FROM personal_unit_paths WHERE logical_path=?",
                (logical_path,),
            ).fetchone()
            if row is None:
                raise PersonalKnowledgeNotFound(identity)
            return str(row["unit_id"])
        finally:
            if owned:
                db.close()

    def get_unit(
        self,
        identity: str,
        *,
        include_tombstoned: bool = False,
    ) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            try:
                unit_id = self.resolve_unit_id(identity, conn=conn)
            except PersonalKnowledgeNotFound:
                return None
            record = self._record(conn, unit_id)
            if record["lifecycle"] == "tombstoned" and not include_tombstoned:
                return None
            return record
        finally:
            conn.close()

    def list_units(
        self,
        *,
        path_prefix: str | None = None,
        category: str | None = None,
        lifecycles: Sequence[str] = ("active", "archived"),
        privacy_classes: Sequence[str] | None = None,
        disclosure_classes: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if lifecycles:
                clauses.append(
                    f"u.lifecycle IN ({','.join('?' for _ in lifecycles)})"
                )
                params.extend(lifecycles)
            if path_prefix:
                prefix = normalize_logical_path(path_prefix).rstrip("/")
                clauses.append(
                    "EXISTS (SELECT 1 FROM personal_unit_paths p "
                    "WHERE p.unit_id=u.unit_id AND "
                    "(p.logical_path=? OR p.logical_path LIKE ?))"
                )
                params.extend((prefix, f"{prefix}/%"))
            if category:
                clauses.append(
                    "EXISTS (SELECT 1 FROM personal_unit_categories c "
                    "WHERE c.unit_id=u.unit_id AND c.category=?)"
                )
                params.append(category)
            if privacy_classes is not None:
                if not privacy_classes:
                    return []
                clauses.append(
                    f"u.privacy_class IN ({','.join('?' for _ in privacy_classes)})"
                )
                params.extend(privacy_classes)
            if disclosure_classes is not None:
                if not disclosure_classes:
                    return []
                clauses.append(
                    f"u.disclosure_class IN ({','.join('?' for _ in disclosure_classes)})"
                )
                params.extend(disclosure_classes)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = conn.execute(
                "SELECT u.unit_id FROM personal_units u" + where +
                " ORDER BY u.current_path",
                params,
            ).fetchall()
            return [self._record(conn, str(row["unit_id"])) for row in rows]
        finally:
            conn.close()

    def search(
        self,
        query: str = "",
        *,
        path_prefix: str | None = None,
        category: str | None = None,
        privacy_classes: Sequence[str] | None = None,
        disclosure_classes: Sequence[str] | None = None,
        include_tombstoned: bool = False,
    ) -> list[dict[str, Any]]:
        lifecycles = ("active", "archived", "tombstoned") if include_tombstoned else (
            "active",
            "archived",
        )
        rows = self.list_units(
            path_prefix=path_prefix,
            category=category,
            lifecycles=lifecycles,
            privacy_classes=privacy_classes,
            disclosure_classes=disclosure_classes,
        )
        needle = query.strip().casefold()
        if not needle:
            return rows
        result: list[dict[str, Any]] = []
        for row in rows:
            haystack = "\n".join(
                [
                    row["current_path"],
                    row["name"],
                    row["description"],
                    row["summary"],
                    row.get("body") or "",
                    *row["aliases"],
                    *row["tags"],
                    *row["categories"],
                ]
            ).casefold()
            if needle in haystack:
                result.append(row)
        return result

    def observations(self, identity: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            unit_id = self.resolve_unit_id(identity, conn=conn)
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM personal_observations WHERE unit_id=? "
                    "ORDER BY observed_at, observation_id",
                    (unit_id,),
                ).fetchall()
            ]
        finally:
            conn.close()

    def revisions(self, identity: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            unit_id = self.resolve_unit_id(identity, conn=conn)
            return [
                {**dict(row), "snapshot": json.loads(row["snapshot_json"])}
                for row in conn.execute(
                    "SELECT * FROM personal_unit_revisions WHERE unit_id=? "
                    "ORDER BY revision",
                    (unit_id,),
                ).fetchall()
            ]
        finally:
            conn.close()

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM personal_search_outbox WHERE delivered_at IS NULL "
                    "ORDER BY committed_at,event_id LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            ]
        finally:
            conn.close()

    def receipt_result(
        self, idempotency_key: str, request: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT request_sha256,result_json FROM personal_mutation_receipts "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            if row["request_sha256"] != sha256_json(request):
                raise PersonalKnowledgeIdempotencyConflict(
                    "idempotency key was already used for a different request"
                )
            return json.loads(row["result_json"])
        finally:
            conn.close()

    def mark_outbox_delivered(self, event_id: str) -> bool:
        conn = self.connect()
        try:
            cursor = conn.execute(
                "UPDATE personal_search_outbox SET delivered_at=? "
                "WHERE event_id=? AND delivered_at IS NULL",
                (utcnow(), event_id),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public mutations
    # ------------------------------------------------------------------

    def create_unit(
        self,
        *,
        logical_path: str,
        name: str,
        description: str = "",
        summary: str = "",
        body: str | None = "",
        body_mode: str = "plain",
        document_binding_id: str | None = None,
        document_store_id: str | None = None,
        document_id: str | None = None,
        interaction_contract_id: str = "personal_note/v1",
        interaction_contract_version: int = 1,
        categories: Sequence[str] = (),
        aliases: Sequence[str] = (),
        tags: Sequence[str] = (),
        requires: Sequence[str] = (),
        parent_paths: Sequence[str] = (),
        reference_paths: Sequence[str] = (),
        severity: str = "",
        privacy_class: str = "private",
        disclosure_class: str = "local_only",
        observation_count: int = 0,
        last_observed: str = "",
        observations: Sequence[Mapping[str, Any]] = (),
        source_file: str = "",
        source_ref: str | None = None,
        actor: str = "user",
        idempotency_key: str | None = None,
        unit_id: str | None = None,
        mutation_kind: str = "create",
        idempotency_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        stable_unit_id = unit_id or (
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"personal-mutation:{key}"))
            if idempotency_key
            else str(uuid.uuid4())
        )
        payload = self._normalize_payload(
            {
                "unit_id": stable_unit_id,
                "logical_path": logical_path,
                "name": name,
                "description": description,
                "summary": summary,
                "body": body,
                "body_mode": body_mode,
                "document_binding_id": document_binding_id,
                "document_store_id": document_store_id,
                "document_id": document_id,
                "interaction_contract_id": interaction_contract_id,
                "interaction_contract_version": interaction_contract_version,
                "categories": categories,
                "aliases": aliases,
                "tags": tags,
                "requires": requires,
                "parent_paths": parent_paths,
                "reference_paths": reference_paths,
                "severity": severity,
                "privacy_class": privacy_class,
                "disclosure_class": disclosure_class,
                "observation_count": observation_count,
                "last_observed": last_observed,
                "observations": observations,
                "source_file": source_file,
                "source_ref": source_ref,
            }
        )
        request = dict(idempotency_request) if idempotency_request is not None else {
            "operation": mutation_kind,
            "payload": payload,
        }
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._receipt_replay(conn, key, request)
            if replay is not None:
                conn.rollback()
                return replay
            require_mutations_open(conn, domain="personal_knowledge")
            result = self._insert_unit(conn, payload, actor=actor, mutation_kind=mutation_kind)
            self._record_receipt(conn, key, request, mutation_kind, result)
            conn.commit()
            return result
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise PersonalKnowledgeConflict(str(exc)) from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_unit(
        self,
        identity: str,
        patch: Mapping[str, Any],
        *,
        expected_revision: int,
        actor: str = "user",
        idempotency_key: str | None = None,
        mutation_kind: str = "update",
    ) -> dict[str, Any]:
        clean_patch = dict(patch)
        request = {
            "operation": mutation_kind,
            "identity": identity,
            "expected_revision": expected_revision,
            "patch": clean_patch,
        }
        key = idempotency_key or str(uuid.uuid4())
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._receipt_replay(conn, key, request)
            if replay is not None:
                conn.rollback()
                return replay
            require_mutations_open(conn, domain="personal_knowledge")
            unit_id = self.resolve_unit_id(identity, conn=conn)
            current = self._record(conn, unit_id)
            if current["current_revision"] != int(expected_revision):
                raise PersonalKnowledgeRevisionConflict(
                    expected=int(expected_revision), actual=current["current_revision"]
                )
            payload = self._normalize_payload(self._merged_payload(current, clean_patch))
            revision = current["current_revision"] + 1
            now = utcnow()
            conn.execute(
                "UPDATE personal_units SET current_path=?,name=?,description=?,summary=?,"
                "body=?,body_mode=?,document_binding_id=?,document_store_id=?,document_id=?,"
                "interaction_contract_id=?,interaction_contract_version=?,severity=?,"
                "privacy_class=?,disclosure_class=?,lifecycle=?,current_revision=?,"
                "source_file=?,source_ref=?,updated_at=?,tombstoned_at=? WHERE unit_id=?",
                (
                    payload["logical_path"], payload["name"], payload["description"],
                    payload["summary"], payload["body"], payload["body_mode"],
                    payload["document_binding_id"], payload["document_store_id"],
                    payload["document_id"], payload["interaction_contract_id"],
                    payload["interaction_contract_version"], payload["severity"],
                    payload["privacy_class"], payload["disclosure_class"],
                    payload["lifecycle"], revision, payload["source_file"],
                    payload["source_ref"], now,
                    now if payload["lifecycle"] == "tombstoned" else None,
                    unit_id,
                ),
            )
            if payload["logical_path"] != current["current_path"]:
                conn.execute(
                    "UPDATE personal_unit_paths SET is_current=0,retired_at=? "
                    "WHERE unit_id=? AND is_current=1",
                    (now, unit_id),
                )
                conn.execute(
                    "INSERT INTO personal_unit_paths "
                    "(logical_path,unit_id,is_current,created_at) VALUES (?,?,1,?)",
                    (payload["logical_path"], unit_id, now),
                )
            self._replace_collections(conn, unit_id, payload)
            self._replace_edges(conn, unit_id, payload, now)
            self._resolve_pending_edges(conn)
            record = self._record(conn, unit_id)
            self._write_revision(
                conn, record, mutation_kind=mutation_kind, actor=actor,
                source_ref=payload["source_ref"], intent_id=key, now=now,
            )
            self._write_outbox(conn, record, now=now)
            result = self._result(
                record, "updated" if mutation_kind == "update" else mutation_kind
            )
            self._record_receipt(conn, key, request, mutation_kind, result)
            conn.commit()
            return result
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise PersonalKnowledgeConflict(str(exc)) from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_observation(
        self,
        identity: str,
        *,
        evidence: str,
        observed_at: str | None = None,
        source_ref: str | None = None,
        expected_revision: int,
        actor: str = "user",
        idempotency_key: str | None = None,
        idempotency_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not evidence.strip():
            raise ValueError("evidence is required")
        observed = observed_at or utcnow()
        request = dict(idempotency_request) if idempotency_request is not None else {
            "operation": "observe",
            "identity": identity,
            "expected_revision": expected_revision,
            "evidence": evidence,
            "observed_at": observed,
            "source_ref": source_ref,
        }
        key = idempotency_key or str(uuid.uuid4())
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._receipt_replay(conn, key, request)
            if replay is not None:
                conn.rollback()
                return replay
            require_mutations_open(conn, domain="personal_knowledge")
            unit_id = self.resolve_unit_id(identity, conn=conn)
            current = self._record(conn, unit_id)
            if current["current_revision"] != int(expected_revision):
                raise PersonalKnowledgeRevisionConflict(
                    expected=int(expected_revision), actual=current["current_revision"]
                )
            revision = current["current_revision"] + 1
            now = utcnow()
            body = current["body"]
            if current["body_mode"] == "plain":
                body = _append_evidence(body or "", evidence.strip(), observed)
            conn.execute(
                "UPDATE personal_units SET body=?,observation_count=observation_count+1,"
                "last_observed=?,current_revision=?,updated_at=? WHERE unit_id=?",
                (body, observed[:10], revision, now, unit_id),
            )
            observation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{unit_id}:{key}"))
            conn.execute(
                "INSERT INTO personal_observations "
                "(observation_id,unit_id,observed_at,evidence,source_ref,actor,"
                "unit_revision,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    observation_id, unit_id, observed, evidence.strip(), source_ref,
                    actor, revision, now,
                ),
            )
            record = self._record(conn, unit_id)
            self._write_revision(
                conn, record, mutation_kind="observe", actor=actor,
                source_ref=source_ref, intent_id=key, now=now,
            )
            self._write_outbox(conn, record, now=now)
            result = self._result(record, "updated")
            result["observation_id"] = observation_id
            self._record_receipt(conn, key, request, "observe", result)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def tombstone_unit(
        self,
        identity: str,
        *,
        expected_revision: int,
        actor: str = "user",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.update_unit(
            identity,
            {"lifecycle": "tombstoned"},
            expected_revision=expected_revision,
            actor=actor,
            idempotency_key=idempotency_key,
            mutation_kind="tombstone",
        )

    def restore_unit(
        self,
        identity: str,
        *,
        expected_revision: int,
        actor: str = "user",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.update_unit(
            identity,
            {"lifecycle": "active"},
            expected_revision=expected_revision,
            actor=actor,
            idempotency_key=idempotency_key,
            mutation_kind="restore",
        )

    # ------------------------------------------------------------------
    # Importer-facing transaction helpers
    # ------------------------------------------------------------------

    def insert_imported_unit(
        self,
        conn: sqlite3.Connection,
        payload: Mapping[str, Any],
        *,
        cohort_id: str,
        source_ref: str | None,
    ) -> dict[str, Any]:
        normalized = self._normalize_payload(dict(payload))
        normalized["source_ref"] = source_ref
        return self._insert_unit(
            conn,
            normalized,
            actor="migration",
            mutation_kind=f"import:{cohort_id}",
            intent_id=f"import:{cohort_id}:{normalized['unit_id']}",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        result["unit_id"] = str(result.get("unit_id") or uuid.uuid4())
        result["logical_path"] = normalize_logical_path(str(result["logical_path"]))
        result["name"] = str(result.get("name") or "").strip()
        if not result["name"]:
            raise ValueError("name is required")
        result["description"] = str(result.get("description") or "")
        result["summary"] = str(result.get("summary") or "")
        result["body_mode"] = str(result.get("body_mode") or "plain")
        if result["body_mode"] not in VALID_BODY_MODES:
            raise ValueError("body_mode must be plain or document")
        result["document_binding_id"] = result.get("document_binding_id") or None
        result["document_store_id"] = result.get("document_store_id") or None
        result["document_id"] = result.get("document_id") or None
        if result["body_mode"] == "plain":
            if any(
                result[key]
                for key in ("document_binding_id", "document_store_id", "document_id")
            ):
                raise ValueError("plain bodies cannot carry a document binding")
            result["body"] = str(result.get("body") or "")
        else:
            if not all(
                result[key]
                for key in ("document_binding_id", "document_store_id", "document_id")
            ):
                raise ValueError("document bodies require binding, store, and document IDs")
            if result.get("body") not in (None, ""):
                raise ValueError("document bodies cannot retain a second plain body")
            result["body"] = None
        result["interaction_contract_id"] = str(
            result.get("interaction_contract_id") or "personal_note/v1"
        )
        result["interaction_contract_version"] = int(
            result.get("interaction_contract_version") or 1
        )
        result["categories"] = _as_values(
            result.get("categories", result.get("category", []))
        )
        result["aliases"] = _as_values(result.get("aliases", []))
        result["tags"] = _as_values(result.get("tags", []))
        result["requires"] = _as_values(result.get("requires", []))
        result["parent_paths"] = [
            normalize_logical_path(v) for v in _as_values(result.get("parent_paths", []))
        ]
        explicit_refs = _as_values(result.get("reference_paths", []))
        body_refs = _PLACEHOLDER_RE.findall(result.get("body") or "")
        result["reference_paths"] = _ordered_unique(
            normalize_logical_path(v)
            for v in [*explicit_refs, *body_refs]
            if str(v).startswith("personal/")
        )
        result["severity"] = str(result.get("severity") or "")
        result["privacy_class"] = str(result.get("privacy_class") or "private")
        if result["privacy_class"] not in VALID_PRIVACY_CLASSES:
            raise ValueError("invalid privacy_class")
        result["disclosure_class"] = str(
            result.get("disclosure_class") or "local_only"
        )
        if result["disclosure_class"] not in VALID_DISCLOSURE_CLASSES:
            raise ValueError("invalid disclosure_class")
        result["lifecycle"] = str(result.get("lifecycle") or "active")
        if result["lifecycle"] not in VALID_LIFECYCLES:
            raise ValueError("invalid lifecycle")
        result["observation_count"] = max(0, int(result.get("observation_count") or 0))
        result["last_observed"] = str(result.get("last_observed") or "")
        normalized_observations: list[dict[str, Any]] = []
        for observation in result.get("observations", []) or []:
            evidence = str(observation.get("evidence") or "").strip()
            if not evidence:
                continue
            normalized_observations.append(
                {
                    "observed_at": str(observation.get("observed_at") or ""),
                    "evidence": evidence,
                    "source_ref": observation.get("source_ref") or None,
                    "actor": str(observation.get("actor") or "migration"),
                }
            )
        result["observations"] = normalized_observations
        result["observation_count"] = max(
            result["observation_count"], len(normalized_observations)
        )
        result["source_file"] = str(result.get("source_file") or "")
        result["source_ref"] = result.get("source_ref") or None
        return result

    @staticmethod
    def _merged_payload(current: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "logical_path", "name", "description", "summary", "body", "body_mode",
            "document_binding_id", "document_store_id", "document_id",
            "interaction_contract_id", "interaction_contract_version", "categories",
            "category", "aliases", "tags", "requires", "parent_paths",
            "reference_paths", "severity", "privacy_class", "disclosure_class",
            "lifecycle", "source_file", "source_ref",
        }
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"unsupported personal knowledge fields: {sorted(unknown)}")
        base = {
            "unit_id": current["unit_id"],
            "logical_path": current["current_path"],
            "name": current["name"],
            "description": current["description"],
            "summary": current["summary"],
            "body": current["body"],
            "body_mode": current["body_mode"],
            "document_binding_id": current["document_binding_id"],
            "document_store_id": current["document_store_id"],
            "document_id": current["document_id"],
            "interaction_contract_id": current["interaction_contract_id"],
            "interaction_contract_version": current["interaction_contract_version"],
            "categories": current["categories"],
            "aliases": current["aliases"],
            "tags": current["tags"],
            "requires": current["requires"],
            "parent_paths": current["parent_paths"],
            "reference_paths": current["reference_paths"],
            "severity": current["severity"],
            "privacy_class": current["privacy_class"],
            "disclosure_class": current["disclosure_class"],
            "lifecycle": current["lifecycle"],
            "observation_count": current["observation_count"],
            "last_observed": current["last_observed"],
            "source_file": current["source_file"],
            "source_ref": current["source_ref"],
        }
        base.update(patch)
        return base

    def _insert_unit(
        self,
        conn: sqlite3.Connection,
        payload: Mapping[str, Any],
        *,
        actor: str,
        mutation_kind: str,
        intent_id: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        unit_id = str(payload["unit_id"])
        conn.execute(
            "INSERT INTO personal_units "
            "(unit_id,current_path,name,description,summary,body,body_mode,"
            "document_binding_id,document_store_id,document_id,interaction_contract_id,"
            "interaction_contract_version,severity,privacy_class,disclosure_class,"
            "lifecycle,current_revision,observation_count,last_observed,source_file,"
            "source_ref,created_at,updated_at,tombstoned_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
            (
                unit_id, payload["logical_path"], payload["name"],
                payload["description"], payload["summary"], payload["body"],
                payload["body_mode"], payload["document_binding_id"],
                payload["document_store_id"], payload["document_id"],
                payload["interaction_contract_id"],
                payload["interaction_contract_version"], payload["severity"],
                payload["privacy_class"], payload["disclosure_class"],
                payload.get("lifecycle", "active"), payload["observation_count"],
                payload["last_observed"], payload["source_file"], payload["source_ref"],
                now, now, now if payload.get("lifecycle") == "tombstoned" else None,
            ),
        )
        conn.execute(
            "INSERT INTO personal_unit_paths "
            "(logical_path,unit_id,is_current,created_at) VALUES (?,?,1,?)",
            (payload["logical_path"], unit_id, now),
        )
        self._replace_collections(conn, unit_id, payload)
        self._replace_edges(conn, unit_id, payload, now)
        self._resolve_pending_edges(conn)
        for ordinal, observation in enumerate(payload.get("observations", [])):
            observation_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"personal-observation:{unit_id}:{intent_id or now}:{ordinal}",
                )
            )
            conn.execute(
                "INSERT INTO personal_observations "
                "(observation_id,unit_id,observed_at,evidence,source_ref,actor,"
                "unit_revision,created_at) VALUES (?,?,?,?,?,?,1,?)",
                (
                    observation_id, unit_id,
                    observation.get("observed_at") or payload["last_observed"] or now,
                    observation["evidence"],
                    observation.get("source_ref") or payload["source_ref"],
                    observation.get("actor") or actor, now,
                ),
            )
        record = self._record(conn, unit_id)
        self._write_revision(
            conn, record, mutation_kind=mutation_kind, actor=actor,
            source_ref=payload["source_ref"], intent_id=intent_id, now=now,
        )
        self._write_outbox(conn, record, now=now)
        return self._result(record, "created" if mutation_kind == "create" else mutation_kind)

    @staticmethod
    def _replace_simple(
        conn: sqlite3.Connection,
        table: str,
        value_column: str,
        unit_id: str,
        values: Sequence[str],
        *,
        norm_column: str | None = None,
    ) -> None:
        conn.execute(f"DELETE FROM {table} WHERE unit_id=?", (unit_id,))
        for ordinal, value in enumerate(values):
            if norm_column:
                conn.execute(
                    f"INSERT INTO {table} (unit_id,{value_column},{norm_column},ordinal) "
                    "VALUES (?,?,?,?)",
                    (unit_id, value, value.casefold(), ordinal),
                )
            else:
                conn.execute(
                    f"INSERT INTO {table} (unit_id,{value_column},ordinal) VALUES (?,?,?)",
                    (unit_id, value, ordinal),
                )

    def _replace_collections(
        self, conn: sqlite3.Connection, unit_id: str, payload: Mapping[str, Any]
    ) -> None:
        self._replace_simple(
            conn, "personal_unit_categories", "category", unit_id, payload["categories"]
        )
        self._replace_simple(
            conn, "personal_unit_aliases", "alias", unit_id, payload["aliases"],
            norm_column="alias_norm",
        )
        self._replace_simple(
            conn, "personal_unit_tags", "tag", unit_id, payload["tags"],
            norm_column="tag_norm",
        )
        self._replace_simple(
            conn, "personal_unit_requirements", "requirement", unit_id,
            payload["requires"],
        )

    def _replace_edges(
        self,
        conn: sqlite3.Connection,
        unit_id: str,
        payload: Mapping[str, Any],
        now: str,
    ) -> None:
        conn.execute("DELETE FROM personal_unit_edges WHERE source_unit_id=?", (unit_id,))
        for kind, paths in (
            ("parent", payload["parent_paths"]),
            ("reference", payload["reference_paths"]),
        ):
            for ordinal, target_path in enumerate(paths):
                target = conn.execute(
                    "SELECT unit_id FROM personal_unit_paths WHERE logical_path=?",
                    (target_path,),
                ).fetchone()
                edge_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"personal-edge:{unit_id}:{kind}:{target_path}")
                )
                conn.execute(
                    "INSERT INTO personal_unit_edges "
                    "(edge_id,source_unit_id,edge_kind,target_unit_id,target_path,ordinal,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        edge_id, unit_id, kind,
                        str(target["unit_id"]) if target is not None else None,
                        target_path, ordinal, now,
                    ),
                )

    @staticmethod
    def _resolve_pending_edges(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE personal_unit_edges SET target_unit_id=("
            "SELECT p.unit_id FROM personal_unit_paths p "
            "WHERE p.logical_path=personal_unit_edges.target_path) "
            "WHERE target_unit_id IS NULL AND EXISTS ("
            "SELECT 1 FROM personal_unit_paths p "
            "WHERE p.logical_path=personal_unit_edges.target_path)"
        )

    def _record(self, conn: sqlite3.Connection, unit_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM personal_units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        if row is None:
            raise PersonalKnowledgeNotFound(unit_id)
        record = dict(row)
        record["path_aliases"] = [
            str(r["logical_path"])
            for r in conn.execute(
                "SELECT logical_path FROM personal_unit_paths "
                "WHERE unit_id=? AND is_current=0 ORDER BY created_at,logical_path",
                (unit_id,),
            ).fetchall()
        ]
        for key, table, column in (
            ("categories", "personal_unit_categories", "category"),
            ("aliases", "personal_unit_aliases", "alias"),
            ("tags", "personal_unit_tags", "tag"),
            ("requires", "personal_unit_requirements", "requirement"),
        ):
            record[key] = [
                str(r[column])
                for r in conn.execute(
                    f"SELECT {column} FROM {table} WHERE unit_id=? ORDER BY ordinal",
                    (unit_id,),
                ).fetchall()
            ]
        edge_rows = conn.execute(
            "SELECT e.edge_kind,COALESCE(u.current_path,e.target_path) AS resolved_path "
            "FROM personal_unit_edges e LEFT JOIN personal_units u "
            "ON u.unit_id=e.target_unit_id WHERE e.source_unit_id=? "
            "ORDER BY e.edge_kind,e.ordinal",
            (unit_id,),
        ).fetchall()
        record["parent_paths"] = [
            str(r["resolved_path"]) for r in edge_rows if r["edge_kind"] == "parent"
        ]
        record["reference_paths"] = [
            str(r["resolved_path"]) for r in edge_rows if r["edge_kind"] == "reference"
        ]
        record["child_paths"] = [
            str(r["current_path"])
            for r in conn.execute(
                "SELECT DISTINCT u.current_path FROM personal_unit_edges e "
                "JOIN personal_units u ON u.unit_id=e.source_unit_id "
                "WHERE e.edge_kind='parent' AND e.target_unit_id=? "
                "AND u.lifecycle!='tombstoned' ORDER BY u.current_path",
                (unit_id,),
            ).fetchall()
        ]
        return record

    @staticmethod
    def _snapshot(record: Mapping[str, Any]) -> dict[str, Any]:
        excluded = {"created_at", "updated_at", "tombstoned_at"}
        return {key: value for key, value in record.items() if key not in excluded}

    def _write_revision(
        self,
        conn: sqlite3.Connection,
        record: Mapping[str, Any],
        *,
        mutation_kind: str,
        actor: str,
        source_ref: str | None,
        intent_id: str | None,
        now: str,
    ) -> None:
        snapshot = self._snapshot(record)
        snapshot_json = canonical_json(snapshot)
        conn.execute(
            "INSERT INTO personal_unit_revisions "
            "(unit_id,revision,mutation_kind,actor,source_ref,intent_id,"
            "snapshot_sha256,snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                record["unit_id"], record["current_revision"], mutation_kind, actor,
                source_ref, intent_id,
                hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
                snapshot_json, now,
            ),
        )

    @staticmethod
    def _content_hash(record: Mapping[str, Any]) -> str:
        return sha256_json(
            {
                "path": record["current_path"],
                "name": record["name"],
                "description": record["description"],
                "summary": record["summary"],
                "body": record["body"],
                "bodyMode": record["body_mode"],
                "documentId": record["document_id"],
                "aliases": record["aliases"],
                "tags": record["tags"],
                "categories": record["categories"],
                "parents": record["parent_paths"],
                "references": record["reference_paths"],
            }
        )

    def _write_outbox(
        self, conn: sqlite3.Connection, record: Mapping[str, Any], *, now: str
    ) -> None:
        event_kind = "delete" if record["lifecycle"] == "tombstoned" else "upsert"
        event_id = f"personal-knowledge:{record['unit_id']}:revision:{record['current_revision']}"
        conn.execute(
            "INSERT INTO personal_search_outbox "
            "(event_id,unit_id,revision,event_kind,logical_path,content_sha256,"
            "privacy_class,disclosure_class,committed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id, record["unit_id"], record["current_revision"], event_kind,
                record["current_path"], self._content_hash(record),
                record["privacy_class"], record["disclosure_class"], now,
            ),
        )

    @staticmethod
    def _result(record: Mapping[str, Any], status: str) -> dict[str, Any]:
        return {
            "status": status,
            "unit_id": record["unit_id"],
            "path": record["current_path"],
            "revision": record["current_revision"],
            "lifecycle": record["lifecycle"],
            "privacy_class": record["privacy_class"],
            "disclosure_class": record["disclosure_class"],
        }

    @staticmethod
    def _receipt_replay(
        conn: sqlite3.Connection, key: str, request: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        request_hash = sha256_json(request)
        row = conn.execute(
            "SELECT request_sha256,result_json FROM personal_mutation_receipts "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_hash:
            raise PersonalKnowledgeIdempotencyConflict(
                "idempotency key was already used for a different request"
            )
        return json.loads(row["result_json"])

    @staticmethod
    def _record_receipt(
        conn: sqlite3.Connection,
        key: str,
        request: Mapping[str, Any],
        operation: str,
        result: Mapping[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO personal_mutation_receipts "
            "(idempotency_key,request_sha256,operation,unit_id,revision,result_json,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                key, sha256_json(request), operation, result.get("unit_id"),
                result.get("revision"), canonical_json(result), utcnow(),
            ),
        )
