"""Read-only detachment of sealed legacy domain roots from filesystem search.

The authority rows are the switch. No live configuration edit is required:
once Journal, Projects, Contracts, or Personal Knowledge seals its SQLite
authority, the corresponding configured Markdown archive is omitted from
Vault discovery and direct parsing. Checks use SQLite ``mode=ro`` and never
create, migrate, or write an authority database.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from work_buddy.logging_config import get_logger


logger = get_logger(__name__)


class LegacyRootAuthorityError(RuntimeError):
    """A configured legacy archive is not writable under current authority."""


_HELD_LEGACY_DOMAIN_GUARDS: ContextVar[tuple[str, ...]] = ContextVar(
    "held_legacy_domain_write_guards",
    default=(),
)


def _absolute(path: str | Path, *, base: Path | None = None) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() and base is not None:
        value = base / value
    return Path(os.path.abspath(str(value)))


def _data_root(cfg: dict[str, Any], *, allow_default: bool) -> Path | None:
    override = os.environ.get("WORK_BUDDY_DATA_DIR")
    if override:
        return _absolute(override) / "db"
    paths = cfg.get("paths")
    if isinstance(paths, dict) and paths.get("data_root"):
        from work_buddy.paths import repo_root

        return _absolute(str(paths["data_root"]), base=repo_root()) / "db"
    if allow_default:
        # Follow the configured data-root contract without ``resolve()``, whose
        # convenience behavior creates the parent directory.  Certification is
        # a strictly read-only operation, including when state is absent.
        from work_buddy.paths import _data_base

        return _data_base() / "db"
    # Explicit synthetic configs must not consult the developer's real
    # authority databases by accident.
    return None


def _project_db(cfg: dict[str, Any], data_root: Path) -> Path:
    configured = (cfg.get("projects") or {}).get("db_path")
    if configured:
        from work_buddy.paths import repo_root

        return _absolute(str(configured), base=repo_root())
    return data_root / "projects.db"


def _read_authority(
    path: Path,
    *,
    table: str,
    column: str,
    immutable: bool = False,
) -> tuple[bool, str | None]:
    """Return ``(database_exists, value)`` without creating or migrating it.

    An existing but unreadable/malformed authority fails closed: callers detach
    the legacy root rather than silently reviving a possibly retired archive.
    """

    if not path.is_file():
        return False, None
    if immutable:
        for suffix in ("-wal", "-journal"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.is_file() and sidecar.stat().st_size:
                logger.warning(
                    "vault index cannot certify authority %s while its SQLite "
                    "sidecar is uncheckpointed",
                    path.name,
                )
                return True, "__uncheckpointed_fail_closed__"
    connection: sqlite3.Connection | None = None
    try:
        immutable_query = "&immutable=1" if immutable else ""
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro{immutable_query}",
            uri=True,
            timeout=2,
        )
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return True, None
        row = connection.execute(
            f"SELECT {column} FROM {table} WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError(f"{table} singleton row is missing")
        return True, str(row[0])
    except sqlite3.Error as exc:
        logger.warning(
            "vault index is detaching a legacy root because authority %s "
            "could not be read safely: %s",
            path.name,
            exc,
        )
        return True, "__invalid_fail_closed__"
    finally:
        if connection is not None:
            connection.close()


@dataclass(frozen=True, slots=True)
class LegacyAuthorityState:
    """Read-only authority fact paired with its configured legacy root."""

    domain: str
    database_path: Path
    configured_root: Path
    database_exists: bool
    value: str | None
    sealed: bool


_DOMAIN_ORDER = ("journal", "projects", "contracts", "personal_knowledge")


def _authority_declarations(
    cfg: dict[str, Any],
    *,
    allow_default_data_root: bool,
) -> dict[str, tuple[Path, str, str, str]]:
    data_root = _data_root(cfg, allow_default=allow_default_data_root)
    if data_root is None:
        return {}
    return {
        "journal": (
            data_root / "journal_capture.db",
            "journal_authority_control",
            "mode",
            "legacy_compatibility",
        ),
        "projects": (
            _project_db(cfg, data_root),
            "project_authority_state",
            "authority",
            "legacy_markdown",
        ),
        "contracts": (
            data_root / "contracts.db",
            "contract_authority",
            "state",
            "legacy",
        ),
        "personal_knowledge": (
            data_root / "personal_knowledge.db",
            "personal_knowledge_authority",
            "authority",
            "legacy_markdown",
        ),
    }


def configured_legacy_roots(cfg: dict[str, Any]) -> dict[str, Path]:
    """Resolve the four supported domain roots from configuration only."""

    vault_value = cfg.get("vault_root")
    if not vault_value:
        return {}
    vault_root = _absolute(str(vault_value))
    obsidian = cfg.get("obsidian") or {}
    projects = cfg.get("projects") or {}
    contracts = cfg.get("contracts") or {}
    personal = cfg.get("personal_knowledge") or {}
    return {
        "journal": _absolute(
            str(obsidian.get("journal_dir", "journal")), base=vault_root
        ),
        "projects": _absolute(
            str(projects.get("markdown_dir", "work-buddy/projects")), base=vault_root
        ),
        "contracts": _absolute(
            str(contracts.get("vault_path", "work-buddy/contracts")), base=vault_root
        ),
        "personal_knowledge": _absolute(
            str(personal.get("vault_path", "Meta/WorkBuddy")), base=vault_root
        ),
    }


def prospective_legacy_roots(
    cfg: dict[str, Any],
    domains: Iterable[str],
) -> tuple[Path, ...]:
    """Validate domain names and return only their configured legacy roots.

    This is the bounded pre-seal operator boundary: callers select known
    domains, never arbitrary paths.  It permits an index purge before authority
    exposure; the durable authority resolver takes over after the seal.
    """

    requested = tuple(dict.fromkeys(str(domain) for domain in domains))
    unknown = sorted(set(requested) - set(_DOMAIN_ORDER))
    if unknown:
        raise ValueError(f"unsupported prospective legacy domains: {', '.join(unknown)}")
    roots = configured_legacy_roots(cfg)
    if requested and len(roots) != len(_DOMAIN_ORDER):
        raise ValueError("vault_root is required for prospective legacy detachment")
    return tuple(roots[domain] for domain in requested)


def legacy_authority_states(
    cfg: dict[str, Any],
    *,
    allow_default_data_root: bool = False,
    immutable: bool = False,
) -> dict[str, LegacyAuthorityState]:
    """Inspect all four authority rows without creating or migrating a store."""

    roots = configured_legacy_roots(cfg)
    data_root = _data_root(cfg, allow_default=allow_default_data_root)
    if not roots or data_root is None:
        return {}

    declarations = {
        "journal": (
            data_root / "journal_capture.db",
            "journal_authority_control",
            "mode",
            {
                "database_only",
                "recovery_fenced",
                "__invalid_fail_closed__",
                "__uncheckpointed_fail_closed__",
            },
        ),
        "projects": (
            _project_db(cfg, data_root),
            "project_authority_state",
            "authority",
            {"sqlite", "__invalid_fail_closed__", "__uncheckpointed_fail_closed__"},
        ),
        "contracts": (
            data_root / "contracts.db",
            "contract_authority",
            "state",
            {"native", "__invalid_fail_closed__", "__uncheckpointed_fail_closed__"},
        ),
        "personal_knowledge": (
            data_root / "personal_knowledge.db",
            "personal_knowledge_authority",
            "authority",
            {"sqlite", "__invalid_fail_closed__", "__uncheckpointed_fail_closed__"},
        ),
    }
    result: dict[str, LegacyAuthorityState] = {}
    from work_buddy.installed_authority import (
        InstalledAuthorityError,
        installed_authority_status,
    )

    for domain in _DOMAIN_ORDER:
        db_path, table, column, sealed_values = declarations[domain]
        exists, value = _read_authority(
            db_path,
            table=table,
            column=column,
            immutable=immutable,
        )
        try:
            installed_detached = (
                installed_authority_status(domain, db_path) is not None
            )
        except InstalledAuthorityError:
            # A present but unprovable installation latch is an irreversible
            # fail-closed fact.  Filesystem readers must never use the archive
            # as an implicit recovery path.
            installed_detached = True
        result[domain] = LegacyAuthorityState(
            domain=domain,
            database_path=db_path,
            configured_root=roots[domain],
            database_exists=exists,
            value=value,
            sealed=(exists and value in sealed_values) or installed_detached,
        )
    return result


def sealed_legacy_roots(
    cfg: dict[str, Any],
    *,
    allow_default_data_root: bool = False,
) -> tuple[Path, ...]:
    """Return configured Markdown roots detached by durable authority seals."""

    states = legacy_authority_states(
        cfg, allow_default_data_root=allow_default_data_root
    )
    return tuple(
        dict.fromkeys(
            state.configured_root for state in states.values() if state.sealed
        )
    )


def _check_legacy_authority_locked(
    conn: sqlite3.Connection,
    *,
    domain: str,
    table: str,
    column: str,
    legacy_value: str,
) -> None:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if table in tables:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE singleton=1"
        ).fetchone()
        if row is None or str(row[column]) != legacy_value:
            value = "invalid" if row is None else str(row[column])
            raise LegacyRootAuthorityError(
                f"{domain} legacy archive writes are fenced while authority is {value}."
            )
        # Early authority-only project databases did not yet carry the
        # maintenance state column. When it is present, require the live
        # compatibility state; the shared cutover-maintenance row below is the
        # second fence for current schemas.
        if (
            domain == "projects"
            and "state" in row.keys()
            and str(row["state"]) != "active"
        ):
            raise LegacyRootAuthorityError(
                "projects legacy archive writes are fenced while authority is paused."
            )
    if "cutover_maintenance" in tables:
        maintenance = conn.execute(
            "SELECT state FROM cutover_maintenance WHERE singleton=1"
        ).fetchone()
        if maintenance is None or str(maintenance[0]) != "open":
            state = "invalid" if maintenance is None else str(maintenance[0])
            raise LegacyRootAuthorityError(
                f"{domain} legacy archive writes are fenced while maintenance is {state}."
            )
    if domain == "journal" and "journal_cutover_gate" in tables:
        gate = conn.execute(
            "SELECT state FROM journal_cutover_gate WHERE singleton=1"
        ).fetchone()
        if gate is None or str(gate[0]) != "open":
            state = "invalid" if gate is None else str(gate[0])
            raise LegacyRootAuthorityError(
                f"Journal legacy archive writes are fenced while cutover gate is {state}."
            )


@contextmanager
def _legacy_domain_write_guard(
    *,
    domain: str,
    database_path: Path,
    table: str,
    column: str,
    legacy_value: str,
) -> Iterator[None]:
    identity = f"{domain}:{normalized_path(database_path, real=True)}"
    held = _HELD_LEGACY_DOMAIN_GUARDS.get()
    if identity in held:
        yield
        return

    from work_buddy.installed_authority import (
        InstalledAuthorityError,
        installed_authority_status,
    )

    try:
        installed = installed_authority_status(domain, database_path)
    except InstalledAuthorityError as exc:
        raise LegacyRootAuthorityError(
            f"{domain} legacy archive writes are fenced by installed authority."
        ) from exc
    if installed is not None:
        raise LegacyRootAuthorityError(
            f"{domain} legacy archive writes are retired under installed authority."
        )

    if domain == "journal":
        from work_buddy.journal_capture.authority import (
            JournalAuthorityStateError,
            legacy_markdown_write_guard,
        )

        token = _HELD_LEGACY_DOMAIN_GUARDS.set((*held, identity))
        try:
            try:
                with legacy_markdown_write_guard(database_path):
                    yield
            except JournalAuthorityStateError as exc:
                raise LegacyRootAuthorityError(str(exc)) from exc
        finally:
            _HELD_LEGACY_DOMAIN_GUARDS.reset(token)
        return

    # A database absent before the native migration remains a legitimate
    # compatibility epoch.  The independent latch check above still fails
    # closed if this installation had ever sealed that missing database.
    if not database_path.is_file():
        token = _HELD_LEGACY_DOMAIN_GUARDS.set((*held, identity))
        try:
            yield
        finally:
            _HELD_LEGACY_DOMAIN_GUARDS.reset(token)
        return

    conn: sqlite3.Connection | None = None
    token = None
    try:
        conn = sqlite3.connect(
            f"file:{database_path.resolve().as_posix()}?mode=rw",
            uri=True,
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")

        # Recheck the independent latch after taking the domain writer lock.
        # If sealing raced us, either its `sealing` row is now visible and we
        # fail, or it must wait for this pre-seal operation to finish.
        try:
            installed = installed_authority_status(domain, database_path)
        except InstalledAuthorityError as exc:
            raise LegacyRootAuthorityError(
                f"{domain} legacy archive writes are fenced by installed authority."
            ) from exc
        if installed is not None:
            raise LegacyRootAuthorityError(
                f"{domain} legacy archive writes are retired under installed authority."
            )
        _check_legacy_authority_locked(
            conn,
            domain=domain,
            table=table,
            column=column,
            legacy_value=legacy_value,
        )
        token = _HELD_LEGACY_DOMAIN_GUARDS.set((*held, identity))
        yield
        conn.commit()
    except LegacyRootAuthorityError:
        if conn is not None and conn.in_transaction:
            conn.rollback()
        raise
    except sqlite3.Error as exc:
        if conn is not None and conn.in_transaction:
            conn.rollback()
        raise LegacyRootAuthorityError(
            f"{domain} authority is unavailable; legacy archive writes are fenced."
        ) from exc
    except BaseException:
        if conn is not None and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if token is not None:
            _HELD_LEGACY_DOMAIN_GUARDS.reset(token)
        if conn is not None:
            conn.close()


@contextmanager
def legacy_root_write_guard(
    target: str | Path,
    *,
    cfg: dict[str, Any] | None = None,
    allow_default_data_root: bool = True,
) -> Iterator[None]:
    """Hold authority locks for every configured legacy root containing target.

    Matching uses real paths so a symlink, junction, or alternate spelling
    cannot route a write around a sealed archive.  The check runs before the
    caller is allowed to inspect target existence or contents and the SQLite
    writer transaction remains open through the complete file operation.
    """

    if cfg is None:
        from work_buddy.config import load_config

        cfg = load_config()
    roots = configured_legacy_roots(cfg)
    declarations = _authority_declarations(
        cfg,
        allow_default_data_root=allow_default_data_root,
    )
    matches = [
        domain
        for domain in _DOMAIN_ORDER
        if domain in roots
        and domain in declarations
        and is_within(target, roots[domain], real=True)
    ]
    with ExitStack() as stack:
        for domain in matches:
            database_path, table, column, legacy_value = declarations[domain]
            stack.enter_context(
                _legacy_domain_write_guard(
                    domain=domain,
                    database_path=database_path,
                    table=table,
                    column=column,
                    legacy_value=legacy_value,
                )
            )
        yield


@contextmanager
def legacy_root_read_guard(
    target: str | Path,
    *,
    cfg: dict[str, Any] | None = None,
    allow_default_data_root: bool = True,
) -> Iterator[None]:
    """Serialize a legacy-root read with the irreversible authority seal.

    The underlying domain writer transaction is intentional: a parse admitted
    before a seal keeps the authority row stable through the complete file
    read, while a seal that wins the lock fences the read before it opens the
    archive.  This shares the established write-guard implementation so all
    four domain latches and recovery states remain identical.
    """

    with legacy_root_write_guard(
        target,
        cfg=cfg,
        allow_default_data_root=allow_default_data_root,
    ):
        yield


def normalized_path(path: str | Path, *, real: bool = False) -> str:
    value = os.path.realpath(path) if real else os.path.abspath(path)
    return os.path.normcase(os.path.normpath(value))


def is_within(path: str | Path, root: str | Path, *, real: bool = False) -> bool:
    candidate = normalized_path(path, real=real)
    boundary = normalized_path(root, real=real)
    try:
        return os.path.commonpath((candidate, boundary)) == boundary
    except ValueError:
        return False


__all__ = [
    "LegacyAuthorityState",
    "LegacyRootAuthorityError",
    "configured_legacy_roots",
    "is_within",
    "legacy_authority_states",
    "legacy_root_read_guard",
    "legacy_root_write_guard",
    "normalized_path",
    "prospective_legacy_roots",
    "sealed_legacy_roots",
]
