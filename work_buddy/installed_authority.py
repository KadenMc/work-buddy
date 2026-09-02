"""Installation-level latch for irreversible native-domain cutovers.

The authority row inside a domain database cannot protect against that
database being deleted or replaced.  This small, content-free sibling ledger
records that an installation has crossed the seal boundary, independently of
the database whose authority it protects.  Once a row exists, opening the
domain store is allowed only when the exact bound database still proves its
sealed native authority.

Seal publication is deliberately two phase across the two SQLite files:
``prepare_domain_seal`` writes ``sealing`` before the domain authority update;
``confirm_domain_seal`` runs only after that update commits.  A crash in the
middle therefore leaves a durable fail-closed latch rather than reopening a
legacy Markdown path.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Final, Iterator, Mapping


SUPPORTED_DOMAINS: Final[frozenset[str]] = frozenset(
    {"journal", "projects", "contracts", "personal_knowledge"}
)
LEDGER_FILENAME: Final[str] = "installed_authority.db"
LEDGER_SCHEMA_VERSION: Final[int] = 1
RESTORE_REBIND_PLAN_SCHEMA: Final[str] = (
    "wb.installed-authority-restore-rebind-plan/v1"
)
RESTORE_REBIND_RECEIPT_SCHEMA: Final[str] = (
    "wb.installed-authority-restore-rebind-receipt/v1"
)
_SEALED_STATES: Final[frozenset[str]] = frozenset(
    {"sealing", "sealed", "released", "recovery_fenced"}
)
_RESTORE_REBINDABLE_STATES: Final[frozenset[str]] = frozenset(
    {"sealed", "released", "recovery_fenced"}
)
_INCOMPLETE_SEAL_RECOVERY: ContextVar[
    tuple[str, str, str, str] | None
] = ContextVar("incomplete_installed_authority_seal_recovery", default=None)


class InstalledAuthorityError(RuntimeError):
    """An installed cutover latch cannot prove its bound native authority."""


@dataclass(frozen=True, slots=True)
class InstalledAuthorityStatus:
    domain: str
    state: str
    cohort_id: str
    authority_db_path_sha256: str
    authority_mode: str
    revision: int
    ledger_path: Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _path_sha256(path: str | Path) -> str:
    normalized = str(_resolved(path)).replace("\\", "/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def ledger_path_for(authority_db_path: str | Path) -> Path:
    """Return the installation ledger beside an exact domain database.

    Production domain databases share ``<data-root>/db`` and therefore share
    one ledger.  Isolated rehearsal databases receive a ledger inside their
    own temporary database directory and can never mutate the live latch.
    """

    return _resolved(authority_db_path).parent / LEDGER_FILENAME


def _validate_domain(domain: str) -> None:
    if domain not in SUPPORTED_DOMAINS:
        raise ValueError(f"unsupported installed authority domain: {domain}")


def _connect_ledger(path: Path, *, create: bool) -> sqlite3.Connection:
    existed = path.is_file()
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    else:
        if not path.is_file():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10,
            isolation_level=None,
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    if create:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        if existed:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if (
                version != LEDGER_SCHEMA_VERSION
                or "installed_domain_authority" not in tables
            ):
                conn.close()
                raise InstalledAuthorityError(
                    "installed authority ledger schema is invalid"
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS installed_domain_authority (
                domain TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK(state IN
                    ('sealing','sealed','released','recovery_fenced')),
                cohort_id TEXT NOT NULL,
                authority_db_path_sha256 TEXT NOT NULL
                    CHECK(length(authority_db_path_sha256)=64),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                sealing_started_at TEXT NOT NULL,
                sealed_at TEXT,
                released_at TEXT,
                recovery_fenced_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(f"PRAGMA user_version={LEDGER_SCHEMA_VERSION}")
    return conn


def initialize_installed_authority_ledger(
    authority_db_path: str | Path,
) -> Path:
    """Create and prove an empty installation ledger for cutover backup.

    This is deliberately weaker than ``prepare_domain_seal``: it creates only
    the shared schema and never publishes a domain latch.  Replays are allowed
    only while the ledger remains empty; an existing authority row means the
    installation has already entered a seal lifecycle and must be handled by
    that lifecycle rather than by preflight preparation.
    """

    ledger = ledger_path_for(authority_db_path)
    create = not ledger.is_file()
    try:
        conn = _connect_ledger(ledger, create=create)
        try:
            conn.execute("BEGIN IMMEDIATE" if create else "BEGIN")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            quick_check = [tuple(row) for row in conn.execute("PRAGMA quick_check")]
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            columns = [
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(installed_domain_authority)"
                )
            ]
            expected_columns = [
                "domain",
                "state",
                "cohort_id",
                "authority_db_path_sha256",
                "revision",
                "sealing_started_at",
                "sealed_at",
                "released_at",
                "recovery_fenced_at",
                "updated_at",
            ]
            if (
                version != LEDGER_SCHEMA_VERSION
                or quick_check != [("ok",)]
                or tables != {"installed_domain_authority"}
                or columns != expected_columns
            ):
                raise InstalledAuthorityError(
                    "installed authority ledger schema is invalid"
                )
            authority_rows = int(
                conn.execute(
                    "SELECT COUNT(*) FROM installed_domain_authority"
                ).fetchone()[0]
            )
            if authority_rows != 0:
                raise InstalledAuthorityError(
                    "installed authority ledger is not empty before preflight"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    except InstalledAuthorityError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise InstalledAuthorityError(
            "installed authority ledger could not be initialized"
        ) from exc
    if not ledger.is_file() or ledger.stat().st_size <= 0:
        raise InstalledAuthorityError(
            "installed authority ledger was not durably initialized"
        )
    return ledger


def _ledger_row(domain: str, ledger_path: Path) -> sqlite3.Row | None:
    if not ledger_path.is_file():
        return None
    try:
        conn = _connect_ledger(ledger_path, create=False)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != LEDGER_SCHEMA_VERSION:
                raise InstalledAuthorityError(
                    "installed authority ledger schema is unsupported"
                )
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "installed_domain_authority" not in tables:
                raise InstalledAuthorityError(
                    "installed authority ledger table is missing"
                )
            return conn.execute(
                "SELECT * FROM installed_domain_authority WHERE domain=?",
                (domain,),
            ).fetchone()
        finally:
            conn.close()
    except InstalledAuthorityError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise InstalledAuthorityError(
            "installed authority ledger is unavailable"
        ) from exc


def _read_authority_row(
    conn: sqlite3.Connection,
    *,
    table: str,
) -> sqlite3.Row:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if table not in tables:
        raise InstalledAuthorityError("sealed authority table is missing")
    row = conn.execute(f"SELECT * FROM {table} WHERE singleton=1").fetchone()
    if row is None:
        raise InstalledAuthorityError("sealed authority row is missing")
    return row


def _require_sealed_cohort(
    conn: sqlite3.Connection,
    *,
    table: str,
    cohort_id: str,
) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if table not in tables:
        raise InstalledAuthorityError("sealed import cohort table is missing")
    row = conn.execute(
        f"SELECT state FROM {table} WHERE cohort_id=?", (cohort_id,)
    ).fetchone()
    if row is None or str(row[0]) != "sealed":
        raise InstalledAuthorityError(
            "installed authority does not have its sealed import cohort"
        )


def prove_domain_authority(
    domain: str,
    authority_db_path: str | Path,
    *,
    expected_cohort_id: str | None = None,
) -> tuple[str, str]:
    """Prove a bound domain database is intact and natively authoritative."""

    _validate_domain(domain)
    path = _resolved(authority_db_path)
    if not path.is_file():
        raise InstalledAuthorityError(
            f"installed {domain} authority database is missing"
        )
    try:
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            integrity = [tuple(row) for row in conn.execute("PRAGMA quick_check")]
            if integrity != [("ok",)]:
                raise InstalledAuthorityError(
                    f"installed {domain} authority database is corrupt"
                )
            if domain == "journal":
                row = _read_authority_row(
                    conn, table="journal_authority_control"
                )
                mode = str(row["mode"])
                cohort_id = str(row["activated_cohort_id"] or "")
                if mode not in {"database_only", "recovery_fenced"}:
                    raise InstalledAuthorityError(
                        "installed Journal authority is not database-only or recovery-fenced"
                    )
                _require_sealed_cohort(
                    conn, table="journal_import_cohorts", cohort_id=cohort_id
                )
            elif domain == "projects":
                row = _read_authority_row(conn, table="project_authority_state")
                mode = str(row["state"])
                cohort_id = str(row["sealed_cohort_id"] or "")
                if str(row["authority"]) != "sqlite" or mode not in {
                    "active", "write_fenced", "recovery"
                }:
                    raise InstalledAuthorityError(
                        "installed Projects authority is not sealed SQLite"
                    )
                _require_sealed_cohort(
                    conn, table="project_import_cohorts", cohort_id=cohort_id
                )
            elif domain == "contracts":
                row = _read_authority_row(conn, table="contract_authority")
                mode = str(row["state"])
                cohort_id = str(row["sealed_cohort_id"] or "")
                if mode != "native":
                    raise InstalledAuthorityError(
                        "installed Contracts authority is not native"
                    )
                _require_sealed_cohort(
                    conn, table="contract_import_cohorts", cohort_id=cohort_id
                )
            else:
                row = _read_authority_row(
                    conn, table="personal_knowledge_authority"
                )
                mode = str(row["authority"])
                cohort_id = str(row["sealed_cohort_id"] or "")
                if mode != "sqlite":
                    raise InstalledAuthorityError(
                        "installed personal knowledge authority is not SQLite"
                    )
                _require_sealed_cohort(
                    conn, table="personal_import_cohorts", cohort_id=cohort_id
                )
        finally:
            conn.close()
    except InstalledAuthorityError:
        raise
    except (OSError, sqlite3.Error, KeyError) as exc:
        raise InstalledAuthorityError(
            f"installed {domain} authority database is unavailable"
        ) from exc
    if not cohort_id:
        raise InstalledAuthorityError(
            f"installed {domain} authority has no sealed cohort"
        )
    if expected_cohort_id is not None and cohort_id != expected_cohort_id:
        raise InstalledAuthorityError(
            f"installed {domain} authority cohort does not match its latch"
        )
    return mode, cohort_id


def inspect_restore_rebind_plan(
    ledger_path: str | Path,
    target_paths: Mapping[str, str | Path],
) -> dict[str, Any]:
    """Inspect a relocated restore without changing its installation latch.

    Installed authority intentionally binds a sealed domain to the absolute
    database path used by the installation that performed the cutover.  A
    machine restore may therefore need an explicit rebind.  This inspection
    emits a content-free semantic plan that can be fingerprinted by a consent
    receipt; it never treats a path match alone as proof of authority.
    """

    ledger = _resolved(ledger_path)
    if not ledger.is_file():
        core: dict[str, Any] = {
            "schema": RESTORE_REBIND_PLAN_SCHEMA,
            "ledger_present": False,
            "ledger_schema_version": None,
            "rows": [],
        }
        return {
            **core,
            "required": False,
            "ready": True,
            "plan_sha256": _canonical_sha256(core),
        }

    rows: list[dict[str, Any]] = []
    try:
        conn = _connect_ledger(ledger, create=False)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != LEDGER_SCHEMA_VERSION:
                raise InstalledAuthorityError(
                    "installed authority restore ledger schema is unsupported"
                )
            if [tuple(row) for row in conn.execute("PRAGMA quick_check")] != [
                ("ok",)
            ]:
                raise InstalledAuthorityError(
                    "installed authority restore ledger is corrupt"
                )
            authority_rows = conn.execute(
                "SELECT domain,state,cohort_id,authority_db_path_sha256,revision "
                "FROM installed_domain_authority ORDER BY domain"
            ).fetchall()
        finally:
            conn.close()
    except InstalledAuthorityError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise InstalledAuthorityError(
            "installed authority restore ledger is unavailable"
        ) from exc

    for authority_row in authority_rows:
        domain = str(authority_row["domain"])
        _validate_domain(domain)
        state = str(authority_row["state"])
        cohort_id = str(authority_row["cohort_id"])
        current_path_sha256 = str(authority_row["authority_db_path_sha256"])
        if not _valid_sha256(current_path_sha256):
            raise InstalledAuthorityError(
                f"installed {domain} restore path binding is invalid"
            )
        target_value = target_paths.get(domain)
        if target_value is None:
            raise InstalledAuthorityError(
                f"installed {domain} restore target path is missing"
            )
        target = _resolved(target_value)
        target_path_sha256 = _path_sha256(target)
        requires_rebind = current_path_sha256 != target_path_sha256
        blocker: str | None = None
        authority_mode: str | None = None
        if state not in _RESTORE_REBINDABLE_STATES:
            blocker = "incomplete_seal"
        else:
            try:
                authority_mode, proven_cohort = prove_domain_authority(
                    domain,
                    target,
                    expected_cohort_id=cohort_id,
                )
                if proven_cohort != cohort_id:  # defensive; helper enforces it
                    blocker = "cohort_mismatch"
            except InstalledAuthorityError:
                blocker = "sealed_authority_unproven"
        rows.append(
            {
                "domain": domain,
                "state": state,
                "cohort_id": cohort_id,
                "revision": int(authority_row["revision"]),
                "current_path_sha256": current_path_sha256,
                "target_path_sha256": target_path_sha256,
                "requires_rebind": requires_rebind,
                "authority_mode": authority_mode,
                "ready": blocker is None,
                "blocker": blocker,
            }
        )

    core = {
        "schema": RESTORE_REBIND_PLAN_SCHEMA,
        "ledger_present": True,
        "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "rows": rows,
    }
    return {
        **core,
        "required": any(bool(row["requires_rebind"]) for row in rows),
        "ready": all(bool(row["ready"]) for row in rows),
        "plan_sha256": _canonical_sha256(core),
    }


def rebind_restored_authority_paths(
    ledger_path: str | Path,
    target_paths: Mapping[str, str | Path],
    *,
    expected_plan_sha256: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Atomically rebind one consent-bound relocated restore.

    The caller must supply the exact plan fingerprint displayed at the trusted
    restore-authorization boundary.  Every row and every target domain cohort
    is re-proved after approval and before the ledger transaction commits.
    Missing, malformed, incomplete, or changed state fails without modifying
    any path binding.  Replaying an already completed rebind is safe and emits
    a new content-free observation receipt.
    """

    if not _valid_sha256(expected_plan_sha256):
        raise ValueError("installed authority restore plan digest is invalid")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("installed authority restore snapshot_id is required")
    ledger = _resolved(ledger_path)
    before = inspect_restore_rebind_plan(ledger, target_paths)
    if before["plan_sha256"] != expected_plan_sha256:
        raise InstalledAuthorityError(
            "installed authority restore plan changed after authorization"
        )
    if not before["ready"]:
        raise InstalledAuthorityError(
            "installed authority restore targets cannot prove every sealed cohort"
        )

    rebound_domains = [
        str(row["domain"])
        for row in before["rows"]
        if row["requires_rebind"]
    ]
    if rebound_domains:
        try:
            conn = _connect_ledger(ledger, create=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                for planned in before["rows"]:
                    domain = str(planned["domain"])
                    current = conn.execute(
                        "SELECT state,cohort_id,authority_db_path_sha256,revision "
                        "FROM installed_domain_authority WHERE domain=?",
                        (domain,),
                    ).fetchone()
                    if current is None or (
                        str(current["state"]) != planned["state"]
                        or str(current["cohort_id"]) != planned["cohort_id"]
                        or str(current["authority_db_path_sha256"])
                        != planned["current_path_sha256"]
                        or int(current["revision"]) != planned["revision"]
                    ):
                        raise InstalledAuthorityError(
                            "installed authority restore ledger changed after authorization"
                        )
                    if not planned["requires_rebind"]:
                        continue
                    target = _resolved(target_paths[domain])
                    prove_domain_authority(
                        domain,
                        target,
                        expected_cohort_id=str(planned["cohort_id"]),
                    )
                    updated = conn.execute(
                        "UPDATE installed_domain_authority SET "
                        "authority_db_path_sha256=?,revision=revision+1,updated_at=? "
                        "WHERE domain=? AND state=? AND cohort_id=? "
                        "AND authority_db_path_sha256=? AND revision=?",
                        (
                            planned["target_path_sha256"],
                            _now(),
                            domain,
                            planned["state"],
                            planned["cohort_id"],
                            planned["current_path_sha256"],
                            planned["revision"],
                        ),
                    ).rowcount
                    if updated != 1:
                        raise InstalledAuthorityError(
                            "installed authority restore rebind lost its exact row"
                        )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        except InstalledAuthorityError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise InstalledAuthorityError(
                "installed authority restore rebind failed"
            ) from exc

    after = inspect_restore_rebind_plan(ledger, target_paths)
    if not after["ready"] or after["required"]:
        raise InstalledAuthorityError(
            "installed authority restore rebind did not prove the target installation"
        )
    return {
        "schema": RESTORE_REBIND_RECEIPT_SCHEMA,
        "snapshot_id": snapshot_id,
        "authorized_plan_sha256": expected_plan_sha256,
        "result_plan_sha256": after["plan_sha256"],
        "rebound_domains": rebound_domains,
        "result": "rebound" if rebound_domains else "already_bound",
        "rebound_at": _now(),
    }


def installed_authority_status(
    domain: str,
    authority_db_path: str | Path,
) -> InstalledAuthorityStatus | None:
    """Return and verify an installed latch, or ``None`` before cutover.

    Every non-absent latch state is irreversible and fail closed.  ``sealing``
    specifically represents a crash window that requires operator recovery.
    """

    _validate_domain(domain)
    path = _resolved(authority_db_path)
    ledger = ledger_path_for(path)
    row = _ledger_row(domain, ledger)
    if row is None:
        return None
    state = str(row["state"])
    if state not in _SEALED_STATES:
        raise InstalledAuthorityError("installed authority latch state is invalid")
    expected_path_sha = _path_sha256(path)
    if str(row["authority_db_path_sha256"]) != expected_path_sha:
        raise InstalledAuthorityError(
            f"installed {domain} authority path does not match its latch"
        )
    if state == "sealing":
        raise InstalledAuthorityError(
            f"installed {domain} authority seal is incomplete; recovery is required"
        )
    mode, cohort_id = prove_domain_authority(
        domain, path, expected_cohort_id=str(row["cohort_id"])
    )
    return InstalledAuthorityStatus(
        domain=domain,
        state=state,
        cohort_id=cohort_id,
        authority_db_path_sha256=expected_path_sha,
        authority_mode=mode,
        revision=int(row["revision"]),
        ledger_path=ledger,
    )


def _sealing_row(
    domain: str,
    authority_db_path: str | Path,
    *,
    cohort_id: str,
) -> sqlite3.Row:
    """Return an exact incomplete latch without weakening its public fence."""

    _validate_domain(domain)
    path = _resolved(authority_db_path)
    row = _ledger_row(domain, ledger_path_for(path))
    if row is None or str(row["state"]) != "sealing":
        raise InstalledAuthorityError(
            f"installed {domain} authority has no incomplete seal to recover"
        )
    if (
        str(row["cohort_id"]) != cohort_id
        or str(row["authority_db_path_sha256"]) != _path_sha256(path)
    ):
        raise InstalledAuthorityError(
            f"installed {domain} authority recovery does not match its latch"
        )
    return row


def _prove_recoverable_preseal(
    domain: str,
    authority_db_path: str | Path,
    *,
    cohort_id: str,
    inventory_sha256: str,
) -> None:
    """Prove the exact durable pre-seal fence for a roll-forward retry.

    This is intentionally stricter than merely observing legacy authority.  A
    recovery caller must prove the cohort, inventory, maintenance fence, import
    state, and domain-specific authority state left by the normal live operator.
    """

    if (
        len(inventory_sha256) != 64
        or any(char not in "0123456789abcdef" for char in inventory_sha256)
    ):
        raise InstalledAuthorityError("installed authority recovery inventory is invalid")
    path = _resolved(authority_db_path)
    if not path.is_file():
        raise InstalledAuthorityError(
            f"installed {domain} authority database is missing during recovery"
        )
    try:
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            if [tuple(row) for row in conn.execute("PRAGMA quick_check")] != [("ok",)]:
                raise InstalledAuthorityError(
                    f"installed {domain} authority database is corrupt during recovery"
                )
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {"cutover_maintenance"}
            declarations = {
                "journal": (
                    "journal_authority_control",
                    "journal_import_cohorts",
                    "sealed",
                ),
                "projects": (
                    "project_authority_state",
                    "project_import_cohorts",
                    "verified",
                ),
                "contracts": (
                    "contract_authority",
                    "contract_import_cohorts",
                    "staged",
                ),
                "personal_knowledge": (
                    "personal_knowledge_authority",
                    "personal_import_cohorts",
                    "verified",
                ),
            }
            authority_table, cohort_table, cohort_state = declarations[domain]
            required.update({authority_table, cohort_table})
            if domain == "journal":
                required.add("journal_cutover_gate")
            if not required.issubset(tables):
                raise InstalledAuthorityError(
                    f"installed {domain} authority recovery tables are incomplete"
                )

            maintenance = conn.execute(
                "SELECT domain,state,cohort_id,inventory_sha256 "
                "FROM cutover_maintenance WHERE singleton=1"
            ).fetchone()
            if (
                maintenance is None
                or str(maintenance["domain"]) != domain
                or str(maintenance["state"]) != "preseal_fenced"
                or str(maintenance["cohort_id"]) != cohort_id
                or str(maintenance["inventory_sha256"]) != inventory_sha256
            ):
                raise InstalledAuthorityError(
                    f"installed {domain} authority recovery fence does not match"
                )
            cohort = conn.execute(
                f"SELECT state,inventory_sha256 FROM {cohort_table} WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if (
                cohort is None
                or str(cohort["state"]) != cohort_state
                or str(cohort["inventory_sha256"]) != inventory_sha256
            ):
                raise InstalledAuthorityError(
                    f"installed {domain} authority recovery cohort does not match"
                )

            authority = conn.execute(
                f"SELECT * FROM {authority_table} WHERE singleton=1"
            ).fetchone()
            if authority is None:
                raise InstalledAuthorityError(
                    f"installed {domain} authority recovery state is missing"
                )
            if domain == "journal":
                if str(authority["mode"]) != "legacy_compatibility":
                    raise InstalledAuthorityError(
                        "installed Journal recovery is not at compatibility authority"
                    )
                gate = conn.execute(
                    "SELECT state,cohort_id FROM journal_cutover_gate WHERE singleton=1"
                ).fetchone()
                if (
                    gate is None
                    or str(gate["state"]) != "paused"
                    or str(gate["cohort_id"]) != cohort_id
                ):
                    raise InstalledAuthorityError(
                        "installed Journal recovery gate does not match"
                    )
            elif domain == "projects":
                if (
                    str(authority["authority"]) != "legacy_markdown"
                    or str(authority["state"]) != "write_fenced"
                ):
                    raise InstalledAuthorityError(
                        "installed Projects recovery is not at its pre-seal fence"
                    )
            elif domain == "contracts":
                if str(authority["state"]) != "legacy":
                    raise InstalledAuthorityError(
                        "installed Contracts recovery is not at legacy authority"
                    )
            elif str(authority["authority"]) != "legacy_markdown":
                raise InstalledAuthorityError(
                    "installed personal knowledge recovery is not at legacy authority"
                )
        finally:
            conn.close()
    except InstalledAuthorityError:
        raise
    except (OSError, sqlite3.Error, KeyError) as exc:
        raise InstalledAuthorityError(
            f"installed {domain} authority pre-seal recovery proof failed"
        ) from exc


def _matching_incomplete_recovery(
    domain: str,
    authority_db_path: str | Path,
) -> tuple[str, str] | None:
    permit = _INCOMPLETE_SEAL_RECOVERY.get()
    if permit is None:
        return None
    permit_domain, path_sha256, cohort_id, inventory_sha256 = permit
    if permit_domain != domain or path_sha256 != _path_sha256(authority_db_path):
        return None
    return cohort_id, inventory_sha256


def require_domain_store_open(
    domain: str,
    authority_db_path: str | Path,
) -> InstalledAuthorityStatus | None:
    """Fail closed when an installed seal cannot prove its native database."""

    recovery = _matching_incomplete_recovery(domain, authority_db_path)
    if recovery is not None:
        cohort_id, inventory_sha256 = recovery
        path = _resolved(authority_db_path)
        row = _ledger_row(domain, ledger_path_for(path))
        if row is None:
            raise InstalledAuthorityError(
                f"installed {domain} authority recovery latch disappeared"
            )
        if str(row["state"]) != "sealing":
            status = installed_authority_status(domain, path)
            if status is None or status.cohort_id != cohort_id:
                raise InstalledAuthorityError(
                    f"installed {domain} authority recovery cohort changed"
                )
            return status
        _sealing_row(domain, path, cohort_id=cohort_id)
        try:
            prove_domain_authority(
                domain, path, expected_cohort_id=cohort_id
            )
        except InstalledAuthorityError:
            _prove_recoverable_preseal(
                domain,
                path,
                cohort_id=cohort_id,
                inventory_sha256=inventory_sha256,
            )
            return None
        return confirm_domain_seal(
            domain, path, cohort_id=cohort_id
        )
    return installed_authority_status(domain, authority_db_path)


@contextmanager
def recover_incomplete_domain_seal(
    domain: str,
    authority_db_path: str | Path,
    *,
    cohort_id: str,
    inventory_sha256: str,
) -> Iterator[str]:
    """Admit one exact roll-forward retry for a crash-interrupted seal.

    Ordinary store opens remain fail closed.  A post-commit crash is finalized
    immediately after proving the sealed database.  A pre-commit crash receives
    a process-local permit only while the same fenced cohort is replayed; normal
    seal code must commit and confirm before the context can exit successfully.
    """

    _validate_domain(domain)
    if not isinstance(cohort_id, str) or not cohort_id.strip():
        raise ValueError("installed authority recovery cohort_id is required")
    path = _resolved(authority_db_path)
    row = _ledger_row(domain, ledger_path_for(path))
    if row is None:
        yield "not_required"
        return
    if str(row["state"]) != "sealing":
        status = installed_authority_status(domain, path)
        if status is None or status.cohort_id != cohort_id:
            raise InstalledAuthorityError(
                f"installed {domain} authority recovery cohort does not match"
            )
        yield "not_required"
        return

    _sealing_row(domain, path, cohort_id=cohort_id)
    try:
        prove_domain_authority(domain, path, expected_cohort_id=cohort_id)
    except InstalledAuthorityError:
        _prove_recoverable_preseal(
            domain,
            path,
            cohort_id=cohort_id,
            inventory_sha256=inventory_sha256,
        )
    else:
        confirm_domain_seal(domain, path, cohort_id=cohort_id)
        yield "confirmed"
        return

    token = _INCOMPLETE_SEAL_RECOVERY.set(
        (domain, _path_sha256(path), cohort_id, inventory_sha256)
    )
    try:
        yield "resumed"
    except BaseException:
        # The durable sealing row remains in place for an exact later replay.
        raise
    else:
        confirm_domain_seal(domain, path, cohort_id=cohort_id)
    finally:
        _INCOMPLETE_SEAL_RECOVERY.reset(token)


def prepare_domain_seal(
    domain: str,
    authority_db_path: str | Path,
    *,
    cohort_id: str,
) -> Path:
    """Persist the fail-closed half of a domain seal before publication."""

    _validate_domain(domain)
    if not isinstance(cohort_id, str) or not cohort_id.strip():
        raise ValueError("installed authority cohort_id is required")
    path = _resolved(authority_db_path)
    ledger = ledger_path_for(path)
    path_sha = _path_sha256(path)
    now = _now()
    try:
        conn = _connect_ledger(ledger, create=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM installed_domain_authority WHERE domain=?",
                (domain,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO installed_domain_authority "
                    "(domain,state,cohort_id,authority_db_path_sha256,revision,"
                    "sealing_started_at,updated_at) VALUES (?, 'sealing', ?, ?, 1, ?, ?)",
                    (domain, cohort_id, path_sha, now, now),
                )
            elif (
                str(row["cohort_id"]) != cohort_id
                or str(row["authority_db_path_sha256"]) != path_sha
            ):
                raise InstalledAuthorityError(
                    f"installed {domain} authority latch is already bound"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    except InstalledAuthorityError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise InstalledAuthorityError(
            "installed authority seal latch could not be persisted"
        ) from exc
    return ledger


def confirm_domain_seal(
    domain: str,
    authority_db_path: str | Path,
    *,
    cohort_id: str,
) -> InstalledAuthorityStatus:
    """Verify the committed native authority and finalize its installed latch."""

    path = _resolved(authority_db_path)
    mode, proven_cohort = prove_domain_authority(
        domain, path, expected_cohort_id=cohort_id
    )
    del mode, proven_cohort
    ledger = ledger_path_for(path)
    now = _now()
    try:
        conn = _connect_ledger(ledger, create=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM installed_domain_authority WHERE domain=?",
                (domain,),
            ).fetchone()
            if (
                row is None
                or str(row["cohort_id"]) != cohort_id
                or str(row["authority_db_path_sha256"]) != _path_sha256(path)
            ):
                raise InstalledAuthorityError(
                    f"installed {domain} authority latch changed during seal"
                )
            if str(row["state"]) == "sealing":
                conn.execute(
                    "UPDATE installed_domain_authority SET state='sealed',"
                    "sealed_at=?,updated_at=?,revision=revision+1 WHERE domain=?",
                    (now, now, domain),
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    except InstalledAuthorityError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise InstalledAuthorityError(
            "installed authority seal latch could not be finalized"
        ) from exc
    status = installed_authority_status(domain, path)
    if status is None:  # pragma: no cover - guarded by the transaction above
        raise InstalledAuthorityError("installed authority latch disappeared")
    return status


def mark_domain_released(
    domain: str,
    authority_db_path: str | Path,
    *,
    cohort_id: str,
) -> InstalledAuthorityStatus:
    """Persist successful post-seal release without weakening the seal."""

    path = _resolved(authority_db_path)
    status = installed_authority_status(domain, path)
    if status is None or status.cohort_id != cohort_id:
        raise InstalledAuthorityError(
            f"installed {domain} authority is not sealed for release"
        )
    if status.state != "released":
        now = _now()
        conn = _connect_ledger(status.ledger_path, create=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE installed_domain_authority SET state='released',"
                "released_at=COALESCE(released_at,?),updated_at=?,revision=revision+1 "
                "WHERE domain=? AND cohort_id=? AND state IN ('sealed','recovery_fenced')",
                (now, now, domain, cohort_id),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    result = installed_authority_status(domain, path)
    if result is None:  # pragma: no cover
        raise InstalledAuthorityError("installed authority latch disappeared")
    return result


__all__ = [
    "InstalledAuthorityError",
    "InstalledAuthorityStatus",
    "LEDGER_FILENAME",
    "RESTORE_REBIND_PLAN_SCHEMA",
    "RESTORE_REBIND_RECEIPT_SCHEMA",
    "SUPPORTED_DOMAINS",
    "confirm_domain_seal",
    "initialize_installed_authority_ledger",
    "inspect_restore_rebind_plan",
    "installed_authority_status",
    "ledger_path_for",
    "mark_domain_released",
    "prepare_domain_seal",
    "prove_domain_authority",
    "recover_incomplete_domain_seal",
    "rebind_restored_authority_paths",
    "require_domain_store_open",
]
