"""Authenticated loopback principal, session, and gesture boundary.

This module implements a deliberately narrow v1 identity boundary for the
local dashboard.  It establishes only that an enrolled local profile exercised
an exact action through a bound browser session.  It does **not** prove physical
presence, sole authorship, or freedom from a same-user-compromised browser,
extension, or process.

Bootstrap tokens are minted only through the in-process API.  HTTP exposes a
redemption endpoint, never a mint endpoint.  Browser session and CSRF secrets
are random bearer values; only their SHA-256 digests are persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from work_buddy import paths
from work_buddy.security.actors import ACTOR_REF_SCHEMA, ActorRef


DEFAULT_AUDIENCE = "work-buddy-dashboard"
SESSION_COOKIE_NAME = "wb_local_session"
CSRF_HEADER_NAME = "X-WB-CSRF"
GESTURE_HEADER_NAME = "X-WB-Gesture"
HUMAN_AUTHORITY_ASSURANCE = "enrolled_local_session_gesture"
HUMAN_AUTHORITY_BASIS = "authenticated_loopback_ui_gesture"
HUMAN_INPUT_INGRESS_SCHEMA = "wb.conversation-message-ingress/v1"
HUMAN_AUTHORITY_THREAT_LIMIT = (
    "Establishes that the enrolled local profile used this exact bound UI "
    "gesture; it does not prove physical presence, sole composition, or "
    "immunity from a same-user-compromised browser, extension, or process."
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,255}$")
_PROXY_HEADERS = frozenset(
    {
        "forwarded",
        "via",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    }
)


class LocalIdentityError(RuntimeError):
    """Typed, content-free failure safe to map onto an HTTP response."""

    def __init__(self, code: str, message: str, *, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class LocalIdentityPolicy:
    """Bounded lifetimes for local credentials.

    The defaults favor a normal working session while forcing periodic bearer
    rotation.  Constructor validation prevents config mistakes from creating
    effectively immortal credentials.
    """

    bootstrap_ttl_seconds: int = 90
    session_idle_ttl_seconds: int = 8 * 60 * 60
    session_absolute_ttl_seconds: int = 24 * 60 * 60
    session_rotation_seconds: int = 30 * 60
    gesture_ttl_seconds: int = 60

    def __post_init__(self) -> None:
        bounds = {
            "bootstrap_ttl_seconds": (self.bootstrap_ttl_seconds, 5, 300),
            "session_idle_ttl_seconds": (
                self.session_idle_ttl_seconds,
                60,
                24 * 60 * 60,
            ),
            "session_absolute_ttl_seconds": (
                self.session_absolute_ttl_seconds,
                5 * 60,
                7 * 24 * 60 * 60,
            ),
            "session_rotation_seconds": (
                self.session_rotation_seconds,
                60,
                24 * 60 * 60,
            ),
            "gesture_ttl_seconds": (self.gesture_ttl_seconds, 5, 300),
        }
        for name, (value, minimum, maximum) in bounds.items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            if value < minimum or value > maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.session_idle_ttl_seconds > self.session_absolute_ttl_seconds:
            raise ValueError(
                "session_idle_ttl_seconds cannot exceed the absolute lifetime"
            )
        if self.session_rotation_seconds > self.session_absolute_ttl_seconds:
            raise ValueError(
                "session_rotation_seconds cannot exceed the absolute lifetime"
            )


@dataclass(frozen=True)
class BoundaryRequest:
    """Transport facts observed by the server, never supplied by request JSON."""

    remote_addr: str | None
    scheme: str
    host: str
    origin: str | None
    proxy_marked: bool = False

    @classmethod
    def from_environ(
        cls,
        *,
        remote_addr: str | None,
        scheme: str,
        host: str,
        origin: str | None,
        headers: Mapping[str, Any] | None = None,
    ) -> "BoundaryRequest":
        lowered = {str(key).lower(): value for key, value in (headers or {}).items()}
        return cls(
            remote_addr=remote_addr,
            scheme=scheme,
            host=host,
            origin=origin,
            proxy_marked=any(bool(lowered.get(key)) for key in _PROXY_HEADERS),
        )


@dataclass(frozen=True)
class LocalPrincipal:
    actor: ActorRef
    session_id: str
    origin: str
    audience: str
    session_expires_at: float
    rotation_due_at: float

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor.to_dict(),
            "origin": self.origin,
            "audience": self.audience,
            "session_expires_at": self.session_expires_at,
            "rotation_due_at": self.rotation_due_at,
            "assurance": "enrolled_local_session",
        }


@dataclass(frozen=True)
class BootstrapGrant:
    token: str
    origin: str
    audience: str
    expires_at: float


@dataclass(frozen=True)
class SessionGrant:
    cookie_token: str
    csrf_token: str
    principal: LocalPrincipal


@dataclass(frozen=True)
class GestureGrant:
    token: str
    action: str
    subject_sha256: str
    context_sha256: str
    expires_at: float


@dataclass(frozen=True)
class HumanAuthorityContext:
    principal: LocalPrincipal
    action: str
    subject_sha256: str
    context_sha256: str
    gesture_id: str
    assurance: str = HUMAN_AUTHORITY_ASSURANCE
    basis: str = HUMAN_AUTHORITY_BASIS
    threat_model_limit: str = HUMAN_AUTHORITY_THREAT_LIMIT

    def to_input_ingress(self) -> dict[str, Any]:
        """Return durable, content-free provenance for an exact UI submission.

        This records the authenticated inputter and gesture assurance only.  It
        deliberately makes no authorship or semantic-review assertion about the
        submitted words.
        """

        return {
            "schema": HUMAN_INPUT_INGRESS_SCHEMA,
            "inputter": self.principal.actor.to_dict(),
            "session_id_sha256": sha256_text(self.principal.session_id),
            "gesture_id": self.gesture_id,
            "action": self.action,
            "subject_sha256": self.subject_sha256,
            "context_sha256": self.context_sha256,
            "assurance": self.assurance,
            "basis": self.basis,
            "threat_model_limit": self.threat_model_limit,
        }


def sha256_text(value: str) -> str:
    """Return the lowercase SHA-256 digest of exact UTF-8 text."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _token_hash(value: str, *, expected_prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(expected_prefix)
        or len(value) > 512
        or len(value) < len(expected_prefix) + 32
    ):
        raise LocalIdentityError(
            "invalid_credential", "The local credential is invalid.", status=401
        )
    return sha256_text(value)


