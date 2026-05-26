"""Consent system for gating sensitive work-buddy operations.

This module provides a hard programmatic stop for operations that require
user approval. The decorator raises ConsentRequired if no valid consent
exists in the cache — the function body never executes without it.

Consent is stored in a session-scoped SQLite database:
    - agents/<session>/consent.db — all consent grants (session-scoped)
    - agents/<session>/consent_audit.log — audit trail (session-scoped)

ALL grants are session-scoped — new sessions start with a clean slate.

Session routing (workflow consent):
    The MCP server runs under its own bootstrap session, which is NOT
    the agent's session. For workflow grants to be visible to auto_run
    subprocesses (which run under the agent's session), grant/revoke
    pass ``session_id=`` through to ``ConsentCache.grant/revoke`` — the
    cache opens a one-off connection to that specific session's DB.
    The conductor pins ``agent_session_id`` on the DAG and threads it
    to every grant/revoke/auto_run call. Full notes in the
    ``notifications/consent`` directions unit under "Session routing".

Three consent modes:
    - "always": long-lived (24h TTL), session-scoped
    - "temporary": time-limited via caller-specified TTL
    - "once": single-use, auto-revoked after successful execution

Consent context (nested call handling):
    When a consent-gated function is executing (consent was granted), it
    establishes a thread-local "consent context". Any inner @requires_consent
    calls see this context and pass through automatically — the outer consent
    subsumes the inner ones. This eliminates double-prompting for nested
    operations (e.g., toggle_task → write_file) without requiring manual
    bookkeeping like consent_operations lists or parallel *_raw functions.

    The context also collects which inner operations were covered (for
    audit trail and observability).

Flow:
    1. Decorated function is called
    2. If inside an active consent context → PASS THROUGH (nested call)
    3. Decorator checks session DB for valid consent
    4. If found and valid: establish consent context, execute, cleanup
       (once grants auto-revoke after success)
    5. If not found: raises ConsentRequired with operation details
    6. Caller grants consent via grant_consent() or wb_run("consent_grant", ...)
    7. Caller retries the function (DB now has valid entry)
"""

import functools
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from work_buddy.agent_session import (
    get_session_consent_db_path,
    get_session_audit_path,
)


logger = logging.getLogger(__name__)

# One-shot deprecation logger for legacy ``__workflow_consent__`` carries.
# A workflow that still relies on the legacy blanket key (rather than the
# composable ``workflow_class:`` / ``workflow_run:`` keys) trips this log
# the first time it carries each operation in a given process. We do NOT
# emit per-call to avoid log spam — one line per (operation) is enough to
# locate unconverted call sites.
_LEGACY_BLANKET_LOGGED: set[str] = set()


def _log_legacy_blanket_use_once(operation: str) -> None:
    if operation in _LEGACY_BLANKET_LOGGED:
        return
    _LEGACY_BLANKET_LOGGED.add(operation)
    _audit_log(
        "LEGACY_WORKFLOW_BLANKET_USED",
        operation,
        "deprecated: use workflow_class/workflow_run keys",
    )
    logger.info(
        "consent: legacy __workflow_consent__ carried operation=%r — "
        "convert the workflow to mint workflow_class:/workflow_run: keys",
        operation,
    )


# ---------------------------------------------------------------------------
# Consent context — thread-local nesting support
# ---------------------------------------------------------------------------
# When a consent-gated function is executing, it establishes a context.
# Inner @requires_consent calls see this context and pass through — the
# outer consent subsumes the inner ones.  This is analogous to reentrant
# locks or nested database transactions.
#
# The context also collects which inner operations were covered, for
# audit trail and the gateway's bundled notification.

class _ConsentContext(threading.local):
    """Thread-local consent execution context.

    Attributes:
        depth: Nesting depth. 0 = not inside a consented operation.
        outer_operation: The operation ID that established the context.
        covered_operations: Inner operations that passed through.
    """

    def __init__(self):
        super().__init__()
        self.depth: int = 0
        self.outer_operation: str | None = None
        self.covered_operations: list[str] = []


_consent_ctx = _ConsentContext()


# ---------------------------------------------------------------------------
# Safe-caller context — call-stack-aware consent risk reduction
# ---------------------------------------------------------------------------
# A capability decorated with ``@reduces_risk_for("some.op", "low")`` declares
# that, while IT is executing, calls to ``some.op`` should be treated as
# low-risk (and auto-pass the consent gate) even if ``some.op`` itself is
# registered as high-risk. This is the mechanism that lets read-only
# capabilities like ``task_briefing`` call ``obsidian.eval_js`` internally
# without triggering a high-risk prompt for every invocation, while DIRECT
# agent calls to ``eval_js`` still prompt as high-risk.
#
# Scope: only reductions to "low" auto-pass in this revision. A reduction
# to "moderate" falls through to the normal consent check (the user still
# sees a prompt, but at moderate instead of high). Reductions can only lower
# risk, never raise it.

class _SafeCallerContext(threading.local):
    """Thread-local stack of active safe-caller declarations.

    Each stack entry is a dict ``{operation: effective_risk}`` contributed by
    one active ``@reduces_risk_for``-decorated frame. The innermost entry for
    a given operation wins on lookup.
    """

    def __init__(self):
        super().__init__()
        self.stack: list[dict[str, str]] = []


_safe_caller_ctx = _SafeCallerContext()


# Registry of declared safe invocations for inspection / audit.
# Shape: {inner_operation: {caller_qualname: effective_risk}}
_RISK_REDUCERS: dict[str, dict[str, str]] = {}


def _active_reduced_risk(operation: str) -> str | None:
    """Return the effective risk active for ``operation`` via the safe-caller
    stack, or ``None`` if no reduction is active.

    Innermost matching entry wins.
    """
    for layer in reversed(_safe_caller_ctx.stack):
        if operation in layer:
            return layer[operation]
    return None


def list_risk_reducers() -> dict[str, dict[str, str]]:
    """Return a snapshot of all declared risk-reducing callers.

    Shape: ``{inner_operation: {caller_qualname: effective_risk}}``.
    Useful for audit / PR review of the security boundary.
    """
    return {
        op: dict(callers) for op, callers in _RISK_REDUCERS.items()
    }


