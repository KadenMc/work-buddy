"""HTTP API for inter-agent messaging.

Run with:  python -m work_buddy.messaging.service
"""

import json
import sys

from flask import Flask, Response, jsonify, request

from work_buddy.config import load_config
from work_buddy.messaging.models import (
    create_message,
    create_reply,
    get_connection,
    get_message,
    get_thread,
    query_messages,
    record_read,
    summarize_pending,
    update_status,
)

app = Flask(__name__)

_cfg = load_config()


def _get_conn():
    """Get a thread-local database connection.

    SQLite connections cannot be shared across threads. Flask's dev server
    (and any threaded WSGI server) dispatches requests on worker threads,
    so we create one connection per request using Flask's ``g`` object.
    """
    from flask import g
    if "_msg_conn" not in g:
        g._msg_conn = get_connection(_cfg)
    return g._msg_conn


@app.teardown_appcontext
def _close_conn(exc):
    from flask import g
    conn = g.pop("_msg_conn", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/messages")
def list_messages():
    recipient = request.args.get("recipient")
    session = request.args.get("session")
    status = request.args.get("status")
    sender = request.args.get("sender")
    thread_id = request.args.get("thread_id")
    fmt = request.args.get("format")
    limit = request.args.get("limit", 50, type=int)

    # Hook context format — return pre-wrapped additionalContext JSON
    if fmt == "context":
        hook_event = request.args.get("hook_event", "SessionStart")
        ttl_days = request.args.get("ttl_days", None, type=int)
        include_instructions = (hook_event == "SessionStart")
        summary = summarize_pending(
            _get_conn(), recipient or "", session,
            ttl_days=ttl_days, include_instructions=include_instructions,
        )
        if not summary:
            # No messages — return empty so hook is a no-op
            return Response("", status=204)
        return jsonify({
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "additionalContext": summary,
            }
        })

    msgs = query_messages(
        _get_conn(),
        recipient=recipient,
        session=session,
        status=status,
        sender=sender,
        thread_id=thread_id,
        limit=limit,
    )
    # Strip body from list responses — agents must GET /messages/<id> for full content
    for m in msgs:
        m.pop("body", None)
    return jsonify({"count": len(msgs), "messages": msgs})


@app.get("/messages/<msg_id>")
def get_single_message(msg_id: str):
    conn = _get_conn()

    # Auto-record read before fetching so the response reflects it
    session = request.args.get("session")
    reader_project = request.args.get("reader_project")
    if session:
        record_read(conn, msg_id, session, reader_project=reader_project)

    msg = get_message(conn, msg_id)
    if msg is None:
        return jsonify({"error": "not found"}), 404

    return jsonify(msg)


@app.post("/messages")
def send_message():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ("sender", "recipient", "type", "subject")
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    tags = data.get("tags")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = [tags]

    msg = create_message(
        _get_conn(),
        sender=data["sender"],
        recipient=data["recipient"],
        type=data["type"],
        subject=data["subject"],
        body=data.get("body"),
        sender_session=data.get("sender_session"),
        recipient_session=data.get("recipient_session"),
        thread_id=data.get("thread_id"),
        priority=data.get("priority", "normal"),
        in_reply_to=data.get("in_reply_to"),
        tags=tags,
    )
    return jsonify(msg), 201


@app.patch("/messages/<msg_id>")
def patch_message(msg_id: str):
    data = request.get_json(silent=True)
    if not data or "status" not in data:
        return jsonify({"error": "JSON body with 'status' required"}), 400

    msg = update_status(_get_conn(), msg_id, data["status"])
    if msg is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(msg)


@app.get("/threads/<thread_id>")
def get_thread_messages(thread_id: str):
    msgs = get_thread(_get_conn(), thread_id)
    return jsonify({"thread_id": thread_id, "count": len(msgs), "messages": msgs})


@app.post("/messages/<msg_id>/reply")
def reply_to_message(msg_id: str):
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ("sender", "body")
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    msg = create_reply(
        _get_conn(),
        msg_id,
        sender=data["sender"],
        body=data["body"],
        sender_session=data.get("sender_session"),
        recipient_session=data.get("recipient_session"),
        type=data.get("type", "ack"),
        priority=data.get("priority", "normal"),
        tags=data.get("tags"),
    )
    if msg is None:
        return jsonify({"error": "parent message not found"}), 404
    return jsonify(msg), 201


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    port = _cfg.get("messaging", {}).get("service_port", 5123)
    print(f"work-buddy messaging service starting on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
