"""Conversations-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The closure code below
is moved verbatim from the former ``registry.py`` builder.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op



def _register() -> None:
    """Conversation capabilities — multi-turn agent-user dialogue.

    Conversations are a standalone subsystem backed by SQLite. The
    dashboard renders them in a sidebar chat panel.

    Renamed from ``_thread_capabilities``; the ``thread`` namespace
    is reserved for the universal-entity primitive in
    :mod:`work_buddy.threads`.
    """
    import os
    import time
    import urllib.request
    from work_buddy.conversations.store import (
        create_conversation as _create_conversation,
        get_conversation as _get_conversation,
        get_conversation_with_messages as _get_conv_msgs,
        add_message as _add_msg,
        get_pending_question as _get_pending,
        respond_to_conversation as _respond_conv,
        close_conversation as _close_conversation,
        list_conversations as _list_conversations,
    )

    def _notify_conversation_created(
        conversation_id: str, title: str, body: str = "",
    ) -> None:
        """Deliver a conversation_chat notification through the
        notification system.

        Creates a Notification record and delivers via SurfaceDispatcher.
        DashboardSurface.deliver() creates the workflow view, and the
        dashboard poll loop detects it and shows a toast.
        """
        try:
            from work_buddy.notifications.store import (
                create_notification as _create_notif,
                mark_delivered as _mark_delivered,
            )
            from work_buddy.notifications.models import Notification, ResponseType
            from work_buddy.notifications.dispatcher import SurfaceDispatcher

            n = Notification(
                notification_id=f"conversation-{conversation_id}",
                title=title,
                body=body[:100] if body else "New conversation",
                response_type=ResponseType.NONE.value,
                custom_template={
                    "type": "conversation_chat",
                    "conversation_id": conversation_id,
                },
                expandable=True,
            )
            created = _create_notif(n)
            dispatcher = SurfaceDispatcher.from_config()
            dispatcher.deliver(created, mark_delivered_fn=_mark_delivered)
        except Exception:
            pass  # Dashboard/notification system may not be running

    def conversation_create(
        title: str, message: str = "", source: str = "",
    ) -> dict:
        if not source:
            source = f"agent:{os.environ.get('WORK_BUDDY_SESSION_ID', 'unknown')}"
        conv = _create_conversation(title=title, source=source)
        result = {"conversation_id": conv.conversation_id, "status": "created"}

        if message:
            msg = _add_msg(conv.conversation_id, "agent", message)
            if msg:
                result["message_id"] = msg.message_id

        _notify_conversation_created(conv.conversation_id, title, message)
        return result

    def conversation_send(conversation_id: str, message: str) -> dict:
        msg = _add_msg(conversation_id, "agent", message)
        if msg is None:
            return {
                "error": f"Conversation not found or closed: {conversation_id}",
            }
        # Frontend polls /api/conversations/<id> for new messages
        return {
            "message_id": msg.message_id,
            "conversation_id": conversation_id,
        }

    def conversation_ask(
        conversation_id: str,
        question: str,
        response_type: str = "freeform",
        choices: list | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        choice_dicts = None
        if choices:
            choice_dicts = []
            for c in choices:
                if isinstance(c, str):
                    choice_dicts.append({"key": c, "label": c})
                elif isinstance(c, dict):
                    choice_dicts.append(c)

        msg = _add_msg(
            conversation_id, "agent", question,
            message_type="question",
            response_type=response_type,
            choices=choice_dicts,
        )
        if msg is None:
            return {
                "error": f"Conversation not found or closed: {conversation_id}",
            }
        result = {
            "message_id": msg.message_id,
            "conversation_id": conversation_id,
            "status": "pending",
        }

        # Optional blocking poll
        if timeout_seconds is not None:
            timeout_seconds = min(timeout_seconds, 110)
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                pending = _get_pending(conversation_id)
                if pending is None or pending.status == "answered":
                    data = _get_conv_msgs(conversation_id)
                    if data:
                        for m in reversed(data["messages"]):
                            if m.get("message_id") == msg.message_id:
                                result["status"] = "answered"
                                result["response"] = m.get("response")
                                return result
                    result["status"] = "answered"
                    return result
                time.sleep(3)
            result["status"] = "timeout"

        return result

    def conversation_poll(
        conversation_id: str,
        timeout_seconds: int | None = None,
    ) -> dict:
        pending = _get_pending(conversation_id)
        if pending is None:
            data = _get_conv_msgs(conversation_id)
            if not data:
                return {"error": f"Conversation not found: {conversation_id}"}
            answered = [m for m in data["messages"]
                        if m.get("status") == "answered"]
            if answered:
                last = answered[-1]
                return {
                    "status": "answered",
                    "message_id": last["message_id"],
                    "response": last.get("response"),
                }
            return {"status": "no_pending_question"}

        if timeout_seconds is None:
            return {
                "status": "pending",
                "message_id": pending.message_id,
                "question": pending.content,
            }

        timeout_seconds = min(timeout_seconds, 110)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            p = _get_pending(conversation_id)
            if p is None:
                data = _get_conv_msgs(conversation_id)
                if data:
                    answered = [m for m in data["messages"]
                                if m.get("message_id") == pending.message_id]
                    if answered:
                        return {
                            "status": "answered",
                            "message_id": pending.message_id,
                            "response": answered[0].get("response"),
                        }
                return {"status": "answered", "message_id": pending.message_id}
            time.sleep(3)

        return {"status": "timeout", "waited_seconds": timeout_seconds}

    def conversation_close(conversation_id: str) -> dict:
        ok = _close_conversation(conversation_id)
        if not ok:
            return {"error": f"Conversation not found: {conversation_id}"}
        try:
            from work_buddy.notifications.store import cancel_notification
            cancel_notification(f"conversation-{conversation_id}")
        except Exception:
            pass
        try:
            req = urllib.request.Request(
                f"http://localhost:5127/api/workflow-views/conversation-{conversation_id}/dismiss",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
        return {"closed": True, "conversation_id": conversation_id}

    def conversation_list(status: str = "open") -> dict:
        conversations = _list_conversations(
            status=status if status != "all" else None,
        )
        return {
            "conversations": conversations,
            "count": len(conversations),
        }

    register_op("op.wb.conversation_create", conversation_create)
    register_op("op.wb.conversation_send", conversation_send)
    register_op("op.wb.conversation_ask", conversation_ask)
    register_op("op.wb.conversation_poll", conversation_poll)
    register_op("op.wb.conversation_close", conversation_close)
    register_op("op.wb.conversation_list", conversation_list)


_register()