def reduces_risk_for(operation: str, effective_risk: str = "low"):
    """Decorator: mark the wrapped function as a safe invoker of ``operation``.

    While the decorated function is on the call stack, any
    ``@requires_consent`` check for ``operation`` will use ``effective_risk``
    instead of the operation's original risk. When ``effective_risk == "low"``,
    the check auto-passes (no user prompt). The outer consent context is
    established so any further nested ``@requires_consent`` calls inside
    ``operation`` pass through as normal covered operations.

    This does NOT disable consent for direct calls to the primitive from
    code that is NOT wrapped by this decorator — only calls that happen
    while this function is on the stack.

    Example::

        @reduces_risk_for("obsidian.eval_js", "low")
        def daily_briefing():
            # Internal eval_js calls here are low-risk (auto-pass).
            ...

    Args:
        operation: The inner operation identifier this function declares
            itself a safe invoker of (e.g. ``"obsidian.eval_js"``).
        effective_risk: ``"low"``, ``"moderate"``, or ``"high"``. Only
            ``"low"`` auto-passes; higher values let the usual consent gate
            fire but with the reduced risk for prompt styling.
    """
    if effective_risk not in (r.value for r in Risk):
        raise ValueError(
            f"Invalid effective_risk: {effective_risk!r}. "
            f"Must be one of: {', '.join(r.value for r in Risk)}"
        )

    def decorator(fn: Callable) -> Callable:
        # Register for inspection (at decoration time, so it's visible
        # even before the first call).
        _RISK_REDUCERS.setdefault(operation, {})[fn.__qualname__] = effective_risk

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            entry = {operation: effective_risk}
            _safe_caller_ctx.stack.append(entry)
            try:
                return fn(*args, **kwargs)
            finally:
                _safe_caller_ctx.stack.pop()

        return wrapper

    return decorator


def in_consent_context() -> bool:
    """Check if the current thread is inside a consented operation."""
    return _consent_ctx.depth > 0


def get_consent_context_info() -> dict[str, Any] | None:
    """Return info about the active consent context, or None if not in one.

    Useful for observability and the gateway's notification enrichment.
    """
    if _consent_ctx.depth == 0:
        return None
    return {
        "depth": _consent_ctx.depth,
        "outer_operation": _consent_ctx.outer_operation,
        "covered_operations": list(_consent_ctx.covered_operations),
    }


# ---------------------------------------------------------------------------
# User-initiated consent context — the click IS the consent
# ---------------------------------------------------------------------------
#
# Most ``@requires_consent`` gates exist because work-buddy's agents act
# autonomously: cron-fired LLM calls, sidecar scans, background workflows.
# The user isn't watching, so a moderate/high-risk operation needs explicit
# permission before it fires.
#
# But UI endpoints are the inverse case: the user just clicked Submit on a
# form. Pre-emptively prompting them to grant consent for the action they
# explicitly initiated is bureaucratic UX — they already consented by
# clicking. The endpoint is the consent boundary.
#
# ``user_initiated`` is the context manager for that case. Wrap the
# critical section of a UI-driven Flask endpoint (or any code path
# directly attributable to a user action) and nested ``@requires_consent``
# calls pass through, with an audit-log entry recording the originating
# action. It does NOT lower risk for OTHER threads or background workers
# — the context is thread-local.
#
# Use sparingly. The right callers are: dashboard POST handlers that the
# user reached via a button click; CLI scripts the user invoked
# explicitly; slash-command handlers. Do NOT use this in code that an
# agent can reach without a user click — that defeats the consent model.

from contextlib import contextmanager


@contextmanager
def user_initiated(operation: str):
    """Mark a block as a user-initiated consent boundary.

    Inside the block, ``@requires_consent``-gated calls pass through:
    the user's UI action (button click, slash-command invocation, …) is
    the consent. The audit log records ``USER_INITIATED`` with the
    operation name and the inner operations that passed through.

    Args:
        operation: A short identifier for the user action — e.g.
            ``"dashboard.review_submit"``, ``"cli.flag_density"``.
            Shows up in audit logs so operations triggered through
            this path are distinguishable from autonomous ones.

    Example::

        @app.post("/api/review/execute")
        def api_review_execute():
            decisions = request.get_json()
            with user_initiated("dashboard.review_submit"):
                executed = execute_triage_decisions(decisions, presentation)
            return jsonify(executed)

    Reentrant: nested ``user_initiated`` blocks are fine; each adds a
    layer to the depth counter. Only the outermost frame writes the
    summary audit entry.
    """
    prev_depth = _consent_ctx.depth
    prev_outer = _consent_ctx.outer_operation
    prev_covered = _consent_ctx.covered_operations

    _consent_ctx.depth = prev_depth + 1
    _consent_ctx.outer_operation = operation
    _consent_ctx.covered_operations = []
    _audit_log(
        "USER_INITIATED", operation,
        "ui_action_treated_as_consent",
    )
    try:
        yield
    finally:
        covered = list(_consent_ctx.covered_operations)
        if covered:
            _audit_log(
                "USER_INITIATED_COVERED", operation,
                f"inner_ops={','.join(covered)}",
            )
        _consent_ctx.depth = prev_depth
        _consent_ctx.outer_operation = prev_outer
        _consent_ctx.covered_operations = prev_covered


class Risk(str, Enum):
    """Risk levels for consent-gated operations."""
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class ConsentRequired(Exception):
    """Raised when a function requires user consent to proceed.

    Attributes:
        operation: Unique identifier for the operation.
        reason: Human-readable explanation of what the operation does.
        risk: Risk level — "low", "moderate", or "high".
        default_ttl: Suggested TTL in minutes for temporary grants.
    """

    def __init__(
        self,
        operation: str,
        reason: str,
        risk: str,
        default_ttl: int,
    ):
        self.operation = operation
        self.reason = reason
        self.risk = risk
        self.default_ttl = default_ttl
        super().__init__(
            f"ConsentRequired: '{operation}' ({risk} risk)\n"
            f"Reason: {reason}\n"
            f"Suggested TTL: {default_ttl} minutes\n"
            f"\n"
            f"To proceed, call:\n"
            f"  grant_consent('{operation}', mode='always')\n"
            f"  OR\n"
            f"  grant_consent('{operation}', mode='temporary', "
            f"ttl_minutes={default_ttl})\n"
            f"  OR\n"
            f"  grant_consent('{operation}', mode='once')"
        )


