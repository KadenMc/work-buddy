"""Connection and integrity boundary for the Contracts SQLite authority."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from work_buddy.contracts_domain.migrations import CONTRACT_MIGRATIONS
from work_buddy.installed_authority import require_domain_store_open


class ContractStoreError(RuntimeError):
    """The Contracts authority cannot be opened or validated safely."""


def default_db_path() -> Path:
    from work_buddy.paths import resolve

    return resolve("db/contracts")


class ContractStore:
    """A per-domain SQLite store with explicit transaction ownership."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    @classmethod
    def default(cls) -> "ContractStore":
        return cls(default_db_path())

    @classmethod
    def create(cls, path: str | Path) -> "ContractStore":
        store = cls(path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        connection = store.connect()
        connection.close()
        return store

    def exists(self) -> bool:
        return self.path.is_file()

    def connect(self, *, migrate: bool = True) -> sqlite3.Connection:
        require_domain_store_open("contracts", self.path)
        if migrate:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise ContractStoreError("contracts database is unavailable")
        connection = sqlite3.connect(
            str(self.path),
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        if migrate:
            connection.execute("PRAGMA journal_mode=WAL")
            CONTRACT_MIGRATIONS.run(connection)
            connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

    @contextlib.contextmanager
    def write_transaction(
        self, connection: sqlite3.Connection | None = None
    ) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
            return
        owned = self.connect()
        owned.execute("BEGIN IMMEDIATE")
        try:
            yield owned
            owned.execute("COMMIT")
        except Exception:
            if owned.in_transaction:
                owned.execute("ROLLBACK")
            raise
        finally:
            owned.close()

    def authority(self) -> dict[str, Any]:
        with self.read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM contract_authority WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise ContractStoreError("contracts authority row is missing")
            return dict(row)

    def is_native_authority(self) -> bool:
        return self.exists() and self.authority()["state"] == "native"

    @staticmethod
    def validate_connection(connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        integrity = [tuple(row) for row in connection.execute("PRAGMA integrity_check")]
        if integrity != [("ok",)]:
            raise ContractStoreError("contracts database failed integrity_check")
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign_keys:
            raise ContractStoreError("contracts database has foreign-key violations")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != CONTRACT_MIGRATIONS.target_version:
            raise ContractStoreError(
                f"contracts database schema is v{version}; expected "
                f"v{CONTRACT_MIGRATIONS.target_version}"
            )
        authority = connection.execute(
            "SELECT * FROM contract_authority WHERE singleton=1"
        ).fetchone()
        if authority is None:
            raise ContractStoreError("contracts authority row is missing")
        if authority["state"] == "native":
            cohort = connection.execute(
                "SELECT * FROM contract_import_cohorts WHERE cohort_id=?",
                (authority["sealed_cohort_id"],),
            ).fetchone()
            seal = connection.execute(
                "SELECT * FROM contract_import_seals WHERE cohort_id=?",
                (authority["sealed_cohort_id"],),
            ).fetchone()
            if (
                cohort is None
                or cohort["state"] != "sealed"
                or seal is None
                or seal["inventory_sha256"] != cohort["inventory_sha256"]
                or seal["coordinator_decision_id"]
                != authority["coordinator_decision_id"]
                or seal["coordinator_decision_sha256"]
                != authority["coordinator_decision_sha256"]
            ):
                raise ContractStoreError(
                    "native contracts authority lacks a matching sealed cohort"
                )
        stale = connection.execute(
            """
            SELECT c.contract_id
            FROM contracts c
            LEFT JOIN contract_revisions r
              ON r.contract_id=c.contract_id AND r.revision=c.current_revision
            WHERE r.revision_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if stale is not None:
            raise ContractStoreError("contract current revision is not retained")
        missing_outbox = connection.execute(
            """
            SELECT c.contract_id
            FROM contracts c
            LEFT JOIN contract_search_outbox o
              ON o.contract_id=c.contract_id AND o.revision=c.current_revision
            WHERE o.event_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if missing_outbox is not None:
            raise ContractStoreError("contract current revision lacks a search outbox event")

        bad_receipt = connection.execute(
            """
            SELECT mr.receipt_id
            FROM contract_mutation_receipts mr
            JOIN contract_revisions r
              ON r.contract_id=mr.contract_id AND r.revision=mr.revision
            WHERE mr.result_sha256 != r.snapshot_sha256
            LIMIT 1
            """
        ).fetchone()
        if bad_receipt is not None:
            raise ContractStoreError("contract mutation receipt digest mismatch")

        bad_binding = connection.execute(
            """
            SELECT br.contract_id
            FROM contract_body_roles br
            JOIN contract_document_bindings b
              ON b.binding_id=br.current_document_binding_id
            WHERE br.body_mode='document'
              AND (b.lifecycle!='current'
                OR b.interaction_contract_id!=br.interaction_contract_id
                OR b.interaction_contract_version!=br.interaction_contract_version)
            LIMIT 1
            """
        ).fetchone()
        if bad_binding is not None:
            raise ContractStoreError("current contract document binding is inconsistent")

        def digest_json(raw: str) -> str:
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ContractStoreError("contracts database contains invalid JSON") from exc
            canonical = json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        for row in connection.execute(
            "SELECT snapshot_json,snapshot_sha256 FROM contract_revisions"
        ):
            if digest_json(row["snapshot_json"]) != row["snapshot_sha256"]:
                raise ContractStoreError("contract revision digest mismatch")
        for row in connection.execute(
            "SELECT payload_json,content_sha256 FROM contract_search_outbox"
        ):
            if digest_json(row["payload_json"]) != row["content_sha256"]:
                raise ContractStoreError("contract search outbox digest mismatch")
        for row in connection.execute(
            "SELECT result_json,result_sha256 FROM contract_import_receipts"
        ):
            if digest_json(row["result_json"]) != row["result_sha256"]:
                raise ContractStoreError("contract import receipt digest mismatch")
        for row in connection.execute(
            "SELECT frozen_bytes,source_sha256,byte_length FROM contract_import_inventory"
        ):
            frozen = bytes(row["frozen_bytes"])
            if (
                len(frozen) != row["byte_length"]
                or hashlib.sha256(frozen).hexdigest() != row["source_sha256"]
            ):
                raise ContractStoreError("contract import frozen input digest mismatch")
        for row in connection.execute(
            "SELECT record_json,record_sha256 FROM contract_import_stage"
        ):
            if digest_json(row["record_json"]) != row["record_sha256"]:
                raise ContractStoreError("contract import staged record digest mismatch")

        for cohort in connection.execute("SELECT * FROM contract_import_cohorts"):
            inventory = connection.execute(
                "SELECT source_key,source_sha256,byte_length,disposition "
                "FROM contract_import_inventory WHERE cohort_id=? ORDER BY source_key",
                (cohort["cohort_id"],),
            ).fetchall()
            descriptor = [
                {
                    "source_key": row["source_key"],
                    "source_sha256": row["source_sha256"],
                    "byte_length": row["byte_length"],
                }
                for row in inventory
            ]
            descriptor_json = json.dumps(
                descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            if hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest() != cohort[
                "inventory_sha256"
            ]:
                raise ContractStoreError("contract import inventory manifest mismatch")
            counts = {
                "accepted": sum(row["disposition"] == "accepted" for row in inventory),
                "quarantined": sum(
                    row["disposition"] == "quarantined" for row in inventory
                ),
                "ignored": sum(row["disposition"] == "ignored" for row in inventory),
            }
            if (
                len(inventory) != cohort["item_count"]
                or counts["accepted"] != cohort["accepted_count"]
                or counts["quarantined"] != cohort["quarantined_count"]
                or counts["ignored"] != cohort["ignored_count"]
            ):
                raise ContractStoreError("contract import cohort counts are inconsistent")
            staged = connection.execute(
                "SELECT COUNT(*) FROM contract_import_stage WHERE cohort_id=?",
                (cohort["cohort_id"],),
            ).fetchone()[0]
            if staged != counts["accepted"]:
                raise ContractStoreError("contract import stage does not match inventory")

    def validate(self) -> None:
        connection = self.connect(migrate=False)
        try:
            self.validate_connection(connection)
        finally:
            connection.close()


__all__ = ["ContractStore", "ContractStoreError", "default_db_path"]
