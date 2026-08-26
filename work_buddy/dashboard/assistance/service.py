"""Conversations-backed advisory broker. No current-draft or domain authority.

The extension tables bind a conversation to an immutable host identity and keep
per-message snapshots/patch receipts. The only live draft is in the mounted
widget; this module cannot invoke a capability, submit a form, or touch a DOM.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from work_buddy.conversations import store as conversations

from .contracts import (
    AssistanceError,
    canonical,
    digest,
    form_schema,
    text_id,
    validate_identity,
    validate_operations,
    validate_snapshot,
)
from .runner import AssistanceRunner, SourceBoundAssistanceRunner

CONSUMER = "dashboard.assisted-draft"
MAX_TURNS = 40


class AssistanceBroker:
    def __init__(self, *, runner: AssistanceRunner | None = None, dispatch: Callable[[Callable[[], None]], None] | None = None, read_only: Callable[[], bool] | None = None):
        self.runner = runner or SourceBoundAssistanceRunner()
        self.dispatch = dispatch or self._background
        self.read_only = read_only or (lambda: False)
        self._initialized = False
        self._init_lock = threading.Lock()

    @staticmethod
    def _background(callback: Callable[[], None]) -> None:
        threading.Thread(target=callback, name="assisted-draft-turn", daemon=True).start()

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
                      state TEXT NOT NULL DEFAULT 'prepared',
                      PRIMARY KEY(session_id, message_id),
                      FOREIGN KEY(session_id) REFERENCES assisted_draft_sessions(session_id)
                    );
                    """)
                    conn.commit()
                    self._initialized = True
        return conn

    def availability(self) -> dict[str, Any]:
        return self.runner.availability()

    def start(self, body: Mapping[str, Any], actor: str) -> dict[str, Any]:
        if self.read_only():
            raise AssistanceError("dashboard_read_only", status=403)
        if set(body) != {"requestId", "identity", "schema", "interactionMode", "readOnly", "disclosureAccepted", "providerId", "modelId"}:
            raise AssistanceError("invalid_assistance_request")
        if body.get("interactionMode") != "operate" or body.get("readOnly") is not False:
            raise AssistanceError("assistance_mode_blocked", status=403)
        if body.get("disclosureAccepted") is not True:
            raise AssistanceError("disclosure_gesture_required", status=403)
        identity = validate_identity(body.get("identity"))
        form = form_schema(identity["draftName"], body.get("schema"))
        request_id = text_id(body.get("requestId"), "request_id")
        session_id = "as-" + digest({"actor": actor, "request": request_id})[:32]
        request_hash = digest({"identity": identity, "schema": form["schema"], "providerId": body["providerId"], "modelId": body["modelId"]})
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT * FROM assisted_draft_sessions WHERE session_id=?", (session_id,)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise AssistanceError("request_id_conflict", status=409)
                return self._public(self._read_session(prior, actor))
            availability = self.availability()
            if not availability["available"]:
                raise AssistanceError(availability["code"], availability["message"], 503)
            if body["providerId"] != availability.get("providerId") or body["modelId"] != availability.get("modelId"):
                raise AssistanceError("provider_selection_changed", "The provider or model changed. Review the updated disclosure and start again.", 409)
            expires = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
            # Canonical Conversations creation commits its own operation. The
            # following binding is in the same SQLite database. A stable source
            # recovers that narrow crash boundary without a second transcript.
            source = f"assisted-draft:{session_id}"
            existing = conn.execute("SELECT conversation_id FROM conversations WHERE source=?", (source,)).fetchone()
            if existing:
                conversation_id = existing["conversation_id"]
            else:
                conversation = conversations.create_conversation(title=form["title"], source=source, metadata={"assistedDraft": {"sessionId": session_id, "identity": identity, "submitPolicy": "user_only"}}, conn=conn)
                conversation_id = conversation.conversation_id
                conn.execute("BEGIN IMMEDIATE")
            binding = {"assistantSessionId": session_id, "conversationId": conversation_id, "identity": identity, "schema": form["schema"], "expiresAt": expires, "availability": availability, "authorizationRef": f"assistance-start:{actor}:{request_id}"}
            conn.execute("INSERT OR IGNORE INTO assisted_draft_sessions VALUES (?,?,?,?,?,?)", (session_id, actor, request_hash, conversation_id, canonical(binding), expires))
            conn.commit()
            persisted = conn.execute("SELECT * FROM assisted_draft_sessions WHERE session_id=?", (session_id,)).fetchone()
            if persisted["request_hash"] != request_hash:
                raise AssistanceError("request_id_conflict", status=409)
            return self._public(self._read_session(persisted, actor))
        finally:
            conn.close()

    @staticmethod
    def _public(value: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if key != "authorizationRef"}

    def _read_session(self, row: Any, actor: str | None) -> dict[str, Any]:
        if row is None or (actor is not None and row["actor_id"] != actor):
            raise AssistanceError("assistance_session_not_found", status=404)
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            raise AssistanceError("assistance_session_expired", "This assistance session expired. Your form draft is unchanged.", 410)
        return json.loads(row["binding_json"])

    def session(self, session_id: str, actor: str | None, *, internal: bool = False) -> dict[str, Any]:
        conn = self._connection()
        try:
            result = self._read_session(conn.execute("SELECT * FROM assisted_draft_sessions WHERE session_id=?", (session_id,)).fetchone(), actor)
            return result if internal else self._public(result)
        finally:
            conn.close()

    def prepare(self, session_id: str, actor: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.read_only():
            raise AssistanceError("dashboard_read_only", status=403)
        if set(body) != {"messageId", "baseDraftRevision", "baseSnapshotHash", "snapshot"}:
            raise AssistanceError("invalid_assistance_snapshot")
        session = self.session(session_id, actor)
        form = form_schema(session["identity"]["draftName"], session["schema"])
        message_id = text_id(body.get("messageId"), "message_id")
        revision = body.get("baseDraftRevision")
        if type(revision) is not int or revision < 0:
            raise AssistanceError("invalid_draft_revision")
        snapshot = validate_snapshot(form, body.get("snapshot"))
        if body.get("baseSnapshotHash") != digest(snapshot):
            raise AssistanceError("snapshot_hash_mismatch")
        prepared = {"messageId": message_id, "baseDraftRevision": revision, "baseSnapshotHash": digest(snapshot), "snapshot": snapshot}
        request_hash = digest(prepared)
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT snapshot_hash FROM assisted_draft_turns WHERE session_id=? AND message_id=?", (session_id, message_id)).fetchone()
            if old:
                if old["snapshot_hash"] != request_hash:
                    raise AssistanceError("message_id_conflict", status=409)
            else:
                count = conn.execute("SELECT COUNT(*) FROM assisted_draft_turns WHERE session_id=?", (session_id,)).fetchone()[0]
                if count >= MAX_TURNS:
                    raise AssistanceError("assistance_turn_limit", "Start a new assistance session to continue.", 429)
                conn.execute("INSERT INTO assisted_draft_turns(session_id,message_id,snapshot_hash,snapshot_json) VALUES(?,?,?,?)", (session_id, message_id, request_hash, canonical(prepared)))
            conn.commit()
            return {"prepared": True, "messageId": message_id}
        finally:
            conn.close()

    def respond(self, session_id: str, conversation_id: str, actor: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.read_only():
            raise AssistanceError("dashboard_read_only", status=403)
        session = self.session(session_id, actor)
        if conversation_id != session["conversationId"]:
            raise AssistanceError("assistance_binding_mismatch", status=409)
        if set(body) - {"value", "message_id"}:
            raise AssistanceError("invalid_assistance_turn")
        message_id = text_id(body.get("message_id"), "message_id")
        value = body.get("value")
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 8192:
            raise AssistanceError("invalid_assistance_turn")
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM assisted_draft_turns WHERE session_id=? AND message_id=?", (session_id, message_id)).fetchone()
            if row is None:
                raise AssistanceError("snapshot_required")
            user_hash = digest({"value": value, "snapshotHash": row["snapshot_hash"]})
            if row["user_hash"] is not None and row["user_hash"] != user_hash:
                raise AssistanceError("message_id_conflict", status=409)
            snapshot = json.loads(row["snapshot_json"])
            message = conversations.post_user_message(conversation_id, value, message_id=message_id, context={"kind": "assisted_draft", "assistant_session_id": session_id, "base_snapshot_hash": snapshot["baseSnapshotHash"]}, conn=conn)
            if message is None:
                raise AssistanceError("conversation_closed", status=409)
            conn.execute("UPDATE assisted_draft_turns SET user_hash=?, state=CASE WHEN state='prepared' THEN 'queued' ELSE state END WHERE session_id=? AND message_id=?", (user_hash, session_id, message_id))
            conn.commit()
        finally:
            conn.close()
        self._wake(session_id)
        return {"message_id": message_id}

    def _wake(self, session_id: str) -> None:
        if self.read_only():
            return
        session = self.session(session_id, None, internal=True)
        conversation_id = session["conversationId"]
        generation = uuid.uuid4().hex
        lease = conversations.claim_agent_lease(conversation_id, CONSUMER, generation, execution={"provider_id": session["availability"].get("providerId"), "model_id": session["availability"].get("modelId")})
        if not lease or not lease["claimed"]:
            return
        try:
            conversations.activate_agent_lease(conversation_id, CONSUMER, generation, os.getpid())
            self.dispatch(lambda: self._drive(session_id, generation))
        except Exception:  # noqa: BLE001 - retain the authored turn and release its failed driver lease
            conversations.fail_agent_lease(conversation_id, CONSUMER, generation, error="assistance_start_failed")
            # The authored turn is durable; a retry reuses it and the form is
            # never locked. Do not pretend this is an undelivered chat turn.

    def _drive(self, session_id: str, generation: str) -> None:
        session = self.session(session_id, None, internal=True)
        conversation_id = session["conversationId"]
        drained = False
        try:
            for _ in range(MAX_TURNS):
                if self.read_only():
                    break
                received = conversations.receive_user_message(conversation_id, CONSUMER, generation)
                if received["status"] != "message":
                    drained = received["status"] == "empty"
                    break
                message = received["message"]
                message_id = message["message_id"]
                conn = self._connection()
                try:
                    row = conn.execute("SELECT * FROM assisted_draft_turns WHERE session_id=? AND message_id=?", (session_id, message_id)).fetchone()
                finally:
                    conn.close()
                if row is None:
                    # A bypassed generic conversation turn never becomes model
                    # input: only this surface can stage disclosure context.
                    conversations.ack_user_message(conversation_id, CONSUMER, generation, message_id)
                    continue
                prepared = json.loads(row["snapshot_json"])
                response_id = "assist-reply-" + digest({"session": session_id, "message": message_id})[:32]
                form = form_schema(session["identity"]["draftName"])
                reply = "Assistance could not finish this turn. Your form is unchanged. You can continue manually or send another message to resume."
                producer = None
                patch = None
                try:
                    transcript = conversations.get_conversation_with_messages(conversation_id)["messages"]
                    # Never feed turns newer than the exact inbox message into
                    # this patch's base context, even with concurrent authors.
                    index = next(i for i, item in enumerate(transcript) if item["message_id"] == message_id)
                    prior = [{"role": item["role"], "content": item["content"]} for item in transcript[:index]][-12:]
                    payload = {"form": {"title": form["title"], "fields": form["fields"], "submitPolicy": "user_only"}, "draft": prepared["snapshot"], "conversation": prior, "userMessage": message["content"]}
                    while len(canonical(payload).encode("utf-8")) > 64 * 1024 and prior:
                        prior.pop(0)
                    # On recovery, an existing immutable patch/reply wins. A
                    # model is not replayed merely to regenerate its wording.
                    if row["patch_json"] is not None:
                        patch = json.loads(row["patch_json"])
                        existing = next((item for item in transcript if item["message_id"] == response_id), None)
                        if existing:
                            reply, producer = existing["content"], existing.get("producer")
                    else:
                        if self.read_only():
                            break
                        result = self.runner.run(session=session, turn_id=digest(message_id)[:24], payload=payload, form=form)
                        if set(result) - {"reply", "operations", "producer"}:
                            raise AssistanceError("invalid_assistance_reply")
                        reply = result.get("reply")
                        if not isinstance(reply, str) or not reply.strip() or len(reply) > 4000:
                            raise AssistanceError("invalid_assistance_reply")
                        operations = validate_operations(form, result.get("operations"))
                        producer = result.get("producer")
                        patch = {"protocol": "wb.assisted-draft.patch/v1", "assistantSessionId": session_id, "conversationId": conversation_id, "identity": session["identity"], "schema": session["schema"], "baseDraftRevision": prepared["baseDraftRevision"], "baseSnapshotHash": prepared["baseSnapshotHash"], "baseSnapshot": prepared["snapshot"], "patchId": "ap-" + digest({"session": session_id, "message": message_id})[:32], "operations": operations}
                except Exception:  # noqa: BLE001 - sanitize model failures; never expose prompts or provider errors
                    # Stable, content-free failure; raw prompts/model errors
                    # must not enter logs or leak through public error text.
                    reply = "Assistance could not safely produce a patch. Your form is unchanged. Continue manually, or send a message to resume."
                    patch = None
                    producer = None
                if self.read_only():
                    break
                with conversations.conversation_agent_write_guard(conversation_id, CONSUMER, generation) as conn:
                    # Commit advisory patch and canonical reply together. The
                    # Conversations writer commits the encompassing transaction.
                    conn.execute("UPDATE assisted_draft_turns SET patch_json=?, state=? WHERE session_id=? AND message_id=?", (canonical(patch) if patch else None, "replied" if patch else "failed", session_id, message_id))
                    conversations.send_agent_message_idempotent(conversation_id, reply, response_id, conn=conn, producer=producer)
                conversations.ack_user_message(conversation_id, CONSUMER, generation, message_id)
        except conversations.ConversationLeaseLost:
            pass
        finally:
            lease = conversations.get_agent_lease(conversation_id, CONSUMER)
            owned = bool(lease and lease["generation"] == generation and lease["status"] == "running")
            conversations.stop_agent_lease(conversation_id, CONSUMER, generation)
            if drained and owned and not self.read_only():
                # Close the empty-inbox / lease-stop race. A human turn may
                # have queued while this still-live driver was winding down.
                conn = self._connection()
                try:
                    queued = conn.execute("SELECT 1 FROM assisted_draft_turns WHERE session_id=? AND state='queued' LIMIT 1", (session_id,)).fetchone()
                finally:
                    conn.close()
                if queued:
                    self._wake(session_id)

    def conversation(self, session_id: str, conversation_id: str, actor: str) -> dict[str, Any]:
        session = self.session(session_id, actor)
        if conversation_id != session["conversationId"]:
            raise AssistanceError("assistance_binding_mismatch", status=409)
        payload = conversations.get_conversation_with_messages(conversation_id)
        if payload is None:
            raise AssistanceError("conversation_not_found", status=404)
        lease = conversations.get_agent_lease(conversation_id, CONSUMER)
        payload["conversation"]["agent_alive"] = bool(lease and lease["status"] in {"starting", "running"})
        return payload

    def patches(self, session_id: str, actor: str) -> list[dict[str, Any]]:
        self.session(session_id, actor)
        conn = self._connection()
        try:
            return [{"patch": json.loads(row["patch_json"]), "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None} for row in conn.execute("SELECT patch_json, receipt_json FROM assisted_draft_turns WHERE session_id=? AND patch_json IS NOT NULL ORDER BY rowid", (session_id,))]
        finally:
            conn.close()

    def acknowledge(self, session_id: str, actor: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.read_only():
            raise AssistanceError("dashboard_read_only", status=403)
        self.session(session_id, actor)
        patch_id = text_id(body.get("patchId"), "patch_id")
        if set(body) != {"patchId", "status", "appliedFields", "pendingFields", "resultingRevision", "message"}:
            raise AssistanceError("invalid_patch_receipt")
        if body["status"] not in {"applied", "pending", "partial", "rejected", "undone"} or type(body["resultingRevision"]) is not int or body["resultingRevision"] < 0 or not isinstance(body["message"], str) or len(body["message"]) > 1000:
            raise AssistanceError("invalid_patch_receipt")
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM assisted_draft_turns WHERE session_id=? AND patch_json IS NOT NULL", (session_id,)).fetchall()
            row = next((row for row in rows if json.loads(row["patch_json"])["patchId"] == patch_id), None)
            if row is None:
                raise AssistanceError("patch_not_found", status=404)
            allowed = {canonical(op["path"]) for op in json.loads(row["patch_json"])["operations"]}
            if not isinstance(body["appliedFields"], list) or not isinstance(body["pendingFields"], list):
                raise AssistanceError("invalid_patch_receipt")
            paths = [*body["appliedFields"]]
            for pending in body["pendingFields"]:
                if not isinstance(pending, dict) or set(pending) != {"path", "reason"} or pending["reason"] not in {"focused", "user_changed", "suggest_only", "storage_conflict"}:
                    raise AssistanceError("invalid_patch_receipt")
                paths.append(pending["path"])
            if any(canonical(path) not in allowed for path in paths) or len({canonical(path) for path in paths}) != len(paths):
                raise AssistanceError("invalid_patch_receipt")
            prior = json.loads(row["receipt_json"]) if row["receipt_json"] else None
            # Ack retries are stable; later explicit review/undo is allowed to
            # replace an earlier receipt, never to claim a different patch.
            if prior and prior == body:
                return prior
            if prior and (body["resultingRevision"] < prior["resultingRevision"] or prior["status"] in {"rejected", "undone"}):
                raise AssistanceError("patch_receipt_conflict", "A newer host receipt already owns this patch.", 409)
            conn.execute("UPDATE assisted_draft_turns SET receipt_json=? WHERE session_id=? AND message_id=?", (canonical(body), session_id, row["message_id"]))
            conn.commit()
            return dict(body)
        finally:
            conn.close()

    def stop(self, session_id: str, actor: str) -> dict[str, Any]:
        session = self.session(session_id, actor)
        lease = conversations.get_agent_lease(session["conversationId"], CONSUMER)
        if lease:
            conversations.stop_agent_lease(session["conversationId"], CONSUMER, lease["generation"])
        return {"stopped": True}