class ConsentCache:
    """Session-scoped consent cache backed by SQLite.

    All grants live in agents/<session>/consent.db. No global tier —
    every session starts with a clean slate.

    Workflow consent: When a workflow is active, a blanket grant
    (``__workflow_consent__``) covers all operations unless a step
    explicitly opts out via ``requires_individual_consent: true``.
    """

    # "Always" grants expire after 24 hours
    _ALWAYS_TTL_HOURS = 24

    # Sentinel operation for workflow-level blanket consent
    WORKFLOW_CONSENT_OP = "__workflow_consent__"

    def __init__(self):
        self._db_path: Path | None = None
        self._initialized = False

    def _get_db_path(self) -> Path:
        if self._db_path is None:
            self._db_path = get_session_consent_db_path()
        return self._db_path

    def _connect(self, session_id: str | None = None) -> sqlite3.Connection:
        """Open a connection and ensure the schema exists.

        When ``session_id`` is provided, opens the connection against
        that specific session's ``consent.db`` — bypassing the instance
        cache. This is needed for writes/reads that must land in the
        agent's session DB (e.g. the workflow blanket grant), rather
        than whichever session the MCP server process itself happens
        to be running as.
        """
        if session_id:
            from work_buddy.agent_session import (
                get_session_consent_db_path, get_session_dir,
            )
            db_path = get_session_consent_db_path(get_session_dir(session_id))
        else:
            db_path = self._get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        # Schema is cheap-idempotent; always ensure it (we may be opening a
        # DB that this cache instance hasn't seen before when session_id is
        # supplied). The _initialized flag only tracks the default path.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS grants (
                operation TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                granted_at TEXT NOT NULL,
                expires_at TEXT
            )
        """)
        conn.commit()
        if session_id is None:
            self._initialized = True
        return conn

    def is_granted(
        self,
        operation: str,
        *,
        consent_weight: str = "low",
    ) -> bool:
        """Check if a valid consent exists for the operation.

        Checks in order:
        1. Per-operation grant (explicit consent for this exact operation)
           in the CURRENT session's DB
        2. (When ``consent_weight != "high"``) composable workflow grants:
           any unexpired ``workflow_run:*`` or ``workflow_class:*`` key in
           the current session's DB
        3. (When ``consent_weight != "high"``) legacy
           ``__workflow_consent__`` blanket — deprecation-logged once per
           operation per process
        4. (Fix-a) If none found AND
           ``work_buddy.agent_session.get_originating_session()`` returns
           a session ID different from the current one, re-check step 1
           against THAT session's DB. Steps 2 and 3 are deliberately
           skipped on the originating-session fallback: workflow grants
           do not time-travel across the sidecar retry queue — replays
           only ride individual op grants.

        ``consent_weight``:
            High-weight operations (e.g. destructive writes, irreversible
            external sends) bypass the workflow-grant carry. The per-op
            consent gate always fires for them, even inside an approved
            workflow run. This mirrors Cursor's destructive-command
            carve-out and OpenAI's ``isConsequential`` flag.

        Revocation semantics preserved: if the user revokes the grant
        in their session, the originating-session lookup also finds
        nothing → returns False → caller gets ConsentRequired.
        """
        # Step 1+2+3: current session.
        if self._is_granted_in_session(
            operation, session_id=None, consent_weight=consent_weight,
        ):
            return True

        # Step 4: originating session, if set and different.
        try:
            from work_buddy.agent_session import (
                get_originating_session, _get_session_id,
            )
            originating = get_originating_session()
        except ImportError:  # pragma: no cover — defensive
            return False
        if not originating:
            return False
        # Don't re-check the same DB.
        try:
            current_sid = _get_session_id()
        except Exception:
            current_sid = None
        if originating == current_sid:
            return False

        try:
            granted = self._is_granted_in_session(
                operation,
                session_id=originating,
                consent_weight=consent_weight,
                from_originating=True,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "is_granted: originating-session lookup failed for "
                "operation=%r session=%r: %s",
                operation, originating[:8], exc,
            )
            return False
        if granted:
            _audit_log(
                "GRANT_FROM_ORIGINATING", operation,
                f"originating_session={originating[:8]}",
            )
        return granted

    def _is_granted_in_session(
        self,
        operation: str,
        *,
        session_id: str | None,
        consent_weight: str = "low",
        from_originating: bool = False,
    ) -> bool:
        """Check grant matching for one session's DB.

        ``from_originating`` enables retry-queue isolation: when True (the
        sidecar's originating-session fallback path), workflow grants
        (both composable keys and the legacy blanket) are skipped
        entirely. Only an individual op grant satisfies a replay. This
        prevents workflow grants from time-traveling across the retry-
        queue boundary.
        """
        conn = self._connect(session_id=session_id)
        try:
            now = datetime.now(timezone.utc).isoformat()

            # ── Step 1: individual op grant ──
            row = conn.execute(
                """SELECT 1 FROM grants
                   WHERE operation = ?
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (operation, now),
            ).fetchone()
            if row:
                return True

            # Workflow-grant carry is suppressed on the retry-queue
            # replay path (``from_originating=True``) and for
            # ``consent_weight == "high"`` operations.
            workflow_carry_allowed = (
                not from_originating
                and consent_weight != "high"
                and operation != self.WORKFLOW_CONSENT_OP
                and not operation.startswith(WORKFLOW_CLASS_PREFIX)
                and not operation.startswith(WORKFLOW_RUN_PREFIX)
            )

            if workflow_carry_allowed:
                # ── Step 2: composable workflow grants ──
                # Any unexpired ``workflow_run:*`` or ``workflow_class:*``
                # key in this session authorizes the call. We log which
                # key matched via the diagnostic helper, but the boolean
                # answer is all the decorator needs here — the decorator
                # calls ``diagnose_carry`` separately for audit detail.
                wf_row = conn.execute(
                    """SELECT 1 FROM grants
                       WHERE (operation LIKE 'workflow_run:%'
                              OR operation LIKE 'workflow_class:%')
                         AND (expires_at IS NULL OR expires_at > ?)
                       LIMIT 1""",
                    (now,),
                ).fetchone()
                if wf_row:
                    return True

                # ── Step 3: legacy ``__workflow_consent__`` fallback ──
                # Single deprecation-log line per operation per process so
                # we can grep for unconverted call sites without spam.
                legacy_row = conn.execute(
                    """SELECT 1 FROM grants
                       WHERE operation = ?
                         AND (expires_at IS NULL OR expires_at > ?)""",
                    (self.WORKFLOW_CONSENT_OP, now),
                ).fetchone()
                if legacy_row:
                    _log_legacy_blanket_use_once(operation)
                    return True

            # Clean up expired entries lazily — only on the current
            # session's DB (don't mutate other sessions' state).
            if session_id is None:
                conn.execute(
                    "DELETE FROM grants WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (now,),
                )
                conn.commit()
            return False
        finally:
            conn.close()

    def diagnose_carry(
        self,
        operation: str,
        *,
        session_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Identify which kind of grant currently carries ``operation``.

        Returns ``(source, matched_key)`` where ``source`` is one of:
        ``"individual"``, ``"workflow_run"``, ``"workflow_class"``,
        ``"legacy_blanket"``, or ``"none"`` (no grant). ``matched_key``
        is the actual grant key that matched (or ``None`` for
        ``"none"``).

        Used by the decorator's audit-log emission to record *why* a
        call was authorized (``via=workflow_run:task-new:wf_abc``,
        etc.).  Strict best-effort — never raises.
        """
        try:
            conn = self._connect(session_id=session_id)
        except Exception:  # pragma: no cover — defensive
            return ("none", None)
        try:
            now = datetime.now(timezone.utc).isoformat()
            # Individual op grant has highest priority.
            row = conn.execute(
                """SELECT 1 FROM grants
                   WHERE operation = ?
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (operation, now),
            ).fetchone()
            if row:
                return ("individual", operation)
            # Composable workflow grants — prefer run-level when both
            # are present (more specific scope).
            wf_run = conn.execute(
                """SELECT operation FROM grants
                   WHERE operation LIKE 'workflow_run:%'
                     AND (expires_at IS NULL OR expires_at > ?)
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if wf_run:
                return ("workflow_run", wf_run[0])
            wf_cls = conn.execute(
                """SELECT operation FROM grants
                   WHERE operation LIKE 'workflow_class:%'
                     AND (expires_at IS NULL OR expires_at > ?)
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if wf_cls:
                return ("workflow_class", wf_cls[0])
            # Legacy blanket — last resort.
            legacy = conn.execute(
                """SELECT operation FROM grants
                   WHERE operation = ?
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (self.WORKFLOW_CONSENT_OP, now),
            ).fetchone()
            if legacy:
                return ("legacy_blanket", legacy[0])
            return ("none", None)
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover — defensive
                pass

    def get_mode(self, operation: str) -> str | None:
        """Return the mode of a grant, or None if not found/expired.

        Mirrors :meth:`is_granted`'s originating-session fallback so the
        retry-replay path can correctly distinguish ``"once"`` /
        ``"temporary"`` / ``"always"`` modes when the grant lives in
        the originating session's DB.
        """
        mode = self._get_mode_in_session(operation, session_id=None)
        if mode is not None:
            return mode

        try:
            from work_buddy.agent_session import (
                get_originating_session, _get_session_id,
            )
            originating = get_originating_session()
        except ImportError:  # pragma: no cover — defensive
            return None
        if not originating:
            return None
        try:
            current_sid = _get_session_id()
        except Exception:
            current_sid = None
        if originating == current_sid:
            return None
        try:
            return self._get_mode_in_session(
                operation, session_id=originating,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "get_mode: originating-session lookup failed: %s", exc,
            )
            return None

    def _get_mode_in_session(
        self, operation: str, *, session_id: str | None,
    ) -> str | None:
        """``get_mode`` scoped to a specific session (or current when None)."""
        conn = self._connect(session_id=session_id)
        try:
            now = datetime.now(timezone.utc).isoformat()
            row = conn.execute(
                """SELECT mode FROM grants
                   WHERE operation = ?
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (operation, now),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def grant(
        self,
        operation: str,
        mode: str,
        ttl_minutes: int | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        """Grant consent for an operation (all grants are session-scoped).

        mode="always": 24h TTL, session-scoped.
        mode="temporary": caller-specified TTL, session-scoped.
        mode="once": no expiry (revoked programmatically after execution).

        When ``session_id`` is given, the grant is written to that
        specific session's DB (bypassing the instance cache). This is
        how workflow blanket grants are routed into the agent's DB so
        auto_run subprocesses (which read the agent's DB) can see them.
        """
        now = datetime.now(timezone.utc)

        if mode == "always":
            expires_at = (now + timedelta(hours=self._ALWAYS_TTL_HOURS)).isoformat()
        elif mode == "temporary":
            if ttl_minutes is None:
                raise ValueError("ttl_minutes is required for temporary consent")
            expires_at = (now + timedelta(minutes=ttl_minutes)).isoformat()
        elif mode == "once":
            expires_at = None
        else:
            raise ValueError(
                f"Invalid mode: {mode}. Must be 'always', 'temporary', or 'once'."
            )

        conn = self._connect(session_id=session_id)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO grants (operation, mode, granted_at, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (operation, mode, now.isoformat(), expires_at),
            )
            conn.commit()
        finally:
            conn.close()

    def revoke(self, operation: str, *, session_id: str | None = None) -> None:
        """Revoke consent for an operation.

        When ``session_id`` is given, the revoke targets that specific
        session's DB — needed to undo grants that were written there
        (e.g. the workflow blanket on the agent's DB).
        """
        conn = self._connect(session_id=session_id)
        try:
            conn.execute("DELETE FROM grants WHERE operation = ?", (operation,))
            conn.commit()
        finally:
            conn.close()

    def list_all(self) -> dict[str, Any]:
        """Return all consent entries, marking expired ones."""
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT operation, mode, granted_at, expires_at FROM grants"
            ).fetchall()
        finally:
            conn.close()

        result = {}
        for operation, mode, granted_at, expires_at in rows:
            entry: dict[str, Any] = {
                "mode": mode,
                "granted_at": granted_at,
            }
            if expires_at:
                entry["expires_at"] = expires_at
                try:
                    expiry = datetime.fromisoformat(expires_at)
                    entry["expired"] = now >= expiry
                except ValueError:
                    entry["expired"] = True
            result[operation] = entry

        return result


def _audit_log(event: str, operation: str, details: str = "") -> None:
    """Append an entry to the session's audit log."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{now} | {event} | {operation}"
    if details:
        line += f" | {details}"
    try:
        audit_path = get_session_audit_path()
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # Don't let audit failures block operations


# Module-level cache instance
_cache = ConsentCache()

# Registry of consent metadata — populated at import time by @requires_consent.
# Maps operation ID → {reason, risk, default_ttl}.
# Used by the gateway's auto-request logic to build rich notification bodies.
_CONSENT_REGISTRY: dict[str, dict[str, Any]] = {}


def get_consent_metadata(operation: str) -> dict[str, Any] | None:
    """Look up metadata for a consent-gated operation.

    Returns {reason, risk, default_ttl} or None if the operation
    hasn't been registered (i.e., no @requires_consent has been
    imported for it yet).
    """
    return _CONSENT_REGISTRY.get(operation)


def requires_consent(
    operation: str,
    reason: str,
    risk: str = "moderate",
    default_ttl: int = 5,
    body_extras: Callable[[], str] | None = None,
    consent_weight: str | None = None,
):
    """Decorator that gates a function on user consent.

    Consent context (nesting):
        If this function is called from within an already-consented operation,
        the check is skipped and the call passes through automatically. The
        outer consent subsumes the inner one. The pass-through is recorded in
        the audit trail and the context's covered_operations list.

    For top-level calls:
        The function body will NEVER execute without a valid consent entry
        in the cache. If no consent exists, ConsentRequired is raised.

    For mode="once" grants, the grant is auto-revoked after the function
    executes successfully. If the function raises, the grant is preserved
    so the caller can retry.

    Args:
        operation: Unique identifier for this operation.
        reason: Human-readable explanation shown to the user.
        risk: "low", "moderate", or "high" (validated against Risk enum).
        default_ttl: Suggested TTL in minutes for temporary grants.
        body_extras: Optional no-arg callable returning a string that is
            appended to the consent prompt body just under the static
            ``reason`` line. Use to surface dynamic context (e.g. counts,
            sample titles) that grounds an otherwise abstract approval
            decision. The callable runs at consent-request time inside a
            best-effort try/except in the gateway — exceptions are logged
            and skipped, never blocking the prompt. Must be cheap (single
            file read, no network); the user is waiting.
        consent_weight: ``"low"`` (default — derived from ``risk``),
            ``"moderate"``, or ``"high"``. Controls whether workflow-level
            grants (``workflow_class:`` / ``workflow_run:`` keys) may
            carry the call without surfacing a per-op prompt. A
            ``"high"`` weight bypasses workflow-grant carry: the per-op
            consent gate fires even inside an approved workflow run.
            When omitted, the weight defaults to mirror ``risk`` so
            existing call sites that did not specify a weight retain the
            sensible behavior: high-risk operations get high-weight
            treatment, moderate-risk get moderate-weight, etc.
    """
    # Default consent_weight to mirror risk when not explicitly set.
    # This keeps existing call sites — which never passed consent_weight
    # — calibrated to their declared risk.
    effective_weight = consent_weight if consent_weight is not None else risk

    # Register metadata for gateway auto-request lookup
    _CONSENT_REGISTRY[operation] = {
        "reason": reason,
        "risk": risk,
        "default_ttl": default_ttl,
        "body_extras": body_extras,
        "consent_weight": effective_weight,
    }

    # Validate risk at decoration time (fail-fast on typos)
    if risk not in (r.value for r in Risk):
        raise ValueError(
            f"Invalid risk value: {risk!r}. "
            f"Must be one of: {', '.join(r.value for r in Risk)}"
        )
    if effective_weight not in (r.value for r in Risk):
        raise ValueError(
            f"Invalid consent_weight value: {effective_weight!r}. "
            f"Must be one of: {', '.join(r.value for r in Risk)}"
        )

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # ── Nested call: pass through if inside a consent context ──
            if _consent_ctx.depth > 0:
                _consent_ctx.covered_operations.append(operation)
                _audit_log(
                    "PASS_THROUGH", operation,
                    f"nested_in:{_consent_ctx.outer_operation}",
                )
                return fn(*args, **kwargs)

            # ── Call-stack-aware risk reduction ──
            # If a declared-safe caller is active for this operation and the
            # reduced risk is "low", auto-pass the gate. The outer frame
            # becomes an implicit consent context so deeper @requires_consent
            # calls nested inside this operation pass through normally.
            reduced_risk = _active_reduced_risk(operation)
            if reduced_risk == Risk.LOW.value:
                _audit_log(
                    "RISK_REDUCED_PASS", operation,
                    f"safe_caller:{reduced_risk}",
                )
                prev_depth = _consent_ctx.depth
                prev_outer = _consent_ctx.outer_operation
                prev_covered = _consent_ctx.covered_operations
                _consent_ctx.depth = prev_depth + 1
                _consent_ctx.outer_operation = operation
                _consent_ctx.covered_operations = []
                try:
                    return fn(*args, **kwargs)
                finally:
                    _consent_ctx.depth = prev_depth
                    _consent_ctx.outer_operation = prev_outer
                    _consent_ctx.covered_operations = prev_covered

            # ── Top-level: check consent cache ──
            if _cache.is_granted(operation, consent_weight=effective_weight):
                # Determine consent source for audit. The composable
                # consent model recognizes four carry shapes —
                # ``individual`` / ``workflow_run`` / ``workflow_class``
                # / ``legacy_blanket`` — and ``diagnose_carry`` returns
                # which one matched plus the actual key for traceability.
                carry_source, carry_key = _cache.diagnose_carry(operation)
                if carry_source == "individual":
                    op_mode = _cache.get_mode(operation)
                    is_once = op_mode == "once"
                    _audit_log("EXECUTED", operation, "via=individual")
                elif carry_source == "workflow_run":
                    is_once = False
                    _audit_log(
                        "EXECUTED", operation,
                        f"via=workflow_run | key={carry_key}",
                    )
                elif carry_source == "workflow_class":
                    is_once = False
                    _audit_log(
                        "EXECUTED", operation,
                        f"via=workflow_class | key={carry_key}",
                    )
                elif carry_source == "legacy_blanket":
                    is_once = False
                    _audit_log(
                        "EXECUTED", operation,
                        "via=legacy_blanket (deprecated)",
                    )
                else:
                    # is_granted returned True but diagnose_carry did
                    # not find a matching key — likely the
                    # originating-session fallback path. Record
                    # generically.
                    is_once = False
                    _audit_log(
                        "EXECUTED", operation, "via=originating_session",
                    )

                # Enter consent context — inner @requires_consent will
                # pass through for the duration of this execution.
                prev_depth = _consent_ctx.depth
                prev_outer = _consent_ctx.outer_operation
                prev_covered = _consent_ctx.covered_operations

                _consent_ctx.depth = prev_depth + 1
                _consent_ctx.outer_operation = operation
                _consent_ctx.covered_operations = []
                try:
                    result = fn(*args, **kwargs)
                finally:
                    # Capture covered ops before restoring context
                    covered = list(_consent_ctx.covered_operations)
                    if covered:
                        _audit_log(
                            "CONTEXT_COVERED", operation,
                            f"inner_ops={','.join(covered)}",
                        )
                    # Restore previous context (supports re-entrant nesting)
                    _consent_ctx.depth = prev_depth
                    _consent_ctx.outer_operation = prev_outer
                    _consent_ctx.covered_operations = prev_covered

                # Auto-revoke "once" grants after successful execution.
                # Also revoke any inner operations that passed through —
                # they were granted as part of the same bundled consent,
                # so they should be revoked together.  This ensures the
                # next call triggers a full bundled notification again.
                if is_once:
                    _cache.revoke(operation)
                    _audit_log("AUTO_REVOKED", operation, "once_grant_consumed")
                    for inner_op in covered:
                        inner_mode = _cache.get_mode(inner_op)
                        if inner_mode == "once":
                            _cache.revoke(inner_op)
                            _audit_log(
                                "AUTO_REVOKED", inner_op,
                                f"covered_by_once:{operation}",
                            )

                return result

            # No valid consent — raise. If a safe caller declared a
            # reduced risk (not "low" — that would have auto-passed above),
            # propagate that risk to the prompt so the user sees the
            # caller-contextualized severity.
            effective_risk = reduced_risk if reduced_risk is not None else risk
            _audit_log("BLOCKED", operation, f"no_consent risk={effective_risk}")
            raise ConsentRequired(
                operation=operation,
                reason=reason,
                risk=effective_risk,
                default_ttl=default_ttl,
            )
        return wrapper
    return decorator


def grant_consent(
    operation: str,
    mode: str = "always",
    ttl_minutes: int | None = None,
    *,
    session_id: str | None = None,
) -> None:
    """Grant consent for an operation.

    Args:
        operation: The operation identifier.
        mode: "always" (permanent), "temporary" (time-limited), or "once" (single-use).
        ttl_minutes: Expiry in minutes for "temporary" mode. Required for temporary,
                     ignored for always/once.
        session_id: When given, write the grant to THIS specific session's
            consent DB instead of the calling process's. The sidecar uses
            this when an out-of-band ``consent_grant`` message arrives so
            the grant lands in the originating agent's DB, not the
            sidecar's. Mirrors the existing ``ConsentCache.grant`` keyword.
    """
    _cache.grant(operation, mode, ttl_minutes=ttl_minutes, session_id=session_id)
    details = f"{mode}"
    if mode == "temporary":
        details += f" | ttl={ttl_minutes}m"
    if session_id:
        details += f" | session={session_id[:8]}"
    _audit_log("GRANTED", operation, details)


def grant_consent_batch(
    operations: list[str],
    mode: str = "always",
    ttl_minutes: int | None = None,
    *,
    session_id: str | None = None,
) -> None:
    """Grant consent for multiple operations at once.

    Used by the gateway's auto-consent flow to write grants for all
    operations in a bundled consent request after a single user approval.

    ``session_id`` is plumbed through the same way as ``grant_consent`` —
    the sidecar's out-of-band consent_grant path uses it to route batched
    bundle grants to the originating agent's DB.
    """
    for op in operations:
        grant_consent(op, mode=mode, ttl_minutes=ttl_minutes, session_id=session_id)


def revoke_consent(operation: str) -> None:
    """Revoke consent for an operation."""
    _cache.revoke(operation)
    _audit_log("REVOKED", operation)


def list_consents() -> dict[str, Any]:
    """List all consent entries with status."""
    return _cache.list_all()


# ---------------------------------------------------------------------------
# Workflow consent — blanket grants for active workflows
# ---------------------------------------------------------------------------

_WORKFLOW_DEFAULT_TTL_MINUTES = 180  # 3 hours


def grant_workflow_consent(
    workflow_run_id: str,
    ttl_minutes: int = _WORKFLOW_DEFAULT_TTL_MINUTES,
    *,
    session_id: str | None = None,
) -> None:
    """Grant blanket consent for all operations during a workflow run.

    When active, ``@requires_consent`` checks pass for ANY operation
    (unless the step explicitly opts out). The grant expires after
    *ttl_minutes* or when explicitly revoked at workflow completion.

    Args:
        workflow_run_id: For audit trail only.
        ttl_minutes: How long the blanket lasts (default 3h).
        session_id: When given, the grant is written to that session's
            consent DB instead of the MCP server's default. This is how
            workflow blankets land in the agent's DB so auto_run
            subprocesses (running under the agent's session) can see
            them.
    """
    _cache.grant(
        ConsentCache.WORKFLOW_CONSENT_OP,
        mode="temporary",
        ttl_minutes=ttl_minutes,
        session_id=session_id,
    )
    _audit_log(
        "WORKFLOW_CONSENT_GRANTED",
        ConsentCache.WORKFLOW_CONSENT_OP,
        f"workflow={workflow_run_id} | ttl={ttl_minutes}m"
        + (f" | session={session_id}" if session_id else ""),
    )


def revoke_workflow_consent(
    workflow_run_id: str = "",
    *,
    session_id: str | None = None,
) -> None:
    """Revoke the workflow blanket consent (called on workflow completion).

    When ``session_id`` is given, the revoke targets that session's DB —
    mirroring the symmetric grant so we don't leave stale blankets
    behind in agents' DBs.
    """
    try:
        _cache.revoke(ConsentCache.WORKFLOW_CONSENT_OP, session_id=session_id)
        _audit_log(
            "WORKFLOW_CONSENT_REVOKED",
            ConsentCache.WORKFLOW_CONSENT_OP,
            f"workflow={workflow_run_id}" if workflow_run_id else "",
        )
    except Exception:
        pass  # Already revoked or never granted — no-op


def is_workflow_consent_active(*, session_id: str | None = None) -> bool:
    """Check if there's an active workflow blanket consent.

    When ``session_id`` is given, the check is scoped to that specific
    session's ``consent.db`` and skips the originating-session fallback.
    The conductor's orphan-reconciliation sweep needs a check bound to
    exactly one session — a blanket left in session A's DB must not be
    deemed "active" because session B (the originating session) still
    holds one.
    """
    return _cache._is_granted_in_session(
        ConsentCache.WORKFLOW_CONSENT_OP, session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Composable workflow consent — class + run grant keys
# ---------------------------------------------------------------------------
# Two grant levels coexist with the legacy ``__workflow_consent__`` blanket:
#
#   ``workflow_class:<name>``         — class-level trust for a workflow
#                                       (the "Allow for 15 min" option).
#                                       TTL-bounded; bounded re-invocation
#                                       within the window reuses the grant.
#
#   ``workflow_run:<name>:<run_id>``  — authorization for a single in-flight
#                                       run. Has no TTL; revoked at run
#                                       completion or cascade-revoked when
#                                       the class grant is explicitly revoked.
#
# The decorator's check path (in ``_is_granted_in_session``) consults these
# keys via a single ``LIKE 'workflow_run:%' OR LIKE 'workflow_class:%'``
# probe — no need for the cache to introspect ``_ACTIVE_RUNS``. Lifecycle
# is enforced by the conductor: ``start_workflow`` mints the run grant;
# ``_build_complete_response`` revokes it; orphan reconciliation cleans up
# after MCP-server restarts.

WORKFLOW_CLASS_PREFIX = "workflow_class:"
WORKFLOW_RUN_PREFIX = "workflow_run:"

# Default TTLs for the class-grant prompt choices. The gateway's
# ``_auto_workflow_consent_request`` uses these for in-window approvals;
# ``resolve_consent_request`` uses them for out-of-band approvals (e.g.
# Telegram callbacks landing after the gateway's poll has exited) so the
# two paths agree on the window the user thought they were authorizing.
WORKFLOW_CLASS_TEMPORARY_TTL_MIN = 15      # "Allow for 15 min"
WORKFLOW_CLASS_ALWAYS_TTL_MIN = 24 * 60    # "Allow always (this session)" = 24h


def _workflow_class_key(workflow_name: str) -> str:
    return f"{WORKFLOW_CLASS_PREFIX}{workflow_name}"


def _workflow_run_key(workflow_name: str, run_id: str) -> str:
    return f"{WORKFLOW_RUN_PREFIX}{workflow_name}:{run_id}"


def grant_workflow_class(
    workflow_name: str,
    *,
    ttl_minutes: int,
    session_id: str | None = None,
) -> None:
    """Grant class-level consent for a workflow.

    Lives in the agent's session DB; bounded by ``ttl_minutes``. Subsequent
    invocations of the same workflow within the window check this grant
    and skip the user-facing pre-flight prompt (the workflow is "trusted
    for the session" up to TTL).
    """
    _cache.grant(
        _workflow_class_key(workflow_name),
        mode="temporary",
        ttl_minutes=ttl_minutes,
        session_id=session_id,
    )
    sid_tag = f" | session={session_id[:8]}" if session_id else ""
    _audit_log(
        "WORKFLOW_CLASS_GRANTED",
        _workflow_class_key(workflow_name),
        f"workflow={workflow_name} | ttl={ttl_minutes}m{sid_tag}",
    )


def grant_workflow_run(
    workflow_name: str,
    run_id: str,
    *,
    session_id: str | None = None,
) -> None:
    """Grant run-level consent for an in-flight workflow run.

    No TTL — revoked when the run completes (``revoke_workflow_run`` with
    ``reason="complete"``) or when the user explicitly revokes the class
    grant with cascade. Stored with ``mode="once"`` as a marker that says
    "expect explicit revocation"; the lazy-expiry path does not affect it.
    """
    _cache.grant(
        _workflow_run_key(workflow_name, run_id),
        mode="once",
        session_id=session_id,
    )
    sid_tag = f" | session={session_id[:8]}" if session_id else ""
    _audit_log(
        "WORKFLOW_RUN_GRANTED",
        _workflow_run_key(workflow_name, run_id),
        f"workflow={workflow_name} | run={run_id}{sid_tag}",
    )


def revoke_workflow_run(
    workflow_name: str,
    run_id: str,
    *,
    session_id: str | None = None,
    reason: str = "complete",
) -> None:
    """Revoke run-level consent for a workflow run.

    ``reason`` is one of: ``"complete"`` (normal lifecycle),
    ``"cascade"`` (parent class-grant revoked), ``"explicit"`` (user
    revoked this specific run), ``"ttl"`` (rare — class TTL expired
    mid-run and we narrowed). Recorded in audit log.

    Idempotent: revoking a missing run is a no-op.
    """
    try:
        _cache.revoke(
            _workflow_run_key(workflow_name, run_id), session_id=session_id,
        )
        sid_tag = f" | session={session_id[:8]}" if session_id else ""
        _audit_log(
            "WORKFLOW_RUN_REVOKED",
            _workflow_run_key(workflow_name, run_id),
            f"workflow={workflow_name} | run={run_id} | reason={reason}{sid_tag}",
        )
    except Exception:  # pragma: no cover — defensive idempotency
        pass


def revoke_workflow_class(
    workflow_name: str,
    *,
    session_id: str | None = None,
) -> None:
    """Revoke class-level consent for a workflow.

    Does NOT cascade to in-flight runs — that is the conductor's job (it
    owns ``_ACTIVE_RUNS``). The convention is:

    - Direct callers wanting cascade behavior call
      ``conductor.cascade_revoke_workflow(name, session_id=...)``, which
      invokes this function THEN walks ``_ACTIVE_RUNS`` calling
      ``revoke_workflow_run`` for each matching run.
    - Callers who only want to remove the class grant (e.g. a TTL-aware
      cleanup that does not want to interrupt in-flight runs) call this
      function directly.

    Idempotent.
    """
    try:
        _cache.revoke(
            _workflow_class_key(workflow_name), session_id=session_id,
        )
        sid_tag = f" | session={session_id[:8]}" if session_id else ""
        _audit_log(
            "WORKFLOW_CLASS_REVOKED",
            _workflow_class_key(workflow_name),
            f"workflow={workflow_name}{sid_tag}",
        )
    except Exception:  # pragma: no cover — defensive idempotency
        pass


def is_workflow_authorized(
    workflow_name: str,
    run_id: str | None = None,
    *,
    session_id: str | None = None,
) -> tuple[bool, str | None]:
    """Check whether a workflow (and optional specific run) is authorized.

    Lookup order:
        1. ``workflow_run:<name>:<run_id>`` (only when ``run_id`` is given)
        2. ``workflow_class:<name>``

    Returns ``(authorized, via)`` where ``via`` is ``"run"`` when the
    run-key matched, ``"class"`` when the class-key matched, or ``None``.
    """
    if run_id is not None:
        if _cache._is_granted_in_session(
            _workflow_run_key(workflow_name, run_id), session_id=session_id,
        ):
            return True, "run"
    if _cache._is_granted_in_session(
        _workflow_class_key(workflow_name), session_id=session_id,
    ):
        return True, "class"
    return False, None


def list_active_workflow_grants(
    *, session_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Diagnostic helper: list active workflow_class and workflow_run grants.

    Returns ``{"class": [...], "run": [...]}`` where each entry has
    ``operation``, ``mode``, ``granted_at``, ``expires_at`` (when set), and
    parsed ``workflow_name`` / ``run_id`` fields. Used by the dashboard,
    by orphan reconciliation, and by ``scripts/audit_workflow_consent.py``.
    """
    conn = _cache._connect(session_id=session_id)
    now = datetime.now(timezone.utc).isoformat()
    try:
        rows = conn.execute(
            """SELECT operation, mode, granted_at, expires_at
               FROM grants
               WHERE (operation LIKE 'workflow_class:%'
                      OR operation LIKE 'workflow_run:%')
                 AND (expires_at IS NULL OR expires_at > ?)""",
            (now,),
        ).fetchall()
    finally:
        conn.close()

    out: dict[str, list[dict[str, Any]]] = {"class": [], "run": []}
    for operation, mode, granted_at, expires_at in rows:
        entry: dict[str, Any] = {
            "operation": operation,
            "mode": mode,
            "granted_at": granted_at,
        }
        if expires_at:
            entry["expires_at"] = expires_at
        if operation.startswith(WORKFLOW_CLASS_PREFIX):
            entry["workflow_name"] = operation[len(WORKFLOW_CLASS_PREFIX):]
            out["class"].append(entry)
        elif operation.startswith(WORKFLOW_RUN_PREFIX):
            tail = operation[len(WORKFLOW_RUN_PREFIX):]
            # ``workflow_run:<name>:<run_id>`` — split on the LAST colon so
            # workflow names containing colons (none today, but possible)
            # don't get garbled.
            if ":" in tail:
                wf_name, run_id = tail.rsplit(":", 1)
            else:  # pragma: no cover — malformed key
                wf_name, run_id = tail, ""
            entry["workflow_name"] = wf_name
            entry["run_id"] = run_id
            out["run"].append(entry)
    return out


# ---------------------------------------------------------------------------
# Async consent requests — thin wrappers around notifications/
# ---------------------------------------------------------------------------

def create_consent_request(
    operation: str,
    reason: str,
    risk: str = "moderate",
    default_ttl: int = 5,
    requester: str = "unknown",
    context: dict | None = None,
    callback: dict | None = None,
    callback_session_id: str | None = None,
    surfaces: list[str] | None = None,
) -> dict:
    """Create a pending consent request for out-of-conversation approval.

    This is a consent-specific wrapper around the generic notification system.
    Creates a CHOICE-type request with consent-specific options (always,
    temporary, once, deny).

    Args:
        operation: Operation identifier (same as @requires_consent keys).
        reason: Human-readable explanation.
        risk: "low", "moderate", or "high".
        default_ttl: Suggested TTL in minutes.
        requester: Who is requesting (e.g., "sidecar:cron_cleanup", "agent:<id>").
        context: Optional metadata for the UI (shown in the Obsidian modal).
        callback: What to run on approval: {"capability": "...", "params": {...}}.
        callback_session_id: If set, resume this Claude Code session on approval.

    Returns:
        Dict with notification_id (aliased as request_id) and the full record.
    """
    from work_buddy.notifications.models import (
        Notification, ResponseType, SourceType,
    )
    from work_buddy.notifications.store import create_notification

    # Determine source type from requester string
    source_type = (
        SourceType.AGENT.value
        if requester.startswith("agent:")
        else SourceType.PROGRAMMATIC.value
    )

    notification = Notification(
        title=f"Consent: {operation}",
        body=reason,
        priority="high" if risk == "high" else "normal",
        source=requester,
        source_type=source_type,
        tags=["consent", f"risk:{risk}", f"op:{operation}"],
        response_type=ResponseType.CHOICE.value,
        choices=[
            {"key": "always", "label": "Allow always (this session, 24h)", "description": "Session-scoped, expires after 24 hours"},
            {"key": "temporary", "label": f"Allow for {default_ttl} min", "description": f"Expires after {default_ttl} minutes"},
            {"key": "once", "label": "Allow once", "description": "Auto-revoked after one execution"},
            {"key": "deny", "label": "Deny", "description": "Do not proceed"},
        ],
        callback=callback,
        callback_session_id=callback_session_id,
        surfaces=surfaces,
    )

    # Store consent-specific metadata in custom_template for the resolver
    notification.custom_template = {
        "consent_meta": {
            "operation": operation,
            "risk": risk,
            "default_ttl": default_ttl,
            "context": context,
        },
    }

    created = create_notification(notification)

    # Return a dict for backward compatibility with MCP capabilities
    result = created.to_dict()
    result["request_id"] = result["notification_id"]  # alias
    _audit_log("REQUEST_CREATED", operation,
               f"request_id={created.notification_id} | requester={requester}")
    return result


def resolve_consent_request(
    request_id: str,
    approved: bool,
    mode: str = "temporary",
    ttl_minutes: int | None = None,
) -> dict:
    """Approve or deny a pending consent request.

    Maps the user's choice to a consent grant and dispatches callbacks.

    Args:
        request_id: The notification ID to resolve.
        approved: True to approve, False to deny.
        mode: Grant mode if approved ("always", "temporary", "once").
        ttl_minutes: TTL for temporary grants.

    Returns:
        The resolved record with dispatch status.
    """
    from work_buddy.notifications.models import StandardResponse, ResponseType
    from work_buddy.notifications import store

    notification = store.get_notification(request_id)
    if notification is None:
        raise ValueError(f"Consent request not found: {request_id}")
    if notification.status not in ("pending", "delivered"):
        raise ValueError(f"Request {request_id} is already {notification.status}")

    # Map approved/mode to a choice key
    if approved:
        choice_key = mode  # "always", "temporary", or "once"
    else:
        choice_key = "deny"

    response = StandardResponse(
        response_type=ResponseType.CHOICE.value,
        value=choice_key,
        raw={"approved": approved, "mode": mode, "ttl_minutes": ttl_minutes},
        surface="direct",  # resolved programmatically, not via a UI surface
    )

    # Record the response
    store.respond_to_notification(request_id, response)

    # Extract consent metadata
    consent_meta = (notification.custom_template or {}).get("consent_meta", {})
    operation = consent_meta.get("operation", notification.title.removeprefix("Consent: "))
    default_ttl = consent_meta.get("default_ttl", 5)

    dispatch_status = None

    if approved:
        # Write the consent grant
        ttl = ttl_minutes or default_ttl
        ttl_for_grant = ttl if mode == "temporary" else None

        # Route the grant to the ORIGINATING agent's session DB when this
        # resolve runs in a different process from the agent that
        # requested consent (e.g. the sidecar handling an out-of-band
        # consent_grant message). The notification record carries
        # ``callback_session_id`` set by the gateway from the agent's
        # ``WORK_BUDDY_SESSION_ID`` at request creation.
        # ``ConsentCache.grant`` already supports the keyword for
        # workflow blanket grants — same plumbing.
        target_session = notification.callback_session_id

        grant_consent(
            operation, mode=mode, ttl_minutes=ttl_for_grant,
            session_id=target_session,
        )

        # When the operation is a bundle label (the gateway uses
        # ``bundle:<capability>`` as a notification label for multi-op
        # consent), also grant each individual op the decorators
        # actually check. The bundle key alone satisfies no
        # ``@requires_consent`` gate; the underlying ops live in
        # ``consent_meta.context.operations``. Doing the unbundle here
        # keeps the out-of-band approval path self-sufficient (it goes
        # through this resolve, not through ``_auto_consent_request``).
        context = consent_meta.get("context") or {}
        operations = context.get("operations")
        if isinstance(operations, list) and operations:
            grant_consent_batch(
                operations, mode=mode, ttl_minutes=ttl_for_grant,
                session_id=target_session,
            )

        # When the notification is the workflow-consent pre-flight
        # prompt (``context.kind == "workflow_consent"``), additionally
        # mint the ``workflow_class:<name>`` grant. Without this, an
        # out-of-band approval (Telegram callback landing after the
        # gateway's poll has exited) would write no class grant and
        # subsequent invocations of the same workflow would re-prompt
        # within the window the user thought they had authorized.
        if context.get("kind") == "workflow_consent" and mode in (
            "temporary", "always",
        ):
            workflow_name = context.get("workflow_name")
            if workflow_name:
                class_ttl = (
                    WORKFLOW_CLASS_TEMPORARY_TTL_MIN if mode == "temporary"
                    else WORKFLOW_CLASS_ALWAYS_TTL_MIN
                )
                grant_workflow_class(
                    workflow_name,
                    ttl_minutes=class_ttl,
                    session_id=target_session,
                )

        # Dispatch callback
        dispatch_status = store.dispatch_callback(notification)

        _audit_log("REQUEST_APPROVED", operation,
                   f"request_id={request_id} | mode={mode}"
                   + (f" | dispatch={dispatch_status}" if dispatch_status else ""))
    else:
        _audit_log("REQUEST_DENIED", operation, f"request_id={request_id}")

    # Dismiss the notification on sibling surfaces. The in-window
    # gateway / dashboard / MCP-resolve paths already do this when they
    # receive the response synchronously; this branch covers the
    # out-of-band approval path (Telegram callback / Obsidian-modal
    # click landing after the in-window poll has exited) so the user
    # doesn't see the prompt linger on the surface they didn't use.
    # Best-effort: never fails the resolve.
    try:
        notif_after = store.get_notification(request_id)
        if notif_after and notif_after.delivered_surfaces:
            from work_buddy.notifications.dispatcher import SurfaceDispatcher
            dispatcher = SurfaceDispatcher.from_config()
            dispatcher.dismiss_others(
                request_id,
                responding_surface=response.surface,
                delivered_surfaces=notif_after.delivered_surfaces,
            )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "resolve_consent_request: dismiss_others failed for %s: %s",
            request_id, exc,
        )

    # Return dict for backward compatibility
    result = store.get_notification(request_id)
    out = result.to_dict() if result else {}
    out["request_id"] = request_id
    if dispatch_status:
        out["dispatch"] = dispatch_status
    return out


def get_consent_request(request_id: str) -> dict | None:
    """Get a single consent request by ID."""
    from work_buddy.notifications.store import get_notification
    n = get_notification(request_id)
    if n is None:
        return None
    result = n.to_dict()
    result["request_id"] = result["notification_id"]
    return result


def list_pending_requests() -> list[dict]:
    """List all pending consent requests."""
    from work_buddy.notifications.store import list_pending
    pending = list_pending()
    results = []
    for n in pending:
        # Filter to consent requests only (tagged with "consent")
        if "consent" in (n.tags or []):
            d = n.to_dict()
            d["request_id"] = d["notification_id"]
            results.append(d)
    return results