def _new_identifier(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def _normalize_host(hostname: str) -> str:
    host = hostname.rstrip(".").lower()
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError as exc:
        raise LocalIdentityError(
            "loopback_required",
            "Human-authority actions are available only on direct loopback.",
            status=403,
        ) from exc
    if not address.is_loopback:
        raise LocalIdentityError(
            "loopback_required",
            "Human-authority actions are available only on direct loopback.",
            status=403,
        )
    return address.compressed.lower()


def normalize_loopback_origin(value: str) -> str:
    """Validate and canonicalize an exact HTTP(S) loopback origin."""

    if not isinstance(value, str) or not value or len(value) > 512:
        raise LocalIdentityError(
            "origin_required", "An exact loopback Origin is required.", status=403
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LocalIdentityError(
            "invalid_origin", "The request Origin is invalid.", status=403
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LocalIdentityError(
            "invalid_origin", "The request Origin is invalid.", status=403
        )
    host = _normalize_host(parsed.hostname)
    default = 80 if parsed.scheme == "http" else 443
    display_host = f"[{host}]" if ":" in host else host
    port_suffix = "" if port in {None, default} else f":{port}"
    return f"{parsed.scheme}://{display_host}{port_suffix}"


def _request_origin(boundary: BoundaryRequest, *, require_origin: bool) -> str:
    if boundary.proxy_marked:
        raise LocalIdentityError(
            "direct_loopback_required",
            "Proxied requests cannot exercise local human authority.",
            status=403,
        )
    remote = (boundary.remote_addr or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(remote)
    except ValueError as exc:
        raise LocalIdentityError(
            "loopback_required",
            "Human-authority actions are available only on direct loopback.",
            status=403,
        ) from exc
    if not address.is_loopback:
        raise LocalIdentityError(
            "loopback_required",
            "Human-authority actions are available only on direct loopback.",
            status=403,
        )

    try:
        host_parts = urlsplit(f"//{boundary.host}")
        request_host = host_parts.hostname
        request_port = host_parts.port
    except ValueError as exc:
        raise LocalIdentityError(
            "invalid_host", "The request Host is invalid.", status=403
        ) from exc
    if not request_host or host_parts.username is not None:
        raise LocalIdentityError(
            "invalid_host", "The request Host is invalid.", status=403
        )
    host = _normalize_host(request_host)
    scheme = str(boundary.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise LocalIdentityError(
            "invalid_origin", "The request scheme is invalid.", status=403
        )
    default = 80 if scheme == "http" else 443
    display_host = f"[{host}]" if ":" in host else host
    request_host_origin = (
        f"{scheme}://{display_host}"
        f"{'' if request_port in {None, default} else f':{request_port}'}"
    )

    if boundary.origin:
        origin = normalize_loopback_origin(boundary.origin)
        if not hmac.compare_digest(origin, request_host_origin):
            raise LocalIdentityError(
                "origin_mismatch",
                "The request Origin does not match its loopback Host.",
                status=403,
            )
        return origin
    if require_origin:
        raise LocalIdentityError(
            "origin_required", "An exact loopback Origin is required.", status=403
        )
    return request_host_origin


def _validate_identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise LocalIdentityError(
            "invalid_request", f"{field} is invalid.", status=400
        )
    return value


def _validate_digest(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise LocalIdentityError(
            "invalid_request", f"{field} must be a lowercase SHA-256 digest.", status=400
        )
    return value


class LocalIdentityAuthority:
    """Persistent authority for one enrolled local dashboard profile."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        policy: LocalIdentityPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else paths.resolve(
            "db/local-identity"
        )
        self.policy = policy or LocalIdentityPolicy()
        self._clock = clock
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_identity_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS local_bootstrap_tokens (
                    token_hash TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL
                );

                CREATE TABLE IF NOT EXISTS local_browser_sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    actor_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    idle_expires_at REAL NOT NULL,
                    absolute_expires_at REAL NOT NULL,
                    rotation_due_at REAL NOT NULL,
                    revoked_at REAL,
                    replaced_by_session_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_local_sessions_actor
                    ON local_browser_sessions(actor_id, revoked_at);

                CREATE TABLE IF NOT EXISTS local_session_csrf_tokens (
                    session_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL,
                    PRIMARY KEY(session_id, token_hash),
                    FOREIGN KEY(session_id) REFERENCES local_browser_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS local_gesture_challenges (
                    gesture_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    subject_sha256 TEXT NOT NULL,
                    context_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    FOREIGN KEY(session_id) REFERENCES local_browser_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS local_identity_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    session_id TEXT,
                    object_id TEXT,
                    outcome TEXT NOT NULL
                );
                """
            )
            conn.execute("PRAGMA user_version = 1")
            now = self._clock()
            values = {
                "schema_version": "1",
                "issuer_authority_id": _new_identifier("wia_"),
                "tenant_scope_id": _new_identifier("wts_"),
                "local_actor_id": _new_identifier("wactor_"),
                "enrolled_at": str(now),
            }
            conn.execute("BEGIN IMMEDIATE")
            try:
                for key, value in values.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO local_identity_meta(key, value) "
                        "VALUES (?, ?)",
                        (key, value),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            self._schema_ready = True

    def _meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        rows = conn.execute("SELECT key, value FROM local_identity_meta").fetchall()
        result = {str(row["key"]): str(row["value"]) for row in rows}
        required = {"issuer_authority_id", "tenant_scope_id", "local_actor_id"}
        if not required.issubset(result):
            raise RuntimeError("local identity enrollment is incomplete")
        return result

    def enrolled_actor(self) -> ActorRef:
        with self._connect() as conn:
            meta = self._meta(conn)
        return ActorRef(
            schema=ACTOR_REF_SCHEMA,
            issuer_authority_id=meta["issuer_authority_id"],
            subject=meta["local_actor_id"],
            kind="human",
            tenant_scope_id=meta["tenant_scope_id"],
        )

    def _principal(self, conn: sqlite3.Connection, row: sqlite3.Row) -> LocalPrincipal:
        meta = self._meta(conn)
        if not hmac.compare_digest(str(row["actor_id"]), meta["local_actor_id"]):
            raise LocalIdentityError(
                "principal_unavailable",
                "The enrolled local principal is unavailable.",
                status=403,
            )
        return LocalPrincipal(
            actor=ActorRef(
                schema=ACTOR_REF_SCHEMA,
                issuer_authority_id=meta["issuer_authority_id"],
                subject=meta["local_actor_id"],
                kind="human",
                tenant_scope_id=meta["tenant_scope_id"],
            ),
            session_id=str(row["session_id"]),
            origin=str(row["origin"]),
            audience=str(row["audience"]),
            session_expires_at=min(
                float(row["idle_expires_at"]), float(row["absolute_expires_at"])
            ),
            rotation_due_at=float(row["rotation_due_at"]),
        )

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        *,
        now: float,
        event_type: str,
        outcome: str,
        actor_id: str | None = None,
        session_id: str | None = None,
        object_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO local_identity_audit("
            "occurred_at, event_type, actor_id, session_id, object_id, outcome"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (now, event_type, actor_id, session_id, object_id, outcome),
        )

    def mint_bootstrap(
        self,
        *,
        origin: str,
        audience: str = DEFAULT_AUDIENCE,
        ttl_seconds: int | None = None,
    ) -> BootstrapGrant:
        """Mint a one-time grant for a trusted host launch path.

        This method is intentionally not wrapped by an HTTP mint route.
        """

        normalized_origin = normalize_loopback_origin(origin)
        audience = _validate_identifier(audience, field="audience")
        ttl = self.policy.bootstrap_ttl_seconds if ttl_seconds is None else ttl_seconds
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
            raise ValueError("ttl_seconds must be a positive integer")
        ttl = min(ttl, self.policy.bootstrap_ttl_seconds)
        now = self._clock()
        raw_token = _token("wbb_")
        digest = sha256_text(raw_token)
        with self._connect() as conn:
            meta = self._meta(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO local_bootstrap_tokens("
                    "token_hash, actor_id, origin, audience, created_at, expires_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        digest,
                        meta["local_actor_id"],
                        normalized_origin,
                        audience,
                        now,
                        now + ttl,
                    ),
                )
                self._audit(
                    conn,
                    now=now,
                    event_type="bootstrap_minted",
                    outcome="created",
                    actor_id=meta["local_actor_id"],
                    object_id=digest[:16],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return BootstrapGrant(
            token=raw_token,
            origin=normalized_origin,
            audience=audience,
            expires_at=now + ttl,
        )

    def redeem_bootstrap(
        self,
        *,
        token: str,
        boundary: BoundaryRequest,
        audience: str = DEFAULT_AUDIENCE,
    ) -> SessionGrant:
        observed_origin = _request_origin(boundary, require_origin=True)
        audience = _validate_identifier(audience, field="audience")
        digest = _token_hash(token, expected_prefix="wbb_")
        now = self._clock()
        cookie_token = _token("wbs_")
        csrf_token = _token("wbc_")
        session_id = _new_identifier("wbsession_")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM local_bootstrap_tokens WHERE token_hash = ?",
                    (digest,),
                ).fetchone()
                if row is None:
                    raise LocalIdentityError(
                        "bootstrap_unavailable",
                        "The local launch grant is unavailable.",
                        status=401,
                    )
                if row["consumed_at"] is not None:
                    raise LocalIdentityError(
                        "bootstrap_replayed",
                        "The local launch grant has already been used.",
                        status=409,
                    )
                if float(row["expires_at"]) <= now:
                    raise LocalIdentityError(
                        "bootstrap_expired",
                        "The local launch grant has expired.",
                        status=401,
                    )
                if not hmac.compare_digest(str(row["origin"]), observed_origin):
                    raise LocalIdentityError(
                        "origin_mismatch",
                        "The local launch grant is bound to a different Origin.",
                        status=403,
                    )
                if not hmac.compare_digest(str(row["audience"]), audience):
                    raise LocalIdentityError(
                        "audience_mismatch",
                        "The local launch grant is bound to a different audience.",
                        status=403,
                    )
                consumed = conn.execute(
                    "UPDATE local_bootstrap_tokens SET consumed_at = ? "
                    "WHERE token_hash = ? AND consumed_at IS NULL",
                    (now, digest),
                )
                if consumed.rowcount != 1:
                    raise LocalIdentityError(
                        "bootstrap_replayed",
                        "The local launch grant has already been used.",
                        status=409,
                    )
                absolute_expires = now + self.policy.session_absolute_ttl_seconds
                idle_expires = min(
                    now + self.policy.session_idle_ttl_seconds, absolute_expires
                )
                rotation_due = min(
                    now + self.policy.session_rotation_seconds, absolute_expires
                )
                conn.execute(
                    "INSERT INTO local_browser_sessions("
                    "session_id, token_hash, actor_id, origin, audience, "
                    "created_at, last_seen_at, idle_expires_at, "
                    "absolute_expires_at, rotation_due_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        sha256_text(cookie_token),
                        str(row["actor_id"]),
                        observed_origin,
                        audience,
                        now,
                        now,
                        idle_expires,
                        absolute_expires,
                        rotation_due,
                    ),
                )
                conn.execute(
                    "INSERT INTO local_session_csrf_tokens("
                    "session_id, token_hash, created_at"
                    ") VALUES (?, ?, ?)",
                    (session_id, sha256_text(csrf_token), now),
                )
                self._audit(
                    conn,
                    now=now,
                    event_type="bootstrap_redeemed",
                    outcome="session_created",
                    actor_id=str(row["actor_id"]),
                    session_id=session_id,
                    object_id=digest[:16],
                )
                session_row = conn.execute(
                    "SELECT * FROM local_browser_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                principal = self._principal(conn, session_row)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return SessionGrant(cookie_token, csrf_token, principal)

    def _session_row(
        self,
        conn: sqlite3.Connection,
        *,
        cookie_token: str,
        boundary: BoundaryRequest,
        csrf_token: str | None,
        require_csrf: bool,
        require_origin: bool | None = None,
        allow_rotation_due: bool,
        touch: bool,
    ) -> sqlite3.Row:
        observed_origin = _request_origin(
            boundary,
            require_origin=require_csrf if require_origin is None else require_origin,
        )
        digest = _token_hash(cookie_token, expected_prefix="wbs_")
        row = conn.execute(
            "SELECT * FROM local_browser_sessions WHERE token_hash = ?", (digest,)
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise LocalIdentityError(
                "session_unavailable", "The local session is unavailable.", status=401
            )
        now = self._clock()
        if (
            float(row["idle_expires_at"]) <= now
            or float(row["absolute_expires_at"]) <= now
        ):
            conn.execute(
                "UPDATE local_browser_sessions SET revoked_at = COALESCE(revoked_at, ?) "
                "WHERE session_id = ?",
                (now, str(row["session_id"])),
            )
            raise LocalIdentityError(
                "session_expired", "The local session has expired.", status=401
            )
        if not hmac.compare_digest(str(row["origin"]), observed_origin):
            raise LocalIdentityError(
                "origin_mismatch",
                "The local session is bound to a different Origin.",
                status=403,
            )
        if require_csrf:
            supplied = _token_hash(csrf_token or "", expected_prefix="wbc_")
            csrf_row = conn.execute(
                "SELECT token_hash FROM local_session_csrf_tokens "
                "WHERE session_id = ? AND token_hash = ?",
                (str(row["session_id"]), supplied),
            ).fetchone()
            if csrf_row is None or not hmac.compare_digest(
                str(csrf_row["token_hash"]), supplied
            ):
                raise LocalIdentityError(
                    "csrf_mismatch", "The CSRF token is invalid.", status=403
                )
            conn.execute(
                "UPDATE local_session_csrf_tokens SET last_used_at = ? "
                "WHERE session_id = ? AND token_hash = ?",
                (now, str(row["session_id"]), supplied),
            )
        if not allow_rotation_due and float(row["rotation_due_at"]) <= now:
            raise LocalIdentityError(
                "session_rotation_required",
                "The local session must be rotated before another protected action.",
                status=409,
            )
        if touch:
            idle_expires = min(
                now + self.policy.session_idle_ttl_seconds,
                float(row["absolute_expires_at"]),
            )
            conn.execute(
                "UPDATE local_browser_sessions "
                "SET last_seen_at = ?, idle_expires_at = ? WHERE session_id = ?",
                (now, idle_expires, str(row["session_id"])),
            )
            row = conn.execute(
                "SELECT * FROM local_browser_sessions WHERE session_id = ?",
                (str(row["session_id"]),),
            ).fetchone()
        return row

    def authenticate_session(
        self,
        *,
        cookie_token: str,
        boundary: BoundaryRequest,
        csrf_token: str | None = None,
        require_csrf: bool = False,
        allow_rotation_due: bool = False,
        touch: bool = True,
    ) -> LocalPrincipal:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE" if touch else "BEGIN")
            try:
                row = self._session_row(
                    conn,
                    cookie_token=cookie_token,
                    boundary=boundary,
                    csrf_token=csrf_token,
                    require_csrf=require_csrf,
                    require_origin=None,
                    allow_rotation_due=allow_rotation_due,
                    touch=touch,
                )
                principal = self._principal(conn, row)
                conn.execute("COMMIT")
                return principal
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def refresh_csrf(
        self, *, cookie_token: str, boundary: BoundaryRequest
    ) -> tuple[LocalPrincipal, str]:
        """Rotate CSRF after reload using exact-Origin loopback recovery.

        This endpoint intentionally does not require the previous CSRF value:
        browser memory may be gone after reload.  Exact Origin, Host, loopback,
        session-cookie, and response same-origin protections still apply.
        """

        new_token = _token("wbc_")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._session_row(
                    conn,
                    cookie_token=cookie_token,
                    boundary=boundary,
                    csrf_token=None,
                    require_csrf=False,
                    require_origin=True,
                    allow_rotation_due=True,
                    touch=True,
                )
                conn.execute(
                    "INSERT INTO local_session_csrf_tokens("
                    "session_id, token_hash, created_at"
                    ") VALUES (?, ?, ?)",
                    (str(row["session_id"]), sha256_text(new_token), self._clock()),
                )
                # Keep a bounded set so repeated reloads cannot grow the store
                # forever.  Several tokens remain valid for concurrent tabs.
                conn.execute(
                    "DELETE FROM local_session_csrf_tokens "
                    "WHERE session_id = ? AND token_hash NOT IN ("
                    "SELECT token_hash FROM local_session_csrf_tokens "
                    "WHERE session_id = ? ORDER BY created_at DESC LIMIT 8"
                    ")",
                    (str(row["session_id"]), str(row["session_id"])),
                )
                row = conn.execute(
                    "SELECT * FROM local_browser_sessions WHERE session_id = ?",
                    (str(row["session_id"]),),
                ).fetchone()
                principal = self._principal(conn, row)
                self._audit(
                    conn,
                    now=self._clock(),
                    event_type="csrf_refreshed",
                    outcome="rotated",
                    actor_id=principal.actor.subject,
                    session_id=principal.session_id,
                )
                conn.execute("COMMIT")
                return principal, new_token
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def rotate_session(
        self,
        *,
        cookie_token: str,
        csrf_token: str,
        boundary: BoundaryRequest,
    ) -> SessionGrant:
        new_cookie = _token("wbs_")
        new_csrf = _token("wbc_")
        new_session_id = _new_identifier("wbsession_")
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = self._session_row(
                    conn,
                    cookie_token=cookie_token,
                    boundary=boundary,
                    csrf_token=csrf_token,
                    require_csrf=True,
                    require_origin=None,
                    allow_rotation_due=True,
                    touch=False,
                )
                absolute_expires = float(old["absolute_expires_at"])
                idle_expires = min(
                    now + self.policy.session_idle_ttl_seconds, absolute_expires
                )
                rotation_due = min(
                    now + self.policy.session_rotation_seconds, absolute_expires
                )
                conn.execute(
                    "INSERT INTO local_browser_sessions("
                    "session_id, token_hash, actor_id, origin, audience, "
                    "created_at, last_seen_at, idle_expires_at, "
                    "absolute_expires_at, rotation_due_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_session_id,
                        sha256_text(new_cookie),
                        str(old["actor_id"]),
                        str(old["origin"]),
                        str(old["audience"]),
                        now,
                        now,
                        idle_expires,
                        absolute_expires,
                        rotation_due,
                    ),
                )
                conn.execute(
                    "INSERT INTO local_session_csrf_tokens("
                    "session_id, token_hash, created_at"
                    ") VALUES (?, ?, ?)",
                    (new_session_id, sha256_text(new_csrf), now),
                )
                changed = conn.execute(
                    "UPDATE local_browser_sessions "
                    "SET revoked_at = ?, replaced_by_session_id = ? "
                    "WHERE session_id = ? AND revoked_at IS NULL",
                    (now, new_session_id, str(old["session_id"])),
                )
                if changed.rowcount != 1:
                    raise LocalIdentityError(
                        "session_unavailable",
                        "The local session is unavailable.",
                        status=401,
                    )
                row = conn.execute(
                    "SELECT * FROM local_browser_sessions WHERE session_id = ?",
                    (new_session_id,),
                ).fetchone()
                principal = self._principal(conn, row)
                self._audit(
                    conn,
                    now=now,
                    event_type="session_rotated",
                    outcome="replaced",
                    actor_id=principal.actor.subject,
                    session_id=new_session_id,
                    object_id=str(old["session_id"]),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return SessionGrant(new_cookie, new_csrf, principal)

    def revoke_session(
        self,
        *,
        cookie_token: str,
        csrf_token: str,
        boundary: BoundaryRequest,
    ) -> LocalPrincipal:
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._session_row(
                    conn,
                    cookie_token=cookie_token,
                    boundary=boundary,
                    csrf_token=csrf_token,
                    require_csrf=True,
                    require_origin=None,
                    allow_rotation_due=True,
                    touch=False,
                )
                principal = self._principal(conn, row)
                conn.execute(
                    "UPDATE local_browser_sessions SET revoked_at = ? "
                    "WHERE session_id = ? AND revoked_at IS NULL",
                    (now, principal.session_id),
                )
                self._audit(
                    conn,
                    now=now,
                    event_type="session_revoked",
                    outcome="revoked",
                    actor_id=principal.actor.subject,
                    session_id=principal.session_id,
                )
                conn.execute("COMMIT")
                return principal
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def issue_gesture(
        self,
        *,
        cookie_token: str,
        csrf_token: str,
        boundary: BoundaryRequest,
        action: str,
        subject: str,
        context_sha256: str,
    ) -> tuple[LocalPrincipal, GestureGrant]:
        action = _validate_identifier(action, field="action")
        if not isinstance(subject, str) or not subject or len(subject) > 4096:
            raise LocalIdentityError(
                "invalid_request", "subject is invalid.", status=400
            )
        subject_digest = sha256_text(subject)
        context_sha256 = _validate_digest(context_sha256, field="context_sha256")
        now = self._clock()
        raw_token = _token("wbg_")
        gesture_id = _new_identifier("wbgesture_")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._session_row(
                    conn,
                    cookie_token=cookie_token,
                    boundary=boundary,
                    csrf_token=csrf_token,
                    require_csrf=True,
                    require_origin=None,
                    allow_rotation_due=False,
                    touch=True,
                )
                principal = self._principal(conn, session)
                conn.execute(
                    "INSERT INTO local_gesture_challenges("
                    "gesture_id, token_hash, session_id, action, subject_sha256, "
                    "context_sha256, created_at, expires_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        gesture_id,
                        sha256_text(raw_token),
                        principal.session_id,
                        action,
                        subject_digest,
                        context_sha256,
                        now,
                        now + self.policy.gesture_ttl_seconds,
                    ),
                )
                self._audit(
                    conn,
                    now=now,
                    event_type="gesture_issued",
                    outcome="created",
                    actor_id=principal.actor.subject,
                    session_id=principal.session_id,
                    object_id=gesture_id,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return principal, GestureGrant(
            token=raw_token,
            action=action,
            subject_sha256=subject_digest,
            context_sha256=context_sha256,
            expires_at=now + self.policy.gesture_ttl_seconds,
        )

    def authorize_human_mutation(
        self,
        *,
        cookie_token: str,
        csrf_token: str,
        gesture_token: str,
        boundary: BoundaryRequest,
        action: str,
        subject: str,
        context_sha256: str,
    ) -> HumanAuthorityContext:
        """Consume one exact gesture and return canonical human authority."""

        action = _validate_identifier(action, field="action")
        if not isinstance(subject, str) or not subject or len(subject) > 4096:
            raise LocalIdentityError(
                "invalid_request", "subject is invalid.", status=400
            )
        subject_digest = sha256_text(subject)
        context_sha256 = _validate_digest(context_sha256, field="context_sha256")
        gesture_digest = _token_hash(gesture_token, expected_prefix="wbg_")
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._session_row(
                    conn,
                    cookie_token=cookie_token,
                    boundary=boundary,
                    csrf_token=csrf_token,
                    require_csrf=True,
                    require_origin=None,
                    allow_rotation_due=False,
                    touch=True,
                )
                principal = self._principal(conn, session)
                gesture = conn.execute(
                    "SELECT * FROM local_gesture_challenges WHERE token_hash = ?",
                    (gesture_digest,),
                ).fetchone()
                if gesture is None:
                    raise LocalIdentityError(
                        "gesture_unavailable",
                        "The human-authority gesture is unavailable.",
                        status=401,
                    )
                if gesture["consumed_at"] is not None:
                    raise LocalIdentityError(
                        "gesture_replayed",
                        "The human-authority gesture has already been used.",
                        status=409,
                    )
                if float(gesture["expires_at"]) <= now:
                    raise LocalIdentityError(
                        "gesture_expired",
                        "The human-authority gesture has expired.",
                        status=401,
                    )
                expected = (
                    str(gesture["session_id"]),
                    str(gesture["action"]),
                    str(gesture["subject_sha256"]),
                    str(gesture["context_sha256"]),
                )
                actual = (
                    principal.session_id,
                    action,
                    subject_digest,
                    context_sha256,
                )
                if not all(
                    hmac.compare_digest(left, right)
                    for left, right in zip(expected, actual, strict=True)
                ):
                    raise LocalIdentityError(
                        "gesture_binding_mismatch",
                        "The gesture is bound to a different action or context.",
                        status=409,
                    )
                changed = conn.execute(
                    "UPDATE local_gesture_challenges SET consumed_at = ? "
                    "WHERE gesture_id = ? AND consumed_at IS NULL",
                    (now, str(gesture["gesture_id"])),
                )
                if changed.rowcount != 1:
                    raise LocalIdentityError(
                        "gesture_replayed",
                        "The human-authority gesture has already been used.",
                        status=409,
                    )
                self._audit(
                    conn,
                    now=now,
                    event_type="gesture_consumed",
                    outcome="authorized",
                    actor_id=principal.actor.subject,
                    session_id=principal.session_id,
                    object_id=str(gesture["gesture_id"]),
                )
                conn.execute("COMMIT")
                return HumanAuthorityContext(
                    principal=principal,
                    action=action,
                    subject_sha256=subject_digest,
                    context_sha256=context_sha256,
                    gesture_id=str(gesture["gesture_id"]),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise


_default_authority: LocalIdentityAuthority | None = None
_default_authority_lock = threading.Lock()


def _policy_from_config() -> LocalIdentityPolicy:
    try:
        from work_buddy.config import load_config

        raw = ((load_config().get("dashboard", {}) or {}).get("local_identity", {}) or {})
    except Exception:
        raw = {}
    allowed = {
        "bootstrap_ttl_seconds",
        "session_idle_ttl_seconds",
        "session_absolute_ttl_seconds",
        "session_rotation_seconds",
        "gesture_ttl_seconds",
    }
    values = {key: raw[key] for key in allowed if key in raw}
    try:
        return LocalIdentityPolicy(**values)
    except (TypeError, ValueError):
        # Security lifetime config fails to conservative, bounded defaults;
        # malformed values never disable expiry or expand the boundary.
        return LocalIdentityPolicy()


def get_default_authority() -> LocalIdentityAuthority:
    global _default_authority
    if _default_authority is None:
        with _default_authority_lock:
            if _default_authority is None:
                _default_authority = LocalIdentityAuthority(policy=_policy_from_config())
    return _default_authority


__all__ = [
    "ACTOR_REF_SCHEMA",
    "CSRF_HEADER_NAME",
    "DEFAULT_AUDIENCE",
    "GESTURE_HEADER_NAME",
    "HUMAN_AUTHORITY_ASSURANCE",
    "HUMAN_AUTHORITY_BASIS",
    "HUMAN_AUTHORITY_THREAT_LIMIT",
    "HUMAN_INPUT_INGRESS_SCHEMA",
    "SESSION_COOKIE_NAME",
    "ActorRef",
    "BootstrapGrant",
    "BoundaryRequest",
    "GestureGrant",
    "HumanAuthorityContext",
    "LocalIdentityAuthority",
    "LocalIdentityError",
    "LocalIdentityPolicy",
    "LocalPrincipal",
    "SessionGrant",
    "get_default_authority",
    "normalize_loopback_origin",
    "sha256_text",
]
