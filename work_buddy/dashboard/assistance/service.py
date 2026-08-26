"""Host-bound conversation agents; frozen evidence, never live draft authority."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from work_buddy.agent_execution.presentation import project_selection_labels
from work_buddy.agent_execution.worker_outcome import WorkerExitCode
from work_buddy.conversations import execution as executions
from work_buddy.conversations import store as conversations

from .contracts import (
    PURPOSE,
    SESSION_PROTOCOL,
    AssistanceError,
    canonical,
    digest,
    form_schema,
    text_id,
    validate_control_revision,
    validate_identity,
    validate_operations,
    validate_prepared_snapshot,
)
from .execution_identity import (
    assistance_execution_session_id,
    assistance_generation_from_session,
)
from .runner import AssistanceRunner, HostedAssistanceRunner

CONSUMER = "dashboard.assisted-draft"
MAX_TURNS = 40
MAX_REFERENCE_QUERY_CHARS = 240
MAX_REFERENCE_PAYLOAD_BYTES = 32 * 1024
_SAFE_DRIVER_ERROR = (
    "AI help could not launch. Your form is unchanged. "
    "Choose Launch to try again or continue manually."
)
_AUTH_REQUIRED = "authentication_required"
_DRIVER_FAILED = "driver_failed"
logger = logging.getLogger(__name__)


def _driver_error_message(error: object, provider_id: str) -> str | None:
    """Project only known failure categories, never process diagnostics."""
    if not error:
        return None
    if error == _AUTH_REQUIRED:
        if provider_id == "claude-code":
            return (
                "Sign in to Claude Code again with claude auth login, then choose "
                "Launch. Your form is unchanged."
            )
        return (
            "Sign in to your selected chat provider again, then choose Launch. "
            "Your form is unchanged."
        )
    return _SAFE_DRIVER_ERROR


def _enabled() -> bool:
    from work_buddy.settings.broker import get_dashboard_assistance_settings

    return get_dashboard_assistance_settings().get("enabled") is True


def _read_only() -> bool:
    from work_buddy.config import load_config

    return load_config().get("dashboard", {}).get("read_only") is True


def _source_writable() -> None:
    from work_buddy.backups.source_foundation_restore import (
        require_source_foundation_writable,
    )

    require_source_foundation_writable("dashboard.assistance")


class AssistanceBroker:
    def __init__(
        self,
        *,
        runner: AssistanceRunner | None = None,
        read_only: Callable[[], bool] | None = None,
        enabled: Callable[[], bool] | None = None,
        source_writable: Callable[[], None] | None = None,
        disclosure: Any = None,
    ):
        self.runner = runner or HostedAssistanceRunner()
        self.read_only = read_only or _read_only
        self.enabled = enabled or _enabled
        self.source_writable = source_writable or _source_writable
        self.disclosure = disclosure
        self._initialized = False
        self._init_lock = threading.Lock()

    def _connection(self):
        conn = conversations.get_connection()
        if not self._initialized:
            with self._init_lock:
                if not self._initialized:
                    conn.executescript("""
                    CREATE TABLE IF NOT EXISTS assisted_draft_sessions (
                      session_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
                      request_hash TEXT NOT NULL, conversation_id TEXT NOT NULL UNIQUE,
                      binding_json TEXT NOT NULL, expires_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS assisted_draft_turns (
                      session_id TEXT NOT NULL, message_id TEXT NOT NULL,
                      snapshot_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
                      user_hash TEXT, patch_json TEXT, receipt_json TEXT,
                      state TEXT NOT NULL DEFAULT 'prepared', start_id TEXT NOT NULL DEFAULT '',
                      PRIMARY KEY(session_id, message_id),
                      FOREIGN KEY(session_id) REFERENCES assisted_draft_sessions(session_id)
                    );
                    CREATE TABLE IF NOT EXISTS assisted_draft_starts (
                      session_id TEXT NOT NULL, start_id TEXT NOT NULL,
                      request_hash TEXT NOT NULL, initial_json TEXT NOT NULL,
                      execution_json TEXT NOT NULL, authorization_ref TEXT NOT NULL,
                      history_json TEXT NOT NULL, created_at TEXT NOT NULL,
                      PRIMARY KEY(session_id,start_id)
                    );
                    CREATE TABLE IF NOT EXISTS assisted_draft_deliveries (
                      session_id TEXT NOT NULL, start_id TEXT NOT NULL,
                      generation TEXT NOT NULL, message_id TEXT NOT NULL,
                      PRIMARY KEY(session_id,start_id,generation,message_id)
                    );
                    CREATE TABLE IF NOT EXISTS assisted_draft_context_receipts (
                      session_id TEXT NOT NULL, start_id TEXT NOT NULL,
                      generation TEXT NOT NULL, message_id TEXT NOT NULL,
                      receipt_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
                      disclosed INTEGER NOT NULL DEFAULT 0, reply_message_id TEXT,
                      PRIMARY KEY(session_id,start_id,generation,message_id)
                    );
                    CREATE TABLE IF NOT EXISTS assisted_draft_reference_receipts (
                      session_id TEXT NOT NULL, start_id TEXT NOT NULL,
                      generation TEXT NOT NULL, request_id TEXT NOT NULL,
                      request_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                      disclosed INTEGER NOT NULL DEFAULT 0,
                      PRIMARY KEY(session_id,start_id,generation,request_id)
                    );
                    CREATE TABLE IF NOT EXISTS assisted_draft_supersessions (
                      session_id TEXT NOT NULL, start_id TEXT NOT NULL,
                      previous_start_id TEXT, through_message_id TEXT,
                      turn_count INTEGER NOT NULL, created_at TEXT NOT NULL,
                      PRIMARY KEY(session_id,start_id)
                    );
                    """)
                    columns = {
                        row["name"]
                        for row in conn.execute(
                            "PRAGMA table_info(assisted_draft_turns)"
                        )
                    }
                    if "start_id" not in columns:
                        conn.execute(
                            "ALTER TABLE assisted_draft_turns ADD COLUMN start_id TEXT NOT NULL DEFAULT ''"
                        )
                    conn.commit()
                    self._initialized = True
        return conn

    @contextmanager
    def _transaction(self, conn=None):
        owned = conn is None
        active = self._connection() if owned else conn
        if owned:
            active.execute("BEGIN IMMEDIATE")
        try:
            yield active
            if owned:
                active.commit()
        except Exception:
            if owned and active.in_transaction:
                active.rollback()
            raise
        finally:
            if owned:
                active.close()

    def _require_work(self) -> None:
        from work_buddy.backups.source_foundation_restore import (
            SourceFoundationRestorePending,
        )

        if self.read_only():
            raise AssistanceError(
                "dashboard_read_only",
                "AI help is paused while the dashboard is read-only.",
                403,
            )
        if not self.enabled():
            raise AssistanceError(
                "assistance_disabled",
                "Dashboard AI is disabled. Your form is unchanged.",
                403,
            )
        try:
            self.source_writable()
        except SourceFoundationRestorePending as exc:
            raise AssistanceError(
                "source_foundation_restore_pending",
                "AI help is paused until source restore reconciliation completes.",
                503,
            ) from exc

    def availability(self) -> dict[str, Any]:
        enabled = self.enabled()
        return {
            "enabled": enabled,
            "available": enabled,
            "code": "ready" if enabled else "disabled",
            "purpose": PURPOSE,
            "message": "Choose a chat model, then Launch."
            if enabled
            else "Enable Dashboard AI in Settings, or continue editing manually.",
            "disclosure": "Launch sends up to 32 KiB of allowlisted form fields plus bounded recent conversation context to your selected chat provider (at most 64 KiB per context disclosure). For Jobs, the assistant may also look up the same registered capability and workflow metadata shown in the form. It can ask questions and suggest allowlisted edits, but cannot execute, submit, create or schedule anything. Do not include secrets.",
            "localProviderNotice": "Local inference profiles do not currently provide an interactive chat driver. Only registered chat providers can be selected; there is no cloud fallback.",
        }

    def _row(
        self, conn, session_id: str, actor: str | None, *, cleanup: bool = False
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM assisted_draft_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None or (actor is not None and row["actor_id"] != actor):
            raise AssistanceError("assistance_session_not_found", status=404)
        value = json.loads(row["binding_json"])
        if not cleanup:
            if value.get("protocol") != SESSION_PROTOCOL:
                raise AssistanceError(
                    "assistance_restart_required",
                    "This older AI help session needs a new explicit Launch. Your form and previous receipts remain unchanged.",
                    409,
                )
            if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
                raise AssistanceError(
                    "assistance_session_expired",
                    "This AI help session expired. Your form is unchanged.",
                    410,
                )
            if value.get("phase") == "ended":
                raise AssistanceError(
                    "assistance_session_ended",
                    "This AI help session has ended. Open a new session to continue.",
                    410,
                )
        return value

    def _save(self, conn, value: Mapping[str, Any]) -> None:
        conn.execute(
            "UPDATE assisted_draft_sessions SET binding_json=? WHERE session_id=?",
            (canonical(value), value["assistantSessionId"]),
        )

    @staticmethod
    def _advance_control(value: dict[str, Any]) -> int:
        revision = validate_control_revision(value.get("controlRevision", 0)) + 1
        value["controlRevision"] = revision
        return revision

    @staticmethod
    def _assert_control(value: Mapping[str, Any], expected: int) -> None:
        if value.get("controlRevision", 0) != expected:
            raise AssistanceError(
                "assistance_control_changed",
                "AI help was stopped or changed. Review the current session before starting again.",
                409,
            )

    def _selection(self, conversation_id: str, conn=None):
        return executions.projected_execution(
            conversation_id,
            lambda: self.runner.default_selection().to_dict(),
            conn=conn,
        )

    def recovery(self, session_id: str, actor: str) -> dict[str, Any]:
        """Read-only terminal/legacy projection, never a provider migration."""
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor, cleanup=True)
            expires_at = conn.execute(
                "SELECT expires_at FROM assisted_draft_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()["expires_at"]
        legacy = value.get("protocol") != SESSION_PROTOCOL
        phase = (
            "restart_required"
            if legacy
            else "ended"
            if value.get("phase") == "ended"
            else "expired"
            if datetime.fromisoformat(expires_at) <= datetime.now(UTC)
            else value["phase"]
        )
        return {
            **({"protocol": SESSION_PROTOCOL} if not legacy else {}),
            **{
                key: value[key]
                for key in (
                    "assistantSessionId",
                    "conversationId",
                    "identity",
                    "schema",
                )
            },
            "expiresAt": expires_at,
            "phase": phase,
            "controlRevision": value.get("controlRevision", 0),
            "activeStartId": None,
            "restartRequired": legacy,
            "agent": {
                "status": phase,
                "phase": phase,
                "alive": False,
                "started": False,
                "controlRevision": value.get("controlRevision", 0),
            },
            "availability": self.availability(),
        }

    def _internal(self, value: Mapping[str, Any], conn) -> dict[str, Any]:
        result = dict(value)
        result["execution"] = self._selection(value["conversationId"], conn).to_dict()
        if value.get("activeStartId"):
            start = conn.execute(
                "SELECT * FROM assisted_draft_starts WHERE session_id=? AND start_id=?",
                (value["assistantSessionId"], value["activeStartId"]),
            ).fetchone()
            if start is None:
                raise AssistanceError("assistance_start_required", status=409)
            admitted = json.loads(start["execution_json"])
            if any(
                admitted.get(key) != result["execution"].get(key)
                for key in ("schema_version", "provider_id", "model_id", "revision")
            ):
                raise conversations.ConversationLeaseLost("lease_lost")
            result.update(
                initialSnapshot=json.loads(start["initial_json"]),
                authorizationRef=start["authorization_ref"],
                execution=admitted,
            )
        return result

    def _public(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            key: value[key]
            for key in (
                "protocol",
                "assistantSessionId",
                "conversationId",
                "identity",
                "schema",
                "expiresAt",
                "phase",
                "activeStartId",
                "greetingMessageId",
            )
            if key in value
        }
        result["availability"] = self.availability()
        result.update(self.execution(value["assistantSessionId"], None))
        result["phase"] = result["agent"]["phase"]
        result["controlRevision"] = result["agent"]["controlRevision"]
        return result

    def session(
        self, session_id: str, actor: str | None, *, internal: bool = False
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor)
            if internal:
                return self._internal(value, conn)
        return self._public(value)

    def prepare_session(self, body: Mapping[str, Any], actor: str) -> dict[str, Any]:
        self._require_work()
        if set(body) != {
            "requestId",
            "identity",
            "schema",
            "interactionMode",
            "readOnly",
        }:
            if (
                "disclosureAccepted" in body
                or "providerId" in body
                or "modelId" in body
            ):
                raise AssistanceError(
                    "assistance_restart_required",
                    "Reload the AI help panel to review the current provider disclosure.",
                    409,
                )
            raise AssistanceError("invalid_assistance_request")
        if body["interactionMode"] != "operate" or body["readOnly"] is not False:
            raise AssistanceError("assistance_mode_blocked", status=403)
        identity = validate_identity(body["identity"])
        form = form_schema(identity["draftName"], body["schema"])
        request_id = text_id(body["requestId"], "request_id")
        session_id = "as-" + digest({"actor": actor, "request": request_id})[:32]
        request_hash = digest({"identity": identity, "schema": form["schema"]})
        with self._transaction() as conn:
            prior = conn.execute(
                "SELECT request_hash FROM assisted_draft_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise AssistanceError("request_id_conflict", status=409)
                value = self._row(conn, session_id, actor)
            else:
                source = f"assisted-draft:{session_id}"
                existing = conn.execute(
                    "SELECT conversation_id FROM conversations WHERE source=?",
                    (source,),
                ).fetchone()
                if existing:
                    conversation_id = existing["conversation_id"]
                else:
                    conversation_id = conversations.create_conversation(
                        title=form["title"],
                        source=source,
                        metadata={
                            "assistedDraft": {
                                "sessionId": session_id,
                                "identity": identity,
                                "submitPolicy": "user_only",
                            }
                        },
                        conn=conn,
                    ).conversation_id
                # Default projection is probe-free. An unavailable default is
                # still visible so the person can select another ready pair.
                current = self._selection(conversation_id, conn)
                if not current.persisted:
                    executions.set_execution(
                        conversation_id,
                        current.to_dict(),
                        expected_revision=None,
                        conn=conn,
                    )
                value = {
                    "protocol": SESSION_PROTOCOL,
                    "assistantSessionId": session_id,
                    "conversationId": conversation_id,
                    "identity": identity,
                    "schema": form["schema"],
                    "expiresAt": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                    "phase": "prepared",
                    "controlRevision": 0,
                    "activeStartId": None,
                }
                conn.execute(
                    "INSERT INTO assisted_draft_sessions VALUES (?,?,?,?,?,?)",
                    (
                        session_id,
                        actor,
                        request_hash,
                        conversation_id,
                        canonical(value),
                        value["expiresAt"],
                    ),
                )
        return self._public(value)

    def execution(
        self, session_id: str, actor: str | None, *, refresh: bool = False
    ) -> dict[str, Any]:
        dead_lease = None
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor)
            state = self._selection(value["conversationId"], conn)
            lease = conversations.get_agent_lease(
                value["conversationId"], CONSUMER, conn=conn
            )
            if (
                value["phase"] == "active"
                and lease
                and lease["status"] == "running"
                and type(lease.get("pid")) is int
                and self._driver_exited(lease["pid"], lease["generation"])
            ):
                dead_lease = self._fence_locked(conn, value["conversationId"])
                conn.execute(
                    "UPDATE conversation_agent_leases SET status='failed',error=? WHERE conversation_id=? AND consumer=? AND generation=?",
                    (
                        self._driver_error(lease["pid"], lease["generation"]),
                        value["conversationId"],
                        CONSUMER,
                        lease["generation"],
                    ),
                )
                value["phase"] = "stopped"
                self._advance_control(value)
                self._save(conn, value)
                lease = conversations.get_agent_lease(
                    value["conversationId"], CONSUMER, conn=conn
                )
        self._terminate(dead_lease)
        providers = self.runner.catalog(refresh=refresh)["providers"]
        # Saved model IDs stay visible even after catalog retirement; no hidden
        # substitution and no client-authored labels.
        providers = json.loads(canonical(providers))
        provider = next(
            (item for item in providers if item["id"] == state.provider_id), None
        )
        if provider is None:
            provider = {
                "id": state.provider_id,
                "label": state.provider_label,
                "available": False,
                "availability": "unavailable",
                "auth_mode": "",
                "models": [],
                "unavailable_reason": "This saved chat provider is unavailable.",
            }
            providers.append(provider)
        if not any(item["id"] == state.model_id for item in provider.get("models", [])):
            provider.setdefault("models", []).append(
                {
                    "id": state.model_id,
                    "label": state.model_label,
                    "available": False,
                    "unavailable_reason": "This saved model is unavailable.",
                }
            )
        display_selection = project_selection_labels(state.to_dict(), providers)
        agent = {
            "status": value["phase"],
            "phase": value["phase"],
            "controlRevision": value.get("controlRevision", 0),
            "activeStartId": value.get("activeStartId"),
            "supersededTurnCount": value.get("supersededTurnCount", 0),
            "alive": False,
            "started": False,
        }
        if lease and (
            value["phase"] == "active"
            or (
                value["phase"] == "stopped"
                and lease["status"] in {"failed", "spawn_failed"}
            )
        ):
            agent.update(
                status=lease["status"],
                alive=lease["status"] in {"starting", "running"},
                error=_driver_error_message(lease.get("error"), state.provider_id),
            )
        return {
            "execution": {
                "selection": display_selection,
                "providers": providers,
                "read_only": self.read_only() or not self.enabled(),
            },
            "agent": agent,
        }

    def select_execution(
        self, session_id: str, actor: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require_work()
        if set(body) != {"provider_id", "model_id", "expected_revision"}:
            raise AssistanceError("invalid_execution_selection")
        selected = self.runner.validate_selection(
            text_id(body["provider_id"]), text_id(body["model_id"])
        )
        old_lease = None
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor)
            before = self._selection(value["conversationId"], conn)
            changed = (
                before.provider_id != selected.provider_id
                or before.model_id != selected.model_id
            )
            executions.set_execution(
                value["conversationId"],
                selected.to_dict(),
                expected_revision=body["expected_revision"],
                conn=conn,
            )
            if changed:
                old_lease = self._fence_locked(conn, value["conversationId"])
                value.update(phase="prepared", activeStartId=None)
                self._advance_control(value)
                value.pop("greetingMessageId", None)
                self._save(conn, value)
        self._terminate(old_lease)
        return self.execution(session_id, actor)

    def start(
        self, session_id: str, actor: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require_work()
        if set(body) != {
            "requestId",
            "disclosureAccepted",
            "provider_id",
            "model_id",
            "expected_revision",
            "expected_control_revision",
            "initialSnapshot",
        }:
            raise AssistanceError("invalid_assistance_start")
        if body["disclosureAccepted"] is not True:
            raise AssistanceError("disclosure_gesture_required", status=403)
        request_id = text_id(body["requestId"], "request_id")
        provider_id = text_id(body["provider_id"])
        model_id = text_id(body["model_id"])
        expected_control = validate_control_revision(body["expected_control_revision"])
        expected_execution = text_id(body["expected_revision"], "execution_revision")
        start_id = "ast-" + digest({"session": session_id, "request": request_id})[:32]

        def current_selection(conn, value):
            state = self._selection(value["conversationId"], conn)
            if (
                state.revision != expected_execution
                or state.provider_id != provider_id
                or state.model_id != model_id
            ):
                raise executions.ConversationExecutionConflict(
                    "execution_selection_changed"
                )
            return state

        def prior_attempt(conn, value, request_hash):
            old = conn.execute(
                "SELECT request_hash FROM assisted_draft_starts WHERE session_id=? AND start_id=?",
                (session_id, start_id),
            ).fetchone()
            if old is None:
                return False
            if old["request_hash"] != request_hash:
                raise AssistanceError("request_id_conflict", status=409)
            if value.get("activeStartId") != start_id:
                current_selection(conn, value)
                raise AssistanceError(
                    "assistance_start_superseded",
                    "This launch was superseded. Review the current context and choose Launch again.",
                    409,
                )
            return True

        # Fast exact replay is entirely durable: it does not probe a provider,
        # acquire another generation, or reinterpret a stopped attempt as new.
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor)
            form = form_schema(value["identity"]["draftName"], value["schema"])
            initial = validate_prepared_snapshot(form, body["initialSnapshot"])
            request_hash = digest(
                {
                    "initialSnapshot": initial,
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "expected_revision": expected_execution,
                    "expected_control_revision": expected_control,
                }
            )
            replay = prior_attempt(conn, value, request_hash)
            if not replay:
                self._assert_control(value, expected_control)
                current_selection(conn, value)
        if replay:
            return self.session(session_id, actor)

        # Catalog/auth validation may block. Stop/End must retain authority
        # during that wait, so both control and execution are checked again
        # after it completes, under the canonical destination write lock.
        selected = self.runner.validate_selection(provider_id, model_id)
        self._require_work()
        old_lease = None
        launch = False
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor)
            if not prior_attempt(conn, value, request_hash):
                self._assert_control(value, expected_control)
                state = current_selection(conn, value)
                if (selected.provider_id, selected.model_id) != (provider_id, model_id):
                    raise AssistanceError("execution_selection_changed", status=409)
                admitted_execution = {
                    **state.to_dict(),
                    "provider_label": selected.provider_label,
                    "model_label": selected.model_label,
                }
                launch = True
                old_lease = self._fence_locked(conn, value["conversationId"])
                superseded = self._supersede_pending(conn, value, start_id)
                prior = self._history(conn, value["conversationId"])
                conn.execute(
                    "INSERT INTO assisted_draft_starts VALUES (?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        start_id,
                        request_hash,
                        canonical(initial),
                        canonical(admitted_execution),
                        f"assistance-start:{actor}:{session_id}:{request_id}",
                        canonical(prior),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._stage_snapshot(conn, session_id, start_id, initial, initial=True)
                value.update(
                    activeStartId=start_id,
                    greetingMessageId="assist-greeting-"
                    + digest({"session": session_id, "start": start_id})[:32],
                )
                value["supersededTurnCount"] = superseded
            if launch:
                value["phase"] = "active"
                self._advance_control(value)
                self._save(conn, value)
        self._terminate(old_lease)
        if launch:
            self._wake(
                session_id, start_id=start_id, control_revision=value["controlRevision"]
            )
        return self.session(session_id, actor)

    @staticmethod
    def _supersede_pending(conn, session, start_id: str) -> int:
        """A fresh human Start replaces pending work, never rediscloses old bases."""
        conversation_id = session["conversationId"]
        cursor = conn.execute(
            "SELECT last_created_at,last_message_id FROM conversation_consumer_cursors WHERE conversation_id=? AND consumer=?",
            (conversation_id, CONSUMER),
        ).fetchone()
        created = cursor["last_created_at"] if cursor else ""
        last_id = cursor["last_message_id"] if cursor else ""
        pending = conn.execute(
            "SELECT message_id,created_at FROM messages WHERE conversation_id=? AND role='user' AND (created_at>? OR (created_at=? AND message_id>?)) ORDER BY created_at,message_id",
            (conversation_id, created, created, last_id),
        ).fetchall()
        now = datetime.now(UTC).isoformat()
        for message in pending:
            conn.execute(
                "UPDATE assisted_draft_turns SET state='superseded' WHERE session_id=? AND message_id=?",
                (session["assistantSessionId"], message["message_id"]),
            )
        tail = pending[-1] if pending else None
        if tail is not None:
            conn.execute(
                "INSERT INTO conversation_consumer_cursors VALUES (?,?,?,?,?) ON CONFLICT(conversation_id,consumer) DO UPDATE SET last_created_at=excluded.last_created_at,last_message_id=excluded.last_message_id,updated_at=excluded.updated_at",
                (
                    conversation_id,
                    CONSUMER,
                    tail["created_at"],
                    tail["message_id"],
                    now,
                ),
            )
        # Superseded inline questions remain in history, but cannot consume a
        # response or block the fresh agent's finite-choice question.
        conn.execute(
            "UPDATE messages SET status='sent' WHERE conversation_id=? AND role='agent' AND message_type='question' AND status='pending'",
            (conversation_id,),
        )
        conn.execute(
            "INSERT INTO assisted_draft_supersessions VALUES (?,?,?,?,?,?)",
            (
                session["assistantSessionId"],
                start_id,
                session.get("activeStartId"),
                tail["message_id"] if tail else None,
                len(pending),
                now,
            ),
        )
        return len(pending)

    @staticmethod
    def _history(
        conn, conversation_id: str, *, before_message_id: str | None = None
    ) -> list[dict[str, Any]]:
        messages = conversations.get_conversation_with_messages(
            conversation_id, conn=conn
        )["messages"]
        if before_message_id is not None:
            index = next(
                (
                    i
                    for i, item in enumerate(messages)
                    if item["message_id"] == before_message_id
                ),
                None,
            )
            if index is None:
                raise AssistanceError("assistance_turn_not_found", status=404)
            messages = messages[:index]
        result = [
            {
                "message_id": item["message_id"],
                "role": item["role"],
                "content": item["content"],
                **{
                    key: item[key]
                    for key in (
                        "message_type",
                        "response_type",
                        "choices",
                        "status",
                        "response",
                    )
                    if key in item
                },
                **(
                    {"in_reply_to": item["context"]["in_reply_to"]}
                    if isinstance(item.get("context"), dict)
                    and isinstance(item["context"].get("in_reply_to"), str)
                    else {}
                ),
            }
            for item in messages[-12:]
        ]
        answers = {
            item["context"]["in_reply_to"]: item["content"]
            for item in messages
            if item["role"] == "user"
            and isinstance(item.get("context"), dict)
            and isinstance(item["context"].get("in_reply_to"), str)
        }
        for item in result:
            if (
                item.get("message_type") == "question"
                and item.get("status") == "answered"
            ):
                # Question rows are mutable after an exact answer. A later
                # answer must not leak through an earlier turn's prefix.
                item["response"] = answers.get(item["message_id"])
                if item["response"] is None:
                    item["status"] = "pending"
        while len(canonical(result).encode("utf-8")) > 16 * 1024 and result:
            result.pop(0)
        return result

    @staticmethod
    def _stage_snapshot(
        conn,
        session_id: str,
        start_id: str,
        prepared: Mapping[str, Any],
        *,
        initial: bool = False,
    ) -> None:
        message_id = prepared["messageId"]
        request_hash = digest(prepared)
        row = conn.execute(
            "SELECT snapshot_hash,start_id FROM assisted_draft_turns WHERE session_id=? AND message_id=?",
            (session_id, message_id),
        ).fetchone()
        if row:
            if row["snapshot_hash"] != request_hash or row["start_id"] != start_id:
                raise AssistanceError("message_id_conflict", status=409)
            return
        count = conn.execute(
            "SELECT COUNT(*) FROM assisted_draft_turns WHERE session_id=? AND start_id=?",
            (session_id, start_id),
        ).fetchone()[0]
        if count >= MAX_TURNS:
            raise AssistanceError(
                "assistance_turn_limit", "Open a new AI help session to continue.", 429
            )
        conn.execute(
            "INSERT INTO assisted_draft_turns(session_id,message_id,snapshot_hash,snapshot_json,state,start_id) VALUES (?,?,?,?,?,?)",
            (
                session_id,
                message_id,
                request_hash,
                canonical(prepared),
                "initial" if initial else "prepared",
                start_id,
            ),
        )

    def prepare(
        self, session_id: str, actor: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require_work()
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor)
            if value["phase"] != "active":
                raise AssistanceError("assistance_start_required", status=409)
            prepared = validate_prepared_snapshot(
                form_schema(value["identity"]["draftName"], value["schema"]), body
            )
            self._stage_snapshot(conn, session_id, value["activeStartId"], prepared)
        return {"prepared": True, "messageId": prepared["messageId"]}

    def respond(
        self, session_id: str, conversation_id: str, actor: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require_work()
        if set(body) - {"value", "message_id", "in_reply_to"}:
            raise AssistanceError("invalid_assistance_turn")
        message_id = text_id(body.get("message_id"), "message_id")
        text = body.get("value")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode("utf-8")) > 8192
        ):
            raise AssistanceError("invalid_assistance_turn")
        in_reply_to = body.get("in_reply_to")
        if in_reply_to is not None:
            text_id(in_reply_to, "question_id")
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor)
            if value["conversationId"] != conversation_id:
                raise AssistanceError("assistance_binding_mismatch", status=409)
            if value["phase"] != "active":
                raise AssistanceError("assistance_start_required", status=409)
            row = conn.execute(
                "SELECT * FROM assisted_draft_turns WHERE session_id=? AND message_id=?",
                (session_id, message_id),
            ).fetchone()
            if (
                row is None
                or row["start_id"] != value["activeStartId"]
                or row["state"] == "initial"
            ):
                raise AssistanceError("snapshot_required")
            user_hash = digest(
                {
                    "value": text,
                    "snapshotHash": row["snapshot_hash"],
                    "in_reply_to": in_reply_to,
                }
            )
            if row["user_hash"] is not None and row["user_hash"] != user_hash:
                raise AssistanceError("message_id_conflict", status=409)
            prepared = json.loads(row["snapshot_json"])
            context = {
                "kind": "assisted_draft",
                "assistant_session_id": session_id,
                "start_id": value["activeStartId"],
                "base_snapshot_hash": prepared["baseSnapshotHash"],
            }
            if in_reply_to is not None:
                context["in_reply_to"] = in_reply_to
                question = conn.execute(
                    "SELECT * FROM messages WHERE conversation_id=? AND message_id=? AND role=? AND message_type=?",
                    (conversation_id, in_reply_to, "agent", "question"),
                ).fetchone()
                if question is None:
                    raise AssistanceError("question_unavailable", status=409)
                if question["response_type"] == "boolean" and text not in {
                    "true",
                    "false",
                    "yes",
                    "no",
                }:
                    raise AssistanceError("invalid_question_answer")
                if question["response_type"] == "choice":
                    choices = json.loads(question["choices"] or "[]")
                    if text not in {str(item["key"]) for item in choices}:
                        raise AssistanceError("invalid_question_answer")
                message = conversations.respond_to_message_with_user_message(
                    conversation_id,
                    in_reply_to,
                    text,
                    conn=conn,
                    user_message_id=message_id,
                    context=context,
                )
            else:
                message = conversations.post_user_message(
                    conversation_id,
                    text,
                    conn=conn,
                    message_id=message_id,
                    context=context,
                )
            if message is None:
                raise AssistanceError(
                    "question_unavailable" if in_reply_to else "conversation_closed",
                    status=409,
                )
            conn.execute(
                "UPDATE assisted_draft_turns SET user_hash=?,state=CASE WHEN state='prepared' THEN 'queued' ELSE state END WHERE session_id=? AND message_id=?",
                (user_hash, session_id, message_id),
            )
        # This is a persistent interactive driver. A failed/stopped process is
        # never automatically replaced by a new disclosure generation just
        # because a send was retried; the human must explicitly Launch again.
        return {
            "message_id": message_id,
            **({"in_reply_to": in_reply_to} if in_reply_to is not None else {}),
        }

    @staticmethod
    def _fence_locked(conn, conversation_id: str) -> dict[str, Any] | None:
        lease = conversations.get_agent_lease(conversation_id, CONSUMER, conn=conn)
        if lease:
            conn.execute(
                "UPDATE conversation_agent_leases SET status='stopped',updated_at=? WHERE conversation_id=? AND consumer=? AND generation=?",
                (
                    datetime.now(UTC).isoformat(),
                    conversation_id,
                    CONSUMER,
                    lease["generation"],
                ),
            )
        return lease

    def _terminate(self, lease: Mapping[str, Any] | None) -> None:
        if lease and type(lease.get("pid")) is int and lease["pid"] > 0:
            try:
                self.runner.terminate(lease["pid"], lease["generation"])
            except Exception:  # noqa: BLE001 - provider diagnostics are never public
                # Lease revocation already succeeded. Provider cleanup keeps
                # exact ownership and may retry; never resurrect authority.
                logger.warning(
                    "AI help process cleanup is unconfirmed; its lease remains revoked."
                )

    def _driver_exited(self, pid: int, generation: str) -> bool:
        # An exact-owned completion wins over an unrelated process reusing the
        # same numeric PID. A cache miss still permits the normal liveness check.
        return (
            self.runner.exit_code(pid, generation) is not None
            or not self.runner.is_alive(pid)
        )

    def _driver_error(self, pid: int | None, generation: str) -> str:
        if (
            type(pid) is int
            and pid > 0
            and self.runner.exit_code(pid, generation) == WorkerExitCode.AUTH_REQUIRED
        ):
            return _AUTH_REQUIRED
        return _DRIVER_FAILED

    def _wake(self, session_id: str, *, start_id: str, control_revision: int) -> None:
        self._require_work()
        stale = None
        with self._transaction() as conn:
            value = self._row(conn, session_id, None)
            if (
                value["phase"] != "active"
                or value.get("activeStartId") != start_id
                or value.get("controlRevision", 0) != control_revision
            ):
                return
            session = self._internal(value, conn)
            conversation_id = session["conversationId"]
            existing = conversations.get_agent_lease(
                conversation_id, CONSUMER, conn=conn
            )
            if (
                existing
                and existing["status"] == "running"
                and type(existing.get("pid")) is int
                and self._driver_exited(existing["pid"], existing["generation"])
            ):
                stale = self._fence_locked(conn, conversation_id)
            generation = uuid.uuid4().hex
            execution = {
                key: session["execution"][key]
                for key in (
                    "schema_version",
                    "provider_id",
                    "model_id",
                    "provider_label",
                    "model_label",
                )
            }
            lease = conversations.claim_agent_lease(
                conversation_id,
                CONSUMER,
                generation,
                heartbeat_ttl_seconds=900,
                execution=execution,
                conn=conn,
            )
            if lease is None or not lease["claimed"]:
                return
            if existing and existing["generation"] != generation:
                stale = existing
        self._terminate(stale)
        pid = None
        try:
            self._require_work()
            with self._transaction() as conn:
                self._worker_scope(
                    agent_session_id=assistance_execution_session_id(generation),
                    conversation_id=conversation_id,
                    consumer=CONSUMER,
                    generation=generation,
                    conn=conn,
                    require_initial=False,
                )
            result = self.runner.start(session=session, generation=generation)
            pid = result.get("pid")
            if result.get("status") != "ok" or type(pid) is not int or pid <= 0:
                raise AssistanceError("assistance_start_failed")
            with self._transaction() as conn:
                # A slow provider probe/spawn may outlive Stop, End, expiry,
                # opt-out or a restore/read-only transition. Check all of them
                # again before accepting the returned owned process.
                self._worker_scope(
                    agent_session_id=assistance_execution_session_id(generation),
                    conversation_id=conversation_id,
                    consumer=CONSUMER,
                    generation=generation,
                    conn=conn,
                    require_initial=False,
                )
                if not conversations.activate_agent_lease(
                    conversation_id, CONSUMER, generation, pid, conn=conn
                ):
                    raise conversations.ConversationLeaseLost("lease_lost")
            from work_buddy.conversations.agents import register

            register(conversation_id, pid)
            if self._driver_exited(pid, generation):
                raise AssistanceError("assistance_driver_exited")
        except Exception as exc:  # noqa: BLE001 - sanitize failures and revoke authority
            failure = self._driver_error(pid, generation)
            logger.warning(
                "AI help driver failed: reason=%s, error_type=%s",
                failure,
                type(exc).__name__,
            )
            if type(pid) is int and pid > 0:
                self._terminate({"pid": pid, "generation": generation})
            with self._transaction() as conn:
                value = self._row(conn, session_id, None, cleanup=True)
                current = conversations.get_agent_lease(
                    conversation_id, CONSUMER, conn=conn
                )
                if (
                    current
                    and current["generation"] == generation
                    and value.get("activeStartId") == session["activeStartId"]
                    and value.get("phase") == "active"
                    and value.get("controlRevision", 0) == control_revision
                    and current["status"] in {"starting", "running"}
                ):
                    conn.execute(
                        "UPDATE conversation_agent_leases SET status='spawn_failed',pid=NULL,error=?,updated_at=? WHERE conversation_id=? AND consumer=? AND generation=?",
                        (
                            failure,
                            datetime.now(UTC).isoformat(),
                            conversation_id,
                            CONSUMER,
                            generation,
                        ),
                    )
                    value["phase"] = "stopped"
                    self._advance_control(value)
                    self._save(conn, value)

    def stop(
        self,
        session_id: str,
        actor: str,
        *,
        end: bool = False,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Cleanup is authority-reducing and remains usable after opt-out,
        # expiry, read-only/restore fences, or legacy protocol retirement.
        expected = None
        target_start = None
        if body:
            if not {"requestId", "expected_control_revision"}.issubset(body) or set(
                body
            ) - {"requestId", "expected_control_revision", "startRequestId"}:
                raise AssistanceError("invalid_assistance_stop")
            text_id(body["requestId"], "request_id")
            expected = validate_control_revision(body["expected_control_revision"])
            if "startRequestId" in body:
                request_id = text_id(body["startRequestId"], "request_id")
                target_start = (
                    "ast-"
                    + digest(
                        {
                            "session": session_id,
                            "request": request_id,
                        }
                    )[:32]
                )
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor, cleanup=True)
            current = value.get("controlRevision", 0)
            if not end and expected is not None:
                # A detach can race either side of its pending Start commit.
                # The +1 window belongs only to that exact active Start, not
                # another Start, a previous Stop, or a model-selection change.
                matches_pending_start = (
                    current == expected + 1
                    and value.get("phase") == "active"
                    and target_start is not None
                    and value.get("activeStartId") == target_start
                )
                if current != expected and not matches_pending_start:
                    return {
                        "stopped": True,
                        "controlRevision": current,
                        "outcome": "superseded",
                    }
            lease = self._fence_locked(conn, value["conversationId"])
            if value.get("phase") != "ended":
                value["phase"] = "ended" if end else "stopped"
                self._advance_control(value)
            self._save(conn, value)
        self._terminate(lease)
        if end:
            return {"ended": True, "controlRevision": value.get("controlRevision", 0)}
        return {
            "stopped": True,
            "controlRevision": value.get("controlRevision", 0),
            "outcome": "stopped",
        }

    def conversation(
        self, session_id: str, conversation_id: str, actor: str
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            value = self._row(conn, session_id, actor, cleanup=True)
            if conversation_id != value["conversationId"]:
                raise AssistanceError("assistance_binding_mismatch", status=409)
            result = conversations.get_conversation_with_messages(
                conversation_id, conn=conn
            )
            lease = conversations.get_agent_lease(conversation_id, CONSUMER, conn=conn)
            expires_at = conn.execute(
                "SELECT expires_at FROM assisted_draft_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()["expires_at"]
            result["conversation"]["agent_alive"] = bool(
                value.get("phase") == "active"
                and value.get("protocol") == SESSION_PROTOCOL
                and datetime.fromisoformat(expires_at) > datetime.now(UTC)
                and lease
                and lease["status"] in {"starting", "running"}
            )
            return result

    def patches(self, session_id: str, actor: str) -> list[dict[str, Any]]:
        with self._transaction() as conn:
            session = self._row(conn, session_id, actor, cleanup=True)
            return [
                {
                    "patch": json.loads(row["patch_json"]),
                    "receipt": json.loads(row["receipt_json"])
                    if row["receipt_json"]
                    else None,
                    # Timeline placement is server-authored transport metadata,
                    # not part of either strict patch or receipt envelope.  A
                    # patch can become visible before its reply is durable, so
                    # the exact reply anchor is intentionally nullable and may
                    # be filled by a later projection of the same patch.
                    "sourceMessageId": row["source_message_id"],
                    "replyMessageId": row["reply_message_id"],
                }
                for row in conn.execute(
                    """
                    SELECT turn.patch_json,
                           turn.receipt_json,
                           turn.message_id AS source_message_id,
                           (
                             SELECT context.reply_message_id
                               FROM assisted_draft_context_receipts AS context
                              WHERE context.session_id = turn.session_id
                                AND context.start_id = turn.start_id
                                AND context.message_id = turn.message_id
                                AND context.reply_message_id IS NOT NULL
                                AND EXISTS (
                                  SELECT 1
                                    FROM messages AS reply
                                   WHERE reply.conversation_id = ?
                                     AND reply.message_id = context.reply_message_id
                                     AND reply.role = 'agent'
                                )
                              ORDER BY context.rowid DESC
                              LIMIT 1
                           ) AS reply_message_id
                      FROM assisted_draft_turns AS turn
                     WHERE turn.session_id = ?
                       AND turn.patch_json IS NOT NULL
                     ORDER BY turn.rowid
                    """,
                    (session["conversationId"], session_id),
                )
            ]

    def acknowledge(
        self, session_id: str, actor: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.read_only():
            raise AssistanceError("dashboard_read_only", status=403)
        patch_id = text_id(body.get("patchId"), "patch_id")
        if set(body) != {
            "patchId",
            "status",
            "appliedFields",
            "pendingFields",
            "resultingRevision",
            "message",
        }:
            raise AssistanceError("invalid_patch_receipt")
        if (
            body["status"]
            not in {"applied", "pending", "partial", "rejected", "undone"}
            or type(body["resultingRevision"]) is not int
            or body["resultingRevision"] < 0
            or not isinstance(body["message"], str)
            or len(body["message"]) > 1000
        ):
            raise AssistanceError("invalid_patch_receipt")
        with self._transaction() as conn:
            self._row(conn, session_id, actor, cleanup=True)
            rows = conn.execute(
                "SELECT * FROM assisted_draft_turns WHERE session_id=? AND patch_json IS NOT NULL",
                (session_id,),
            ).fetchall()
            row = next(
                (
                    item
                    for item in rows
                    if json.loads(item["patch_json"])["patchId"] == patch_id
                ),
                None,
            )
            if row is None:
                raise AssistanceError("patch_not_found", status=404)
            allowed = {
                canonical(op["path"])
                for op in json.loads(row["patch_json"])["operations"]
            }
            if not isinstance(body["appliedFields"], list) or not isinstance(
                body["pendingFields"], list
            ):
                raise AssistanceError("invalid_patch_receipt")
            paths = list(body["appliedFields"])
            for pending in body["pendingFields"]:
                if (
                    not isinstance(pending, dict)
                    or set(pending) != {"path", "reason"}
                    or pending["reason"]
                    not in {
                        "focused",
                        "user_changed",
                        "suggest_only",
                        "storage_conflict",
                    }
                ):
                    raise AssistanceError("invalid_patch_receipt")
                paths.append(pending["path"])
            if any(canonical(path) not in allowed for path in paths) or len(
                {canonical(path) for path in paths}
            ) != len(paths):
                raise AssistanceError("invalid_patch_receipt")
            previous = json.loads(row["receipt_json"]) if row["receipt_json"] else None
            if previous == body:
                return previous
            if previous and (
                body["resultingRevision"] < previous["resultingRevision"]
                or previous["status"] in {"rejected", "undone"}
            ):
                raise AssistanceError("patch_receipt_conflict", status=409)
            conn.execute(
                "UPDATE assisted_draft_turns SET receipt_json=? WHERE session_id=? AND message_id=?",
                (canonical(body), session_id, row["message_id"]),
            )
        return dict(body)

    def _worker_scope(
        self,
        *,
        agent_session_id,
        conversation_id,
        consumer,
        generation,
        conn,
        require_initial=True,
    ) -> dict[str, Any]:
        self._require_work()
        if (
            assistance_generation_from_session(agent_session_id) != generation
            or consumer != CONSUMER
        ):
            raise conversations.ConversationLeaseLost("lease_lost")
        row = conn.execute(
            "SELECT session_id FROM assisted_draft_sessions WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise conversations.ConversationLeaseLost("lease_lost")
        value = self._row(conn, row["session_id"], None)
        if value["phase"] != "active" or not value.get("activeStartId"):
            raise AssistanceError("assistance_start_required", status=409)
        lease = conversations.get_agent_lease(conversation_id, consumer, conn=conn)
        conversation = conversations.get_conversation(conversation_id, conn=conn)
        if (
            not lease
            or lease["generation"] != generation
            or lease["status"] not in {"starting", "running"}
            or conversation is None
            or conversation.status != "open"
        ):
            raise conversations.ConversationLeaseLost("lease_lost")
        if (
            lease["status"] == "starting"
            and (
                datetime.now(UTC) - datetime.fromisoformat(lease["started_at"])
            ).total_seconds()
            > 20
        ):
            raise conversations.ConversationLeaseLost("lease_lost")
        result = self._internal(value, conn)
        expected = {
            key: result["execution"][key]
            for key in (
                "schema_version",
                "provider_id",
                "model_id",
                "provider_label",
                "model_label",
            )
        }
        start = conn.execute(
            "SELECT execution_json FROM assisted_draft_starts WHERE session_id=? AND start_id=?",
            (value["assistantSessionId"], value["activeStartId"]),
        ).fetchone()
        if (
            lease["execution"] != expected
            or json.loads(start["execution_json"]) != result["execution"]
        ):
            raise conversations.ConversationLeaseLost("lease_lost")
        if require_initial:
            self._consumed(
                conn, result, generation, result["initialSnapshot"]["messageId"]
            )
        result.update(
            workerSessionId=agent_session_id, consumer=consumer, generation=generation
        )
        return result

    @staticmethod
    def _consumed(conn, session: Mapping[str, Any], generation: str, message_id: str):
        receipt = conn.execute(
            "SELECT * FROM assisted_draft_context_receipts WHERE session_id=? AND start_id=? AND generation=? AND message_id=? AND disclosed=1",
            (
                session["assistantSessionId"],
                session["activeStartId"],
                generation,
                message_id,
            ),
        ).fetchone()
        if receipt is None:
            raise AssistanceError(
                "assistance_context_required",
                "Read the exact bound form context before continuing.",
                409,
            )
        return receipt

    def assert_worker_scope(self, **scope) -> dict[str, Any]:
        conn = scope.pop("conn", None)
        with self._transaction(conn) as active:
            return self._worker_scope(**scope, conn=active)

    def _boundary(self):
        if self.disclosure is None:
            from work_buddy.agent_execution.worker_disclosure import (
                get_default_worker_disclosure,
            )

            self.disclosure = get_default_worker_disclosure()
        return self.disclosure

    @staticmethod
    @contextmanager
    def _disclosure_errors():
        from work_buddy.agent_execution.disclosure import (
            DisclosureError,
            DisclosureReplayBlocked,
        )
        from work_buddy.backups.source_foundation_restore import (
            SourceFoundationRestorePending,
        )
        from work_buddy.sources.errors import SourceError

        try:
            yield
        except SourceFoundationRestorePending as exc:
            raise AssistanceError(
                "source_foundation_restore_pending",
                "AI help is paused until source restore reconciliation completes.",
                503,
            ) from exc
        except DisclosureReplayBlocked as exc:
            raise AssistanceError(
                "assistance_disclosure_ambiguous",
                "The previous context delivery cannot be replayed safely. Choose Launch again with the current form fields.",
                409,
            ) from exc
        except (DisclosureError, SourceError) as exc:
            raise AssistanceError(
                "assistance_disclosure_blocked",
                "The exact context is no longer available for safe disclosure. Your form is unchanged.",
                409,
            ) from exc

    def _bind_output(self, session, *, output_ref, idempotency_key):
        with self._disclosure_errors():
            return self._boundary().bind_output(
                self._run(session),
                output_ref=output_ref,
                idempotency_key=idempotency_key,
            )

    @staticmethod
    def _run(session):
        from work_buddy.agent_execution.worker_disclosure import WorkerRun

        return WorkerRun(
            run_id=session["workerSessionId"],
            worker_session_id=session["workerSessionId"],
            provider_id=session["execution"]["provider_id"],
            model_id=session["execution"]["model_id"],
            authorization_ref=session["authorizationRef"],
            purpose=PURPOSE,
        )

    def _native_message_refs(
        self, session, message_ids: list[str], expected_content
    ) -> list[str]:
        from work_buddy.sources.conversation import (
            ConversationMessageProvider,
            conversation_origin,
        )
        from work_buddy.sources.models import canonical_sha256, sha256_bytes
        from work_buddy.sources.providers import (
            ProviderRegistry,
            source_capture_from_origin,
        )

        boundary = self._boundary()
        refs = []
        for message_id in dict.fromkeys(message_ids):
            registry = ProviderRegistry()
            registry.register(
                ConversationMessageProvider(
                    principal=boundary.sources.issuer,
                    authorization_fingerprint=canonical_sha256(
                        {
                            "purpose": PURPOSE,
                            "conversation_id": session["conversationId"],
                            "message_id": message_id,
                        }
                    ),
                )
            )
            ref = source_capture_from_origin(
                boundary.sources.store,
                registry,
                provider_id="work-buddy-conversation",
                origin_ref=conversation_origin(
                    conversation_id=session["conversationId"], message_id=message_id
                ),
                principal=boundary.sources.issuer,
                purpose=PURPOSE,
                tenant_scope_id=boundary.sources.tenant_scope_id,
                originating_surface="dashboard_assisted_draft",
                namespace=session["conversationId"],
                expected_digest=(
                    sha256_bytes(expected_content[message_id].encode("utf-8"))
                    if message_id in expected_content
                    else None
                ),
            )
            refs.append(ref.uri)
        return refs

    def _account(
        self, session, payload, tool_call_id, message_ids=(), native_content=None
    ):
        if len(canonical(payload).encode("utf-8")) > 64 * 1024:
            raise AssistanceError("assistance_context_too_large")
        with self._disclosure_errors():
            self._require_work()
            boundary = self._boundary()
            expected_content = dict(native_content or {})
            for item in payload.get("conversation", ()):
                expected_content[item["message_id"]] = item["content"]
            if isinstance(payload.get("message"), Mapping):
                expected_content[payload["message"]["message_id"]] = payload["message"][
                    "content"
                ]
            if isinstance(payload.get("question"), str):
                expected_content[payload["message_id"]] = payload["question"]
            refs = self._native_message_refs(
                session, list(message_ids), expected_content
            )
            entry, _ = boundary.account_payload(
                self._run(session),
                payload=payload,
                source_role="derived_content",
                tool_call_id=tool_call_id,
                idempotency_key=f"{tool_call_id}:{digest(payload)}",
                derivation_refs=refs,
            )
            return entry

    def _validate_accounted_input(self, entry) -> None:
        """Short final epoch check; resolution/reservation happened outside DB locks."""
        with self._disclosure_errors():
            self._boundary().sources.validate_disclosure_reservation(
                reservation_id=entry.reservation_id,
                redaction_epoch=entry.redaction_epoch,
            )

    def context_get(
        self, *, assistant_session_id: str, message_id: str, **scope
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            session = self._worker_scope(**scope, conn=conn, require_initial=False)
            if session["assistantSessionId"] != assistant_session_id:
                raise conversations.ConversationLeaseLost("lease_lost")
            initial = session["initialSnapshot"]["messageId"] == message_id
            if not initial:
                self._consumed(
                    conn,
                    session,
                    scope["generation"],
                    session["initialSnapshot"]["messageId"],
                )
                delivered = conn.execute(
                    "SELECT 1 FROM assisted_draft_deliveries WHERE session_id=? AND start_id=? AND generation=? AND message_id=?",
                    (
                        assistant_session_id,
                        session["activeStartId"],
                        scope["generation"],
                        message_id,
                    ),
                ).fetchone()
                if delivered is None:
                    raise AssistanceError("assistance_turn_not_received", status=409)
            turn = conn.execute(
                "SELECT * FROM assisted_draft_turns WHERE session_id=? AND start_id=? AND message_id=?",
                (assistant_session_id, session["activeStartId"], message_id),
            ).fetchone()
            if turn is None:
                raise AssistanceError("assistance_turn_not_found", status=404)
            existing = conn.execute(
                "SELECT * FROM assisted_draft_context_receipts WHERE session_id=? AND start_id=? AND generation=? AND message_id=?",
                (
                    assistant_session_id,
                    session["activeStartId"],
                    scope["generation"],
                    message_id,
                ),
            ).fetchone()
            if existing:
                payload = json.loads(existing["payload_json"])
            else:
                form = form_schema(session["identity"]["draftName"], session["schema"])
                start = conn.execute(
                    "SELECT history_json FROM assisted_draft_starts WHERE session_id=? AND start_id=?",
                    (assistant_session_id, session["activeStartId"]),
                ).fetchone()
                history = (
                    json.loads(start["history_json"])
                    if initial
                    else self._history(
                        conn, session["conversationId"], before_message_id=message_id
                    )
                )
                greeting = conn.execute(
                    "SELECT 1 FROM messages WHERE conversation_id=? AND message_id=?",
                    (session["conversationId"], session["greetingMessageId"]),
                ).fetchone()
                previous_reply = conn.execute(
                    "SELECT reply_message_id FROM assisted_draft_context_receipts WHERE session_id=? AND start_id=? AND message_id=? AND reply_message_id IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                    (assistant_session_id, session["activeStartId"], message_id),
                ).fetchone()
                receipts = [
                    json.loads(row["receipt_json"])
                    for row in conn.execute(
                        "SELECT receipt_json FROM assisted_draft_turns WHERE session_id=? AND receipt_json IS NOT NULL ORDER BY rowid DESC LIMIT 8",
                        (assistant_session_id,),
                    )
                ]
                existing_patch = (
                    json.loads(turn["patch_json"]) if turn["patch_json"] else None
                )
                payload = {
                    "assistant_session_id": assistant_session_id,
                    "conversation_id": session["conversationId"],
                    "message_id": message_id,
                    "consumption_receipt_id": "acr-"
                    + digest(
                        {
                            "session": assistant_session_id,
                            "start": session["activeStartId"],
                            "generation": scope["generation"],
                            "message": message_id,
                        }
                    )[:32],
                    "form": {
                        key: form.get(key, [])
                        for key in (
                            "title",
                            "purpose",
                            "instructions",
                            "fields",
                            "submitPolicy",
                            "referenceScopes",
                        )
                    },
                    "snapshot": json.loads(turn["snapshot_json"]),
                    "conversation": history,
                    "host_receipts": receipts,
                    "greeting_sent": bool(greeting),
                    "greeting_message_id": session["greetingMessageId"],
                    "reply_message_id": previous_reply["reply_message_id"]
                    if previous_reply
                    else None,
                    "existing_patch": {"patch_id": existing_patch["patchId"]}
                    if existing_patch
                    else None,
                    "superseded_pending_turns": session.get("supersededTurnCount", 0),
                    "history_notice": "Earlier pending work was superseded by this explicit Launch, not completed. Historical requests are context only; this snapshot is the sole working base.",
                }
                while len(canonical(payload).encode("utf-8")) > 64 * 1024 and (
                    history or receipts
                ):
                    (history if history else receipts).pop(0)
                conn.execute(
                    "INSERT INTO assisted_draft_context_receipts(session_id,start_id,generation,message_id,receipt_id,payload_json) VALUES (?,?,?,?,?,?)",
                    (
                        assistant_session_id,
                        session["activeStartId"],
                        scope["generation"],
                        message_id,
                        payload["consumption_receipt_id"],
                        canonical(payload),
                    ),
                )
        # Never hold the destination write lock through Sources resolution,
        # retention or reservation. A later failed final recheck leaves this
        # possible handoff nonreplayable; no content is returned to the worker.
        message_ids = [item["message_id"] for item in payload["conversation"]]
        if not initial:
            message_ids.append(message_id)
        entry = self._account(
            session, payload, "assisted_draft_context_get", message_ids
        )
        with self._transaction() as conn:
            current = self._worker_scope(**scope, conn=conn, require_initial=False)
            if current["activeStartId"] != session["activeStartId"]:
                raise conversations.ConversationLeaseLost("lease_lost")
            stored = conn.execute(
                "SELECT payload_json FROM assisted_draft_context_receipts WHERE receipt_id=?",
                (payload["consumption_receipt_id"],),
            ).fetchone()
            if stored is None or stored["payload_json"] != canonical(payload):
                raise AssistanceError("assistance_context_changed", status=409)
            self._validate_accounted_input(entry)
            conn.execute(
                "UPDATE assisted_draft_context_receipts SET disclosed=1 WHERE receipt_id=?",
                (payload["consumption_receipt_id"],),
            )
        return payload

    def reference_search(
        self,
        *,
        assistant_session_id: str,
        message_id: str,
        consumption_receipt_id: str,
        request_id: str,
        reference_kind: str,
        query: str,
        **scope,
    ) -> dict[str, Any]:
        """Return immutable form-authorized metadata without dispatching it."""

        text_id(request_id, "reference_request_id")
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > MAX_REFERENCE_QUERY_CHARS
            or any(ord(character) < 32 for character in query)
        ):
            raise AssistanceError("invalid_reference_query")
        normalized_query = " ".join(query.split())
        request_hash = digest(
            {
                "message_id": message_id,
                "consumption_receipt_id": consumption_receipt_id,
                "reference_kind": reference_kind,
                "query": normalized_query,
            }
        )

        with self._transaction() as conn:
            session = self._worker_scope(**scope, conn=conn, require_initial=False)
            if session["assistantSessionId"] != assistant_session_id:
                raise conversations.ConversationLeaseLost("lease_lost")
            receipt = self._consumed(
                conn, session, scope["generation"], message_id
            )
            if receipt["receipt_id"] != consumption_receipt_id:
                raise AssistanceError("assistance_receipt_mismatch", status=409)
            form = form_schema(session["identity"]["draftName"], session["schema"])
            if reference_kind not in form.get("referenceScopes", ()):
                raise AssistanceError("assistance_reference_not_allowed", status=403)
            existing = conn.execute(
                "SELECT * FROM assisted_draft_reference_receipts WHERE session_id=? AND start_id=? AND generation=? AND request_id=?",
                (
                    assistant_session_id,
                    session["activeStartId"],
                    scope["generation"],
                    request_id,
                ),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise AssistanceError(
                        "assistance_reference_request_conflict", status=409
                    )
                payload = json.loads(existing["payload_json"])
            start_id = session["activeStartId"]

        if existing is None:
            from work_buddy.dashboard.job_registry import search_job_registry

            try:
                results = search_job_registry(
                    reference_kind=reference_kind, query=normalized_query
                )
            except ValueError as exc:
                raise AssistanceError(
                    "assistance_reference_not_allowed", status=403
                ) from exc
            payload = {
                "protocol": "wb.assisted-draft.reference/v1",
                "assistant_session_id": assistant_session_id,
                "conversation_id": scope["conversation_id"],
                "message_id": message_id,
                "request_id": request_id,
                "reference_kind": reference_kind,
                "query": normalized_query,
                "results": results,
                "reference_receipt_id": "arr-"
                + digest(
                    {
                        "session": assistant_session_id,
                        "start": start_id,
                        "generation": scope["generation"],
                        "message": message_id,
                        "context_receipt": consumption_receipt_id,
                        "request": request_id,
                    }
                )[:32],
            }
            if len(canonical(payload).encode("utf-8")) > MAX_REFERENCE_PAYLOAD_BYTES:
                raise AssistanceError("assistance_reference_too_large")
            with self._transaction() as conn:
                current = self._worker_scope(
                    **scope, conn=conn, require_initial=False
                )
                if (
                    current["assistantSessionId"] != assistant_session_id
                    or current["activeStartId"] != start_id
                ):
                    raise conversations.ConversationLeaseLost("lease_lost")
                receipt = self._consumed(
                    conn, current, scope["generation"], message_id
                )
                if receipt["receipt_id"] != consumption_receipt_id:
                    raise AssistanceError(
                        "assistance_receipt_mismatch", status=409
                    )
                raced = conn.execute(
                    "SELECT * FROM assisted_draft_reference_receipts WHERE session_id=? AND start_id=? AND generation=? AND request_id=?",
                    (
                        assistant_session_id,
                        start_id,
                        scope["generation"],
                        request_id,
                    ),
                ).fetchone()
                if raced is None:
                    conn.execute(
                        "INSERT INTO assisted_draft_reference_receipts(session_id,start_id,generation,request_id,request_hash,payload_json) VALUES (?,?,?,?,?,?)",
                        (
                            assistant_session_id,
                            start_id,
                            scope["generation"],
                            request_id,
                            request_hash,
                            canonical(payload),
                        ),
                    )
                elif raced["request_hash"] != request_hash:
                    raise AssistanceError(
                        "assistance_reference_request_conflict", status=409
                    )
                else:
                    payload = json.loads(raced["payload_json"])

        entry = self._account(
            session, payload, "assisted_draft_reference_search"
        )
        with self._transaction() as conn:
            current = self._worker_scope(
                **scope, conn=conn, require_initial=False
            )
            if current["activeStartId"] != start_id:
                raise conversations.ConversationLeaseLost("lease_lost")
            stored = conn.execute(
                "SELECT * FROM assisted_draft_reference_receipts WHERE session_id=? AND start_id=? AND generation=? AND request_id=?",
                (
                    assistant_session_id,
                    start_id,
                    scope["generation"],
                    request_id,
                ),
            ).fetchone()
            if (
                stored is None
                or stored["request_hash"] != request_hash
                or stored["payload_json"] != canonical(payload)
            ):
                raise AssistanceError("assistance_reference_changed", status=409)
            self._validate_accounted_input(entry)
            conn.execute(
                "UPDATE assisted_draft_reference_receipts SET disclosed=1 WHERE session_id=? AND start_id=? AND generation=? AND request_id=?",
                (
                    assistant_session_id,
                    start_id,
                    scope["generation"],
                    request_id,
                ),
            )
        return payload

    def account_worker_payload(self, *, payload, tool_call_id, **scope) -> None:
        conn = scope.pop("conn", None)
        if conn is not None:
            raise AssistanceError(
                "assistance_disclosure_transaction_invalid",
                "Account input outside the conversation write transaction.",
                409,
            )
        delivered_message_id = None
        with self._transaction() as active:
            session = self._worker_scope(**scope, conn=active)
            native_ids = []
            native_content = {}
            message = payload.get("message")
            if isinstance(message, Mapping):
                message_id = text_id(message.get("message_id"), "message_id")
                turn = active.execute(
                    "SELECT start_id FROM assisted_draft_turns WHERE session_id=? AND message_id=?",
                    (session["assistantSessionId"], message_id),
                ).fetchone()
                if turn is None or turn["start_id"] != session["activeStartId"]:
                    raise AssistanceError("assistance_snapshot_unavailable", status=409)
                native_ids.append(message_id)
                delivered_message_id = message_id
            elif isinstance(payload.get("message_id"), str) and (
                "question" in payload or "response" in payload
            ):
                native_ids.append(payload["message_id"])
                # A structured answer belongs to a separate authored turn;
                # account its native origin without treating the question ID
                # as a received user message or form snapshot.
                for row in active.execute(
                    "SELECT message_id,content,context_json FROM messages WHERE conversation_id=? AND role='user'",
                    (session["conversationId"],),
                ):
                    context = json.loads(row["context_json"] or "{}")
                    if context.get("in_reply_to") == payload["message_id"]:
                        native_ids.append(row["message_id"])
                        native_content[row["message_id"]] = row["content"]
                        if (
                            "response" in payload
                            and payload["response"] != row["content"]
                        ):
                            raise AssistanceError(
                                "assistance_context_changed", status=409
                            )
        entry = self._account(
            session, payload, tool_call_id, native_ids, native_content
        )
        with self._transaction() as active:
            current = self._worker_scope(**scope, conn=active)
            if current["activeStartId"] != session["activeStartId"]:
                raise conversations.ConversationLeaseLost("lease_lost")
            self._validate_accounted_input(entry)
            if delivered_message_id is not None:
                active.execute(
                    "INSERT OR IGNORE INTO assisted_draft_deliveries VALUES (?,?,?,?)",
                    (
                        session["assistantSessionId"],
                        session["activeStartId"],
                        scope["generation"],
                        delivered_message_id,
                    ),
                )

    def bind_worker_output(self, *, message_id, content, **scope) -> dict[str, Any]:
        conn = scope.pop("conn", None)
        text_id(message_id, "message_id")
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise AssistanceError("invalid_assistance_reply")
        with self._transaction(conn) as active:
            session = self._worker_scope(**scope, conn=active)
            if message_id == session["greetingMessageId"]:
                target = session["initialSnapshot"]["messageId"]
            else:
                next_turn = conversations._next_user_message(
                    active, conversation_id=session["conversationId"], consumer=CONSUMER
                )
                target = (
                    next_turn["message_id"]
                    if next_turn is not None
                    else session["initialSnapshot"]["messageId"]
                )
            receipt = self._consumed(active, session, scope["generation"], target)
            binding = self._bind_output(
                session,
                output_ref=f"assisted-message:{session['conversationId']}:{message_id}:{digest(content)}",
                idempotency_key=f"assisted-output:{message_id}",
            )
            producer = {
                key: session["execution"][key]
                for key in (
                    "schema_version",
                    "provider_id",
                    "model_id",
                    "provider_label",
                    "model_label",
                )
            }
            producer["disclosure_manifest_sha256"] = binding.manifest_sha256
            active.execute(
                "UPDATE assisted_draft_context_receipts SET reply_message_id=COALESCE(reply_message_id,?) WHERE receipt_id=?",
                (message_id, receipt["receipt_id"]),
            )
            self._require_work()
            return producer

    def assert_worker_turn_consumed(self, *, message_id, **scope) -> None:
        conn = scope.pop("conn", None)
        with self._transaction(conn) as active:
            session = self._worker_scope(**scope, conn=active)
            self._consumed(active, session, scope["generation"], message_id)
            # A restart may consume the same frozen context then acknowledge
            # an already-durable predecessor reply without duplicating it.
            reply = active.execute(
                "SELECT 1 FROM assisted_draft_context_receipts AS receipt JOIN messages AS message ON message.message_id=receipt.reply_message_id AND message.conversation_id=? WHERE receipt.session_id=? AND receipt.start_id=? AND receipt.message_id=?",
                (
                    session["conversationId"],
                    session["assistantSessionId"],
                    session["activeStartId"],
                    message_id,
                ),
            ).fetchone()
            if reply is None:
                raise AssistanceError(
                    "assistance_reply_required",
                    "Send a durable reply before acknowledging this turn.",
                    409,
                )

    def propose_patch(
        self,
        *,
        assistant_session_id: str,
        message_id: str,
        consumption_receipt_id: str,
        proposal_id: str,
        operations: Any,
        **scope,
    ) -> dict[str, Any]:
        text_id(proposal_id, "proposal_id")
        with self._transaction() as conn:
            session = self._worker_scope(**scope, conn=conn)
            if assistant_session_id != session["assistantSessionId"]:
                raise conversations.ConversationLeaseLost("lease_lost")
            receipt = self._consumed(conn, session, scope["generation"], message_id)
            if receipt["receipt_id"] != consumption_receipt_id:
                raise AssistanceError("assistance_receipt_mismatch", status=409)
            form = form_schema(session["identity"]["draftName"], session["schema"])
            validated = validate_operations(form, operations)
            turn = conn.execute(
                "SELECT * FROM assisted_draft_turns WHERE session_id=? AND start_id=? AND message_id=?",
                (assistant_session_id, session["activeStartId"], message_id),
            ).fetchone()
            prepared = json.loads(turn["snapshot_json"])
            patch = {
                "protocol": "wb.assisted-draft.patch/v1",
                "assistantSessionId": assistant_session_id,
                "conversationId": session["conversationId"],
                "identity": session["identity"],
                "schema": session["schema"],
                "baseDraftRevision": prepared["baseDraftRevision"],
                "baseSnapshotHash": prepared["baseSnapshotHash"],
                "baseSnapshot": prepared["snapshot"],
                "patchId": "ap-"
                + digest(
                    {
                        "session": assistant_session_id,
                        "start": session["activeStartId"],
                        "message": message_id,
                        "proposal": proposal_id,
                    }
                )[:32],
                "operations": validated,
            }
            if turn["patch_json"] is not None:
                existing = json.loads(turn["patch_json"])
                if existing != patch:
                    raise AssistanceError(
                        "assistance_patch_conflict",
                        "This frozen turn already has a different patch.",
                        409,
                    )
                return {
                    "patch_id": existing["patchId"],
                    "created": False,
                    "replayed": True,
                }
            current = conversations._next_user_message(
                conn, conversation_id=session["conversationId"], consumer=CONSUMER
            )
            expected_message_id = (
                current["message_id"]
                if current is not None
                else session["initialSnapshot"]["messageId"]
            )
            if message_id != expected_message_id:
                raise AssistanceError(
                    "assistance_turn_not_current",
                    "Propose against the currently received form context.",
                    409,
                )
            self._bind_output(
                session,
                output_ref=f"assisted-patch:{patch['patchId']}:{digest(patch)}",
                idempotency_key=f"assisted-patch:{patch['patchId']}",
            )
            self._require_work()
            conn.execute(
                "UPDATE assisted_draft_turns SET patch_json=?,state='replied' WHERE session_id=? AND message_id=?",
                (canonical(patch), assistant_session_id, message_id),
            )
            return {"patch_id": patch["patchId"], "created": True, "replayed": False}


_default_broker: AssistanceBroker | None = None
_default_lock = threading.Lock()


def get_assistance_broker() -> AssistanceBroker:
    global _default_broker
    if _default_broker is None:
        with _default_lock:
            if _default_broker is None:
                _default_broker = AssistanceBroker()
    return _default_broker


def assert_worker_scope(
    *, agent_session_id, conversation_id, consumer, generation, conn=None
):
    return get_assistance_broker().assert_worker_scope(
        agent_session_id=agent_session_id,
        conversation_id=conversation_id,
        consumer=consumer,
        generation=generation,
        conn=conn,
    )


def account_worker_payload(
    *,
    agent_session_id,
    conversation_id,
    consumer,
    generation,
    payload,
    tool_call_id,
    conn=None,
):
    return get_assistance_broker().account_worker_payload(
        agent_session_id=agent_session_id,
        conversation_id=conversation_id,
        consumer=consumer,
        generation=generation,
        payload=payload,
        tool_call_id=tool_call_id,
        conn=conn,
    )


def bind_worker_output(
    *,
    agent_session_id,
    conversation_id,
    consumer,
    generation,
    message_id,
    content,
    conn=None,
):
    return get_assistance_broker().bind_worker_output(
        agent_session_id=agent_session_id,
        conversation_id=conversation_id,
        consumer=consumer,
        generation=generation,
        message_id=message_id,
        content=content,
        conn=conn,
    )


def assert_worker_turn_consumed(
    *, agent_session_id, conversation_id, consumer, generation, message_id, conn=None
):
    return get_assistance_broker().assert_worker_turn_consumed(
        agent_session_id=agent_session_id,
        conversation_id=conversation_id,
        consumer=consumer,
        generation=generation,
        message_id=message_id,
        conn=conn,
    )
