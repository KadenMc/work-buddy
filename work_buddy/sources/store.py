"""Invariant-enforcing persistence boundary for retained Sources items."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from work_buddy.sources.blobs import BlobRecord, BlobStore
from work_buddy.sources.errors import (
    InvalidSourceRequest,
    SourceAccessDenied,
    SourceAuthorityMismatch,
    SourceIdempotencyConflict,
    SourceIntegrityFailure,
    SourceInvariantViolation,
    SourceNotFound,
    SourceRedacted,
    SourceSchemaTooNew,
    SourceUsageConflict,
)
from work_buddy.sources.migrations import SCHEMA_VERSION, SOURCES_MIGRATIONS
from work_buddy.sources.models import (
    ACTOR_REF_SCHEMA,
    SOURCE_REF_SCHEMA,
    AccessBinding,
    ActorRef,
    AttributionAssertion,
    OriginRef,
    SourceDerivation,
    SourceItem,
    SourceObservation,
    SourceRef,
    SourceRepresentation,
    UsageReservation,
    canonical_json,
    canonical_sha256,
    new_id,
    sha256_bytes,
    utc_now,
    validate_sha256,
)
from work_buddy.storage.migrations import SchemaVersionTooNew
from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)


DEFAULT_INLINE_CONTENT_BYTES = 64 * 1024
DEFAULT_MAX_CONTENT_BYTES = 64 * 1024 * 1024
SQLITE_TIMEOUT_SECONDS = 10.0
SQLITE_BUSY_TIMEOUT_MS = 10_000

logger = logging.getLogger(__name__)

SOURCE_ROLES = frozenset(
    {
        "human_input",
        "conversation_message",
        "imported_file",
        "document_selection",
        "audio",
        "transcript",
        "fetched_passage",
        "agent_output",
        "derived_content",
    }
)
REPRESENTATION_KINDS = frozenset(
    {"raw_bytes", "decoded_text", "multipart", "canonical_text", "lossless_projection"}
)
DERIVATION_RELATIONS = frozenset(
    {
        "quoted_from",
        "transcribed_from",
        "translated_from",
        "summarized_from",
        "revised_from",
        "formatted_from",
        "extracted_from",
    }
)
INPUT_MODES = frozenset(
    {"direct_entry", "paste", "import", "dictation", "automation", "unknown"}
)


@dataclass(frozen=True, slots=True)
class SourcesPaths:
    root: Path
    db: Path
    blobs: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "SourcesPaths":
        resolved = Path(root).expanduser().resolve()
        return cls(root=resolved, db=resolved / "store.db", blobs=resolved / "blobs")


def _json_object(value: Mapping[str, Any] | None) -> str:
    return canonical_json(dict(value or {}))


def _parse_json_object(value: str | None) -> dict[str, Any]:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise SourceIntegrityFailure()
    return parsed


def _require_token(value: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(ch) < 0x20 for ch in value)
    ):
        raise InvalidSourceRequest()
    return value


def _actor_json(actor: ActorRef) -> str:
    return canonical_json(actor.to_dict())


class SourceStore:
    """Machine-level Sources store with a persistent minting authority."""

    def __init__(
        self,
        paths: SourcesPaths,
        authority_id: str,
        *,
        inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
    ) -> None:
        self.paths = paths
        self.authority_id = SourceRef(authority_id, new_id()).authority_id
        if inline_content_bytes < 0 or max_content_bytes < inline_content_bytes:
            raise InvalidSourceRequest()
        self.inline_content_bytes = inline_content_bytes
        self.max_content_bytes = max_content_bytes
        self.blobs = BlobStore(paths.blobs)

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        authority_id: str | None = None,
        inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
    ) -> "SourceStore":
        paths = SourcesPaths.from_root(root)
        if paths.db.exists():
            opened = cls.open(
                paths.root,
                inline_content_bytes=inline_content_bytes,
                max_content_bytes=max_content_bytes,
            )
            if authority_id is not None and opened.authority_id != authority_id:
                raise SourceAuthorityMismatch()
            return opened

        require_source_foundation_writable("sources.create")
        chosen = authority_id or new_id()
        SourceRef(chosen, new_id())
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.blobs.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(paths.db, timeout=SQLITE_TIMEOUT_SECONDS, isolation_level=None)
        try:
            SOURCES_MIGRATIONS.run(conn)
            now = utc_now()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO source_store_info "
                    "(singleton, authority_id, schema_version, created_at) VALUES (1, ?, ?, ?)",
                    (chosen, SCHEMA_VERSION, now),
                )
                conn.execute(
                    "INSERT INTO source_authorities "
                    "(authority_id, custody_kind, created_at) VALUES (?, 'local', ?)",
                    (chosen, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()
        return cls.open(
            paths.root,
            inline_content_bytes=inline_content_bytes,
            max_content_bytes=max_content_bytes,
        )

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
    ) -> "SourceStore":
        paths = SourcesPaths.from_root(root)
        if not paths.db.is_file():
            raise SourceNotFound()
        read_only = source_foundation_read_only()
        probe = sqlite3.connect(
            (
                f"file:{paths.db.resolve()}?mode=ro"
                if read_only
                else str(paths.db)
            ),
            timeout=SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
            uri=read_only,
        )
        try:
            version = int(probe.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise SourceSchemaTooNew()
            if read_only:
                if version != SCHEMA_VERSION:
                    raise SourceIntegrityFailure()
            else:
                try:
                    SOURCES_MIGRATIONS.run(probe)
                except SchemaVersionTooNew as exc:
                    raise SourceSchemaTooNew() from exc
            row = probe.execute(
                "SELECT authority_id, schema_version FROM source_store_info WHERE singleton = 1"
            ).fetchone()
            if row is None or int(row[1]) != SCHEMA_VERSION:
                raise SourceIntegrityFailure()
            authority_id = str(row[0])
        finally:
            probe.close()
        if not read_only:
            paths.blobs.mkdir(parents=True, exist_ok=True)
        store = cls(
            paths,
            authority_id,
            inline_content_bytes=inline_content_bytes,
            max_content_bytes=max_content_bytes,
        )
        # Blob files are staged while holding the SQLite writer lock.  Any
        # file left without a committed representation therefore belongs to
        # an interrupted transaction and is safe to remove at open.
        if not source_foundation_read_only():
            try:
                store.reconcile_blobs(delete_orphans=True)
            except Exception:
                # Keep metadata available if the filesystem cannot be cleaned at
                # this instant.  A later open retries; exact resolution still
                # fails closed if a registered blob is missing or corrupt.
                logger.warning("Sources orphan-blob recovery is deferred", exc_info=True)
        return store

    def connect(self) -> sqlite3.Connection:
        read_only = source_foundation_read_only()
        conn = sqlite3.connect(
            (
                f"file:{self.paths.db.resolve()}?mode=ro"
                if read_only
                else str(self.paths.db)
            ),
            timeout=SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
            uri=read_only,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        require_source_foundation_writable("sources.write")
        conn = self.connect()
        failed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            failed = True
            conn.rollback()
            raise
        finally:
            conn.close()
            if failed:
                # All blob staging is serialized behind BEGIN IMMEDIATE.  Once
                # this failed writer releases the lock, a file absent from the
                # committed representation set cannot belong to an in-flight
                # capture and can be removed without racing another writer.
                try:
                    self.reconcile_blobs(delete_orphans=True)
                except Exception:
                    logger.warning(
                        "Sources rollback orphan-blob recovery is deferred",
                        exc_info=True,
                    )

    def capture_source(
        self,
        *,
        content: bytes | str,
        source_role: str,
        tenant_scope_id: str,
        originating_surface: str,
        media_type: str = "text/plain",
        representation_kind: str | None = None,
        encoding: str | None = None,
        schema_type: str | None = None,
        origin_ref: OriginRef | None = None,
        native_revision: str | None = None,
        fidelity: str = "exact",
        namespace: str | None = None,
        sensitivity_class: str = "private",
        retention_class: str = "durable",
        occurred_at: str | None = None,
        provider_observed_at: str | None = None,
        received_at: str | None = None,
        attributions: Sequence[AttributionAssertion] = (),
        producer: ActorRef | None = None,
    ) -> SourceItem:
        exact, inferred_encoding, inferred_kind = self._normalize_content(
            content, encoding=encoding
        )
        kind = representation_kind or inferred_kind
        with self.write_transaction() as conn:
            staged = self._stage_if_needed(exact, conn=conn)
            return self._capture_source(
                conn,
                content=exact,
                staged_blob=staged,
                source_role=source_role,
                tenant_scope_id=tenant_scope_id,
                originating_surface=originating_surface,
                media_type=media_type,
                representation_kind=kind,
                encoding=inferred_encoding,
                schema_type=schema_type,
                origin_ref=origin_ref,
                native_revision=native_revision,
                fidelity=fidelity,
                namespace=namespace,
                sensitivity_class=sensitivity_class,
                retention_class=retention_class,
                occurred_at=occurred_at,
                provider_observed_at=provider_observed_at,
                received_at=received_at or utc_now(),
                attributions=attributions,
                producer=producer,
            )

    def _normalize_content(
        self, content: bytes | str, *, encoding: str | None
    ) -> tuple[bytes, str | None, str]:
        if isinstance(content, str):
            if encoding not in {None, "utf-8"}:
                raise InvalidSourceRequest()
            exact = content.encode("utf-8")
            return exact, "utf-8", "decoded_text"
        if not isinstance(content, bytes):
            raise InvalidSourceRequest()
        return content, encoding, "raw_bytes"

    def _stage_if_needed(
        self,
        content: bytes,
        *,
        conn: sqlite3.Connection,
    ) -> BlobRecord | None:
        if not conn.in_transaction:
            raise SourceIntegrityFailure()
        if len(content) > self.max_content_bytes:
            from work_buddy.sources.errors import SourceContentTooLarge

            raise SourceContentTooLarge()
        return self.blobs.put(content) if len(content) > self.inline_content_bytes else None

    def _capture_source(
        self,
        conn: sqlite3.Connection,
        *,
        content: bytes,
        staged_blob: BlobRecord | None,
        source_role: str,
        tenant_scope_id: str,
        originating_surface: str,
        media_type: str,
        representation_kind: str,
        encoding: str | None,
        schema_type: str | None,
        origin_ref: OriginRef | None,
        native_revision: str | None,
        fidelity: str,
        namespace: str | None,
        sensitivity_class: str,
        retention_class: str,
        occurred_at: str | None,
        provider_observed_at: str | None,
        received_at: str,
        attributions: Sequence[AttributionAssertion],
        producer: ActorRef | None,
        source_item_id: str | None = None,
        authority_id: str | None = None,
    ) -> SourceItem:
        minting_authority = authority_id or self.authority_id
        if minting_authority != self.authority_id:
            raise SourceAuthorityMismatch()
        if source_role not in SOURCE_ROLES or representation_kind not in REPRESENTATION_KINDS:
            raise InvalidSourceRequest()
        for token in (
            tenant_scope_id,
            originating_surface,
            media_type,
            fidelity,
            sensitivity_class,
            retention_class,
        ):
            _require_token(token)
        item_id = source_item_id or new_id()
        ref = SourceRef(minting_authority, item_id)
        representation_id = new_id()
        digest = sha256_bytes(content)
        now = utc_now()
        char_length: int | None = None
        if encoding:
            try:
                char_length = len(content.decode(encoding))
            except (LookupError, UnicodeDecodeError) as exc:
                raise InvalidSourceRequest() from exc
        blob_digest: str | None = None
        inline: bytes | None = content
        if staged_blob is not None:
            if staged_blob.sha256 != digest or staged_blob.byte_length != len(content):
                raise SourceIntegrityFailure()
            blob_digest = staged_blob.sha256
            inline = None
            conn.execute(
                "INSERT INTO source_blobs "
                "(content_sha256, relative_path, byte_length, ref_count, created_at) "
                "VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(content_sha256) DO UPDATE SET ref_count = ref_count + 1",
                (digest, staged_blob.relative_path, len(content), now),
            )
        conn.execute(
            "INSERT INTO source_items "
            "(authority_id, source_item_id, custodian_authority_id, ref_schema, "
            " primary_representation_id, origin_ref_json, native_revision, source_role, "
            " fidelity, tenant_scope_id, originating_surface, namespace, "
            " sensitivity_class, retention_class, occurred_at, provider_observed_at, "
            " received_at, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ref.authority_id,
                ref.item_id,
                self.authority_id,
                SOURCE_REF_SCHEMA,
                representation_id,
                canonical_json(origin_ref.to_dict()) if origin_ref else None,
                native_revision,
                source_role,
                fidelity,
                tenant_scope_id,
                originating_surface,
                namespace,
                sensitivity_class,
                retention_class,
                occurred_at,
                provider_observed_at,
                received_at,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO source_representations "
            "(representation_id, authority_id, source_item_id, representation_kind, "
            " media_type, schema_type, character_encoding, content_sha256, byte_length, "
            " character_length, inline_content, blob_sha256, is_primary, "
            " producer_ref_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                representation_id,
                ref.authority_id,
                ref.item_id,
                representation_kind,
                media_type,
                schema_type,
                encoding,
                digest,
                len(content),
                char_length,
                inline,
                blob_digest,
                _actor_json(producer) if producer else None,
                now,
            ),
        )
        for assertion in attributions:
            self._add_attribution(conn, ref, assertion, representation_id=representation_id)
        self._add_observation(
            conn,
            ref,
            kind="captured",
            resolver_id="sources-retained",
            resolver_version="1",
            status="ok",
            native_revision=native_revision,
            native_content_sha256=digest,
            retained_sha256=digest,
            observed_at=provider_observed_at or now,
        )
        return self._get_item(conn, ref)

    def get_item(self, source_ref: SourceRef) -> SourceItem | None:
        conn = self.connect()
        try:
            return self._get_item(conn, source_ref, required=False)
        finally:
            conn.close()

    def _get_item(
        self,
        conn: sqlite3.Connection,
        source_ref: SourceRef,
        *,
        required: bool = True,
    ) -> SourceItem | None:
        row = conn.execute(
            "SELECT * FROM source_items WHERE authority_id = ? AND source_item_id = ?",
            (source_ref.authority_id, source_ref.item_id),
        ).fetchone()
        if row is None:
            if required:
                raise SourceNotFound()
            return None
        origin = (
            OriginRef.from_dict(_parse_json_object(row["origin_ref_json"]))
            if row["origin_ref_json"]
            else None
        )
        return SourceItem(
            source_ref=source_ref,
            custodian_authority_id=str(row["custodian_authority_id"]),
            primary_representation_id=str(row["primary_representation_id"]),
            origin_ref=origin,
            native_revision=row["native_revision"],
            source_role=str(row["source_role"]),
            fidelity=str(row["fidelity"]),
            tenant_scope_id=str(row["tenant_scope_id"]),
            originating_surface=str(row["originating_surface"]),
            namespace=row["namespace"],
            sensitivity_class=str(row["sensitivity_class"]),
            retention_class=str(row["retention_class"]),
            occurred_at=row["occurred_at"],
            received_at=str(row["received_at"]),
            committed_at=str(row["committed_at"]),
            lifecycle_state=str(row["lifecycle_state"]),
            redaction_epoch=int(row["redaction_epoch"]),
            redaction_event_id=row["redaction_event_id"],
        )

    def get_representation(self, representation_id: str) -> SourceRepresentation | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM source_representations WHERE representation_id = ?",
                (representation_id,),
            ).fetchone()
            return self._representation_from_row(row) if row else None
        finally:
            conn.close()

    @staticmethod
    def _representation_from_row(row: sqlite3.Row) -> SourceRepresentation:
        return SourceRepresentation(
            representation_id=str(row["representation_id"]),
            source_ref=SourceRef(str(row["authority_id"]), str(row["source_item_id"])),
            kind=str(row["representation_kind"]),
            media_type=str(row["media_type"]),
            content_sha256=str(row["content_sha256"]),
            byte_length=int(row["byte_length"]),
            character_length=(
                int(row["character_length"]) if row["character_length"] is not None else None
            ),
            encoding=row["character_encoding"],
            schema_type=row["schema_type"],
            inline=row["inline_content"] is not None,
            derivation_relation=row["derivation_relation"],
            created_at=str(row["created_at"]),
        )

    def _representation_row(
        self,
        conn: sqlite3.Connection,
        source_ref: SourceRef,
        representation_id: str | None = None,
    ) -> sqlite3.Row:
        item = self._get_item(conn, source_ref)
        assert item is not None
        selected = representation_id or item.primary_representation_id
        row = conn.execute(
            "SELECT * FROM source_representations "
            "WHERE representation_id = ? AND authority_id = ? AND source_item_id = ?",
            (selected, source_ref.authority_id, source_ref.item_id),
        ).fetchone()
        if row is None:
            raise SourceNotFound()
        return row

    def _read_representation_row(self, row: sqlite3.Row) -> bytes:
        if row["redacted_at"] is not None:
            raise SourceRedacted()
        if row["inline_content"] is not None:
            content = bytes(row["inline_content"])
        elif row["blob_sha256"] is not None:
            content = self.blobs.read(
                str(row["blob_sha256"]), expected_length=int(row["byte_length"])
            )
        else:
            raise SourceIntegrityFailure()
        if len(content) != int(row["byte_length"]):
            raise SourceIntegrityFailure()
        if sha256_bytes(content) != str(row["content_sha256"]):
            raise SourceIntegrityFailure()
        return content

    def add_attribution(
        self,
        source_ref: SourceRef,
        assertion: AttributionAssertion,
        *,
        representation_id: str | None = None,
    ) -> str:
        with self.write_transaction() as conn:
            return self._add_attribution(
                conn, source_ref, assertion, representation_id=representation_id
            )

    def _add_attribution(
        self,
        conn: sqlite3.Connection,
        source_ref: SourceRef,
        assertion: AttributionAssertion,
        *,
        representation_id: str | None,
    ) -> str:
        self._get_item(conn, source_ref)
        if representation_id is not None:
            self._representation_row(conn, source_ref, representation_id)
        if assertion.supersedes_id:
            prior = conn.execute(
                "SELECT authority_id, source_item_id, role FROM source_attributions "
                "WHERE attribution_id = ?",
                (assertion.supersedes_id,),
            ).fetchone()
            if (
                prior is None
                or prior["authority_id"] != source_ref.authority_id
                or prior["source_item_id"] != source_ref.item_id
                or prior["role"] != assertion.role
            ):
                raise SourceInvariantViolation()
        attribution_id = new_id()
        now = utc_now()
        conn.execute(
            "INSERT INTO source_attributions "
            "(attribution_id, authority_id, source_item_id, representation_id, role, "
            " actor_ref_json, attribution_state, basis, assurance, selector_json, "
            " asserted_by_json, observed_at, supersedes_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attribution_id,
                source_ref.authority_id,
                source_ref.item_id,
                representation_id,
                assertion.role,
                _actor_json(assertion.actor) if assertion.actor else None,
                assertion.state,
                assertion.basis,
                assertion.assurance,
                _json_object(assertion.selector) if assertion.selector is not None else None,
                _actor_json(assertion.asserted_by) if assertion.asserted_by else None,
                assertion.observed_at or now,
                assertion.supersedes_id,
                now,
            ),
        )
        return attribution_id

    def current_attributions(
        self, conn: sqlite3.Connection, source_ref: SourceRef
    ) -> tuple[AttributionAssertion, ...]:
        rows = conn.execute(
            "SELECT a.* FROM source_attributions a "
            "WHERE a.authority_id = ? AND a.source_item_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM source_attributions successor "
            "                WHERE successor.supersedes_id = a.attribution_id) "
            "ORDER BY a.created_at, a.attribution_id",
            (source_ref.authority_id, source_ref.item_id),
        ).fetchall()
        values: list[AttributionAssertion] = []
        for row in rows:
            actor = (
                ActorRef.from_dict(_parse_json_object(row["actor_ref_json"]))
                if row["actor_ref_json"]
                else None
            )
            asserting = (
                ActorRef.from_dict(_parse_json_object(row["asserted_by_json"]))
                if row["asserted_by_json"]
                else None
            )
            values.append(
                AttributionAssertion(
                    role=str(row["role"]),
                    actor=actor,
                    state=str(row["attribution_state"]),
                    basis=str(row["basis"]),
                    assurance=str(row["assurance"]),
                    asserted_by=asserting,
                    selector=(
                        _parse_json_object(row["selector_json"])
                        if row["selector_json"]
                        else None
                    ),
                    observed_at=str(row["observed_at"]),
                    supersedes_id=row["supersedes_id"],
                )
            )
        return tuple(values)

    def add_observation(self, source_ref: SourceRef, **kwargs: Any) -> SourceObservation:
        with self.write_transaction() as conn:
            return self._add_observation(conn, source_ref, **kwargs)

    def _add_observation(
        self,
        conn: sqlite3.Connection,
        source_ref: SourceRef,
        *,
        kind: str,
        resolver_id: str,
        resolver_version: str,
        status: str,
        observed_at: str | None = None,
        native_revision: str | None = None,
        native_content_sha256: str | None = None,
        retained_sha256: str | None = None,
        error_code: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceObservation:
        self._get_item(conn, source_ref)
        valid_kinds = {
            "captured",
            "resolved",
            "snapshot_integrity_ok",
            "snapshot_integrity_failed",
            "origin_unchanged",
            "origin_changed",
            "origin_unavailable",
            "identity_mismatch",
            "redacted",
            "resolver_failed",
        }
        if kind not in valid_kinds:
            raise InvalidSourceRequest()
        for value in (resolver_id, resolver_version, status):
            _require_token(value)
        if error_code is not None:
            _require_token(error_code, maximum=128)
        observation_id = new_id()
        at = observed_at or utc_now()
        conn.execute(
            "INSERT INTO source_observations "
            "(observation_id, authority_id, source_item_id, observation_kind, "
            " resolver_id, resolver_version, observed_at, native_revision, "
            " native_content_sha256, retained_sha256, status, error_code, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observation_id,
                source_ref.authority_id,
                source_ref.item_id,
                kind,
                resolver_id,
                resolver_version,
                at,
                native_revision,
                native_content_sha256,
                retained_sha256,
                status,
                error_code,
                _json_object(metadata),
            ),
        )
        return SourceObservation(
            observation_id=observation_id,
            source_ref=source_ref,
            kind=kind,
            resolver_id=resolver_id,
            resolver_version=resolver_version,
            observed_at=at,
            status=status,
            native_revision=native_revision,
            content_sha256=retained_sha256,
            error_code=error_code,
        )

    def add_derivation(
        self,
        *,
        derived_ref: SourceRef,
        input_ref: SourceRef,
        relation: str,
        producer: ActorRef,
        activity_id: str,
        selector: Mapping[str, Any] | None = None,
        method: Mapping[str, Any] | None = None,
        fidelity: str = "derived",
    ) -> SourceDerivation:
        if relation not in DERIVATION_RELATIONS:
            raise InvalidSourceRequest()
        with self.write_transaction() as conn:
            self._get_item(conn, derived_ref)
            self._get_item(conn, input_ref)
            derivation_id = new_id()
            now = utc_now()
            try:
                conn.execute(
                    "INSERT INTO source_derivations "
                    "(derivation_id, derived_authority_id, derived_item_id, "
                    " input_authority_id, input_item_id, relation, producer_ref_json, "
                    " activity_id, selector_json, method_json, fidelity, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        derivation_id,
                        derived_ref.authority_id,
                        derived_ref.item_id,
                        input_ref.authority_id,
                        input_ref.item_id,
                        relation,
                        _actor_json(producer),
                        _require_token(activity_id),
                        _json_object(selector) if selector is not None else None,
                        _json_object(method),
                        _require_token(fidelity),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SourceInvariantViolation() from exc
        return SourceDerivation(
            derivation_id=derivation_id,
            derived_ref=derived_ref,
            input_ref=input_ref,
            relation=relation,
            producer=producer,
            activity_id=activity_id,
            selector=selector,
            method=dict(method or {}),
            fidelity=fidelity,
            created_at=now,
        )

    def grant_access(
        self,
        *,
        source_ref: SourceRef,
        principal: ActorRef,
        purpose: str,
        access_mode: str,
        authorization_fingerprint: str,
        scope: Mapping[str, str] | None = None,
        trusted_service_id: str | None = None,
        external_recipient: str | None = None,
        model_id: str | None = None,
        egress_class: str | None = None,
        content_boundary: Mapping[str, Any] | None = None,
        gesture_receipt_id: str | None = None,
        expires_at: str | None = None,
        binding_id: str | None = None,
    ) -> AccessBinding:
        with self.write_transaction() as conn:
            return self._grant_access(
                conn,
                source_ref=source_ref,
                principal=principal,
                purpose=purpose,
                access_mode=access_mode,
                authorization_fingerprint=authorization_fingerprint,
                scope=scope,
                trusted_service_id=trusted_service_id,
                external_recipient=external_recipient,
                model_id=model_id,
                egress_class=egress_class,
                content_boundary=content_boundary,
                gesture_receipt_id=gesture_receipt_id,
                expires_at=expires_at,
                binding_id=binding_id,
            )

    def _grant_access(
        self,
        conn: sqlite3.Connection,
        *,
        source_ref: SourceRef,
        principal: ActorRef,
        purpose: str,
        access_mode: str,
        authorization_fingerprint: str,
        scope: Mapping[str, str] | None = None,
        trusted_service_id: str | None = None,
        external_recipient: str | None = None,
        model_id: str | None = None,
        egress_class: str | None = None,
        content_boundary: Mapping[str, Any] | None = None,
        gesture_receipt_id: str | None = None,
        expires_at: str | None = None,
        binding_id: str | None = None,
    ) -> AccessBinding:
        validate_sha256(authorization_fingerprint)
        if access_mode not in {"metadata", "content"}:
            raise InvalidSourceRequest()
        _require_token(purpose)
        self._get_item(conn, source_ref)
        identifier = binding_id or new_id()
        now = utc_now()
        conn.execute(
            "INSERT INTO source_access_bindings "
            "(binding_id, authority_id, source_item_id, principal_ref_json, "
            " trusted_service_id, purpose, access_mode, scope_json, "
            " external_recipient, model_id, egress_class, content_boundary_json, "
            " authorization_fingerprint, gesture_receipt_id, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                source_ref.authority_id,
                source_ref.item_id,
                _actor_json(principal),
                trusted_service_id,
                purpose,
                access_mode,
                _json_object(scope),
                external_recipient,
                model_id,
                egress_class,
                _json_object(content_boundary) if content_boundary is not None else None,
                authorization_fingerprint,
                gesture_receipt_id,
                expires_at,
                now,
            ),
        )
        return AccessBinding(
            binding_id=identifier,
            source_ref=source_ref,
            principal=principal,
            purpose=purpose,
            access_mode=access_mode,
            scope=dict(scope or {}),
            external_recipient=external_recipient,
            model_id=model_id,
            egress_class=egress_class,
            content_boundary=content_boundary,
            authorization_fingerprint=authorization_fingerprint,
            expires_at=expires_at,
            revoked_at=None,
            created_at=now,
        )

    def revoke_access(self, binding_id: str, *, at: str | None = None) -> None:
        with self.write_transaction() as conn:
            row = conn.execute(
                "SELECT revoked_at FROM source_access_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise SourceNotFound()
            if row["revoked_at"] is None:
                conn.execute(
                    "UPDATE source_access_bindings SET revoked_at = ? WHERE binding_id = ?",
                    (at or utc_now(), binding_id),
                )

    def _find_access_binding(
        self,
        conn: sqlite3.Connection,
        *,
        source_ref: SourceRef,
        principal: ActorRef,
        purpose: str,
        access_mode: str,
        at: str,
        external_recipient: str | None = None,
        model_id: str | None = None,
        egress_class: str | None = None,
        consumer_domain: str | None = None,
        use_kind: str | None = None,
    ) -> sqlite3.Row:
        rows = conn.execute(
            "SELECT * FROM source_access_bindings "
            "WHERE authority_id = ? AND source_item_id = ? "
            "AND principal_ref_json = ? AND purpose = ? AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at DESC",
            (
                source_ref.authority_id,
                source_ref.item_id,
                _actor_json(principal),
                purpose,
                at,
            ),
        ).fetchall()
        for row in rows:
            if access_mode == "content" and row["access_mode"] != "content":
                continue
            if external_recipient is not None and row["external_recipient"] != external_recipient:
                continue
            if model_id is not None and row["model_id"] != model_id:
                continue
            if egress_class is not None and row["egress_class"] != egress_class:
                continue
            scope = _parse_json_object(row["scope_json"])
            if (
                consumer_domain is not None
                and "consumer_domain" in scope
                and scope["consumer_domain"] != consumer_domain
            ):
                continue
            if (
                use_kind is not None
                and "use_kind" in scope
                and scope["use_kind"] != use_kind
            ):
                continue
            return row
        raise SourceAccessDenied()

    def reserve_usage(
        self,
        *,
        source_ref: SourceRef,
        representation_id: str,
        principal: ActorRef,
        purpose: str,
        consumer_domain: str,
        consumer_id: str,
        use_kind: str,
        disclosure_kind: str,
        redaction_policy: str,
        selector: Mapping[str, Any] | None = None,
        external_recipient: str | None = None,
        model_id: str | None = None,
        egress_class: str | None = None,
        at: str | None = None,
    ) -> UsageReservation:
        with self.write_transaction() as conn:
            return self._reserve_usage(
                conn,
                source_ref=source_ref,
                representation_id=representation_id,
                principal=principal,
                purpose=purpose,
                consumer_domain=consumer_domain,
                consumer_id=consumer_id,
                use_kind=use_kind,
                disclosure_kind=disclosure_kind,
                redaction_policy=redaction_policy,
                selector=selector,
                external_recipient=external_recipient,
                model_id=model_id,
                egress_class=egress_class,
                at=at,
            )

    def _reserve_usage(
        self,
        conn: sqlite3.Connection,
        *,
        source_ref: SourceRef,
        representation_id: str,
        principal: ActorRef,
        purpose: str,
        consumer_domain: str,
        consumer_id: str,
        use_kind: str,
        disclosure_kind: str,
        redaction_policy: str,
        selector: Mapping[str, Any] | None = None,
        external_recipient: str | None = None,
        model_id: str | None = None,
        egress_class: str | None = None,
        at: str | None = None,
    ) -> UsageReservation:
        now = at or utc_now()
        request = {
            "source_ref": source_ref.to_dict(),
            "representation_id": representation_id,
            "principal": principal.to_dict(),
            "purpose": purpose,
            "consumer_domain": consumer_domain,
            "consumer_id": consumer_id,
            "use_kind": use_kind,
            "disclosure_kind": disclosure_kind,
            "redaction_policy": redaction_policy,
            "selector": selector,
            "external_recipient": external_recipient,
            "model_id": model_id,
            "egress_class": egress_class,
        }
        request_hash = canonical_sha256(request)
        existing = conn.execute(
            "SELECT * FROM source_usage_intents "
            "WHERE consumer_domain = ? AND consumer_id = ? AND use_kind = ?",
            (consumer_domain, consumer_id, use_kind),
        ).fetchone()
        if existing is not None:
            if existing["request_sha256"] != request_hash:
                raise SourceUsageConflict()
            return self._usage_from_row(existing)
        item = self._get_item(conn, source_ref)
        assert item is not None
        if item.lifecycle_state == "redacted":
            raise SourceRedacted()
        self._representation_row(conn, source_ref, representation_id)
        binding = self._find_access_binding(
            conn,
            source_ref=source_ref,
            principal=principal,
            purpose=purpose,
            access_mode="content" if disclosure_kind != "metadata_only" else "metadata",
            at=now,
            external_recipient=external_recipient,
            model_id=model_id,
            egress_class=egress_class,
            consumer_domain=consumer_domain,
            use_kind=use_kind,
        )
        usage_id = new_id()
        conn.execute(
            "INSERT INTO source_usage_intents "
            "(usage_id, authority_id, source_item_id, representation_id, selector_json, "
            " principal_ref_json, purpose, consumer_domain, consumer_id, use_kind, "
            " disclosure_kind, redaction_policy, access_binding_id, "
            " bound_redaction_epoch, request_sha256, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)",
            (
                usage_id,
                source_ref.authority_id,
                source_ref.item_id,
                representation_id,
                _json_object(selector) if selector is not None else None,
                _actor_json(principal),
                purpose,
                consumer_domain,
                consumer_id,
                use_kind,
                disclosure_kind,
                redaction_policy,
                binding["binding_id"],
                item.redaction_epoch,
                request_hash,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM source_usage_intents WHERE usage_id = ?", (usage_id,)
        ).fetchone()
        assert row is not None
        return self._usage_from_row(row)

    @staticmethod
    def _usage_from_row(row: sqlite3.Row) -> UsageReservation:
        return UsageReservation(
            usage_id=str(row["usage_id"]),
            source_ref=SourceRef(str(row["authority_id"]), str(row["source_item_id"])),
            representation_id=str(row["representation_id"]),
            redaction_epoch=int(row["bound_redaction_epoch"]),
            status=str(row["status"]),
            request_sha256=str(row["request_sha256"]),
            created_at=str(row["created_at"]),
        )

    def precommit_recheck_usage(self, usage_id: str) -> UsageReservation:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT u.*, i.lifecycle_state, i.redaction_epoch current_epoch "
                "FROM source_usage_intents u JOIN source_items i "
                "ON i.authority_id = u.authority_id AND i.source_item_id = u.source_item_id "
                "WHERE u.usage_id = ?",
                (usage_id,),
            ).fetchone()
            if row is None:
                raise SourceNotFound()
            if (
                row["status"] != "reserved"
                or row["lifecycle_state"] != "active"
                or int(row["bound_redaction_epoch"]) != int(row["current_epoch"])
            ):
                raise SourceUsageConflict()
            return self._usage_from_row(row)
        finally:
            conn.close()

    def acknowledge_usage(self, usage_id: str, *, at: str | None = None) -> UsageReservation:
        with self.write_transaction() as conn:
            row = conn.execute(
                "SELECT u.*, i.lifecycle_state FROM source_usage_intents u "
                "JOIN source_items i ON i.authority_id = u.authority_id "
                "AND i.source_item_id = u.source_item_id WHERE u.usage_id = ?",
                (usage_id,),
            ).fetchone()
            if row is None:
                raise SourceNotFound()
            if row["status"] == "released":
                raise SourceUsageConflict()
            if row["status"] == "reserved":
                maintenance = (
                    "pending_redaction" if row["lifecycle_state"] == "redacted" else "clean"
                )
                conn.execute(
                    "UPDATE source_usage_intents SET status = 'acknowledged', "
                    "acknowledged_at = ?, maintenance_state = ? WHERE usage_id = ?",
                    (at or utc_now(), maintenance, usage_id),
                )
            updated = conn.execute(
                "SELECT * FROM source_usage_intents WHERE usage_id = ?", (usage_id,)
            ).fetchone()
            assert updated is not None
            return self._usage_from_row(updated)

    def release_usage(self, usage_id: str, *, at: str | None = None) -> UsageReservation:
        with self.write_transaction() as conn:
            return self._release_usage(conn, usage_id, at=at)

    def release_usage_if_source_active(
        self, usage_id: str, *, at: str | None = None
    ) -> UsageReservation | None:
        """Release a superseded usage only when no redaction raced it.

        The lifecycle check and release share one Sources writer transaction.
        A returned ``None`` means redaction won the race, so the caller must
        leave both the usage and its maintenance effect pending for review.
        """

        with self.write_transaction() as conn:
            row = conn.execute(
                "SELECT u.*,i.lifecycle_state FROM source_usage_intents u "
                "JOIN source_items i ON i.authority_id=u.authority_id "
                "AND i.source_item_id=u.source_item_id WHERE u.usage_id=?",
                (usage_id,),
            ).fetchone()
            if row is None:
                raise SourceNotFound()
            if row["lifecycle_state"] != "active":
                return None
            return self._release_usage(conn, usage_id, at=at, row=row)

    def _release_usage(
        self,
        conn: sqlite3.Connection,
        usage_id: str,
        *,
        at: str | None = None,
        row: sqlite3.Row | None = None,
    ) -> UsageReservation:
        if row is None:
            row = conn.execute(
                "SELECT * FROM source_usage_intents WHERE usage_id = ?", (usage_id,)
            ).fetchone()
        if row is None:
            raise SourceNotFound()
        if row["status"] != "released":
            conn.execute(
                "UPDATE source_usage_intents SET status = 'released', released_at = ?, "
                "maintenance_state = 'completed' WHERE usage_id = ?",
                (at or utc_now(), usage_id),
            )
        item = conn.execute(
            "SELECT lifecycle_state,redaction_event_id FROM source_items "
            "WHERE authority_id=? AND source_item_id=?",
            (row["authority_id"], row["source_item_id"]),
        ).fetchone()
        if (
            item is not None
            and item["lifecycle_state"] == "redacted"
            and item["redaction_event_id"] is not None
        ):
            unfinished = conn.execute(
                "SELECT COUNT(*) FROM source_usage_intents "
                "WHERE authority_id=? AND source_item_id=? "
                "AND (status != 'released' OR maintenance_state != 'completed')",
                (row["authority_id"], row["source_item_id"]),
            ).fetchone()[0]
            if int(unfinished) == 0:
                conn.execute(
                    "UPDATE source_redaction_events SET managed_copy_state='complete' "
                    "WHERE redaction_event_id=?",
                    (item["redaction_event_id"],),
                )
        updated = conn.execute(
            "SELECT * FROM source_usage_intents WHERE usage_id = ?", (usage_id,)
        ).fetchone()
        assert updated is not None
        return self._usage_from_row(updated)

    def idempotency_result(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_scope_id: str,
        issuer: ActorRef,
        principal: ActorRef,
        client_mutation_id: str,
        request_sha256: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT request_sha256, result_json FROM source_idempotency "
            "WHERE authority_id = ? AND tenant_scope_id = ? AND issuer_ref_json = ? "
            "AND principal_ref_json = ? AND client_mutation_id = ?",
            (
                self.authority_id,
                tenant_scope_id,
                _actor_json(issuer),
                _actor_json(principal),
                client_mutation_id,
            ),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise SourceIdempotencyConflict()
        return _parse_json_object(row["result_json"])

    def record_idempotency(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_scope_id: str,
        issuer: ActorRef,
        principal: ActorRef,
        client_mutation_id: str,
        request_sha256: str,
        result: Mapping[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO source_idempotency "
            "(authority_id, tenant_scope_id, issuer_ref_json, principal_ref_json, "
            " client_mutation_id, request_sha256, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.authority_id,
                tenant_scope_id,
                _actor_json(issuer),
                _actor_json(principal),
                client_mutation_id,
                request_sha256,
                _json_object(result),
                utc_now(),
            ),
        )

    def reconcile_blobs(self, *, delete_orphans: bool = False) -> dict[str, tuple[str, ...]]:
        if delete_orphans:
            require_source_foundation_writable("sources.blob_cleanup")
        conn = self.connect()
        try:
            if delete_orphans:
                # Serializes inspection + deletion with every staged writer.
                # Writers create the file only after acquiring the same lock.
                conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT content_sha256, ref_count FROM source_blobs"
            ).fetchall()
            registered = {str(row["content_sha256"]): int(row["ref_count"]) for row in rows}
            referenced_rows = conn.execute(
                "SELECT blob_sha256, COUNT(*) count FROM source_representations "
                "WHERE blob_sha256 IS NOT NULL GROUP BY blob_sha256"
            ).fetchall()
            referenced = {str(row["blob_sha256"]): int(row["count"]) for row in referenced_rows}
            files = self.blobs.digests()
            missing = tuple(sorted(d for d in referenced if d not in files))
            orphan_files = tuple(sorted(files - set(referenced)))
            count_mismatch = tuple(
                sorted(d for d, count in referenced.items() if registered.get(d) != count)
            )
            if delete_orphans:
                for digest in orphan_files:
                    self.blobs.delete(digest)
                conn.commit()
            return {
                "missing": missing,
                "orphan_files": orphan_files,
                "count_mismatch": count_mismatch,
            }
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
