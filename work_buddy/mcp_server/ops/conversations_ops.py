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
    from contextlib import nullcontext
    from work_buddy.conversations.store import (
        ConversationLeaseLost,
        create_conversation as _create_conversation,
        get_conversation as _get_conversation,
        get_conversation_with_messages as _get_conv_msgs,
        add_message as _add_msg,
        send_agent_message_idempotent as _send_agent_idempotent,
        get_pending_question as _get_pending,
        respond_to_conversation as _respond_conv,
        receive_user_message as _receive_user,
        ack_user_message as _ack_user,
        close_conversation as _close_conversation,
        list_conversations as _list_conversations,
        conversation_agent_write_guard as _agent_write_guard,
    )
    from work_buddy.conversations.execution import (
        producer_for_lease as _producer_for_lease,
    )

    def _write_fence(
        conversation_id,
        consumer,
        generation,
        agent_session_id,
    ):
        from work_buddy.cowork.execution_identity import (
            cowork_generation_from_session,
        )

        session_generation = cowork_generation_from_session(
            agent_session_id
        )
        if session_generation is not None:
            if consumer is None or generation is None:
                raise ValueError(
                    "Co-work execution requires consumer and generation"
                )
            if generation != session_generation:
                raise ConversationLeaseLost("lease_lost")
        if consumer is None and generation is None:
            return nullcontext(None)
        if consumer is None or generation is None:
            raise ValueError("consumer and generation must be provided together")
        return _agent_write_guard(conversation_id, consumer, generation)

    def _trusted_producer(
        conversation_id,
        consumer,
        generation,
        lease_conn,
        agent_session_id,
    ):
        producer = None
        if consumer is not None and generation is not None and lease_conn is not None:
            producer = _producer_for_lease(
                conversation_id=conversation_id,
                consumer=consumer,
                generation=generation,
                conn=lease_conn,
            )
        from work_buddy.cowork.execution_identity import (
            cowork_generation_from_session,
        )

        if (
            cowork_generation_from_session(agent_session_id) is not None
            and producer is None
        ):
            raise ConversationLeaseLost("lease_lost")
        return producer

    def _verify_cowork_read_scope(
        conversation_id,
        consumer,
        generation,
        agent_session_id,
    ) -> None:
        """Bind hosted Co-work reads to their exact active lease."""

        from work_buddy.cowork.execution_identity import (
            cowork_generation_from_session,
        )

        session_generation = cowork_generation_from_session(
            agent_session_id
        )
        if session_generation is None:
            return
        if consumer is None or generation is None:
            raise ValueError(
                "Co-work execution requires consumer and generation"
            )
        if generation != session_generation:
            raise ConversationLeaseLost("lease_lost")
        with _agent_write_guard(
            conversation_id,
            consumer,
            generation,
        ):
            pass

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

    def conversation_send(
        conversation_id: str,
        message: str,
        message_id: str | None = None,
        consumer: str | None = None,
        generation: str | None = None,
        agent_session_id: str | None = None,
    ) -> dict:
        try:
            with _write_fence(
                conversation_id,
                consumer,
                generation,
                agent_session_id,
            ) as lease_conn:
                producer = _trusted_producer(
                    conversation_id,
                    consumer,
                    generation,
                    lease_conn,
                    agent_session_id,
                )
                if message_id is None:
                    msg = _add_msg(
                        conversation_id,
                        "agent",
                        message,
                        conn=lease_conn,
                        producer=producer,
                    )
                    created = msg is not None
                else:
                    msg, created = _send_agent_idempotent(
                        conversation_id,
                        message,
                        message_id,
                        conn=lease_conn,
                        producer=producer,
                    )
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return {"status": "invalid_request", "error": str(exc)}
        if msg is None:
            return {
                "error": f"Conversation not found or closed: {conversation_id}",
            }
        # Frontend polls /api/conversations/<id> for new messages
        return {
            "message_id": msg.message_id,
            "conversation_id": conversation_id,
            "created": created,
            "replayed": not created,
        }

    def conversation_ask(
        conversation_id: str,
        question: str,
        response_type: str = "freeform",
        choices: list | None = None,
        timeout_seconds: int | None = None,
        consumer: str | None = None,
        generation: str | None = None,
        agent_session_id: str | None = None,
    ) -> dict:
        choice_dicts = None
        if choices:
            choice_dicts = []
            for c in choices:
                if isinstance(c, str):
                    choice_dicts.append({"key": c, "label": c})
                elif isinstance(c, dict):
                    choice_dicts.append(c)

        try:
            with _write_fence(
                conversation_id,
                consumer,
                generation,
                agent_session_id,
            ) as lease_conn:
                producer = _trusted_producer(
                    conversation_id,
                    consumer,
                    generation,
                    lease_conn,
                    agent_session_id,
                )
                msg = _add_msg(
                    conversation_id,
                    "agent",
                    question,
                    message_type="question",
                    response_type=response_type,
                    choices=choice_dicts,
                    conn=lease_conn,
                    producer=producer,
                )
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return {"status": "invalid_request", "error": str(exc)}
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
                try:
                    with _write_fence(
                        conversation_id,
                        consumer,
                        generation,
                        agent_session_id,
                    ) as lease_conn:
                        pending = _get_pending(
                            conversation_id,
                            conn=lease_conn,
                        )
                        data = (
                            _get_conv_msgs(
                                conversation_id,
                                conn=lease_conn,
                            )
                            if pending is None
                            or pending.status == "answered"
                            else None
                        )
                except ConversationLeaseLost:
                    return {
                        "status": "lease_lost",
                        "conversation_id": conversation_id,
                    }
                if pending is None or pending.status == "answered":
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
        consumer: str | None = None,
        generation: str | None = None,
        agent_session_id: str | None = None,
    ) -> dict:
        def _scoped_snapshot():
            with _write_fence(
                conversation_id,
                consumer,
                generation,
                agent_session_id,
            ) as lease_conn:
                pending_question = _get_pending(
                    conversation_id,
                    conn=lease_conn,
                )
                conversation_data = (
                    _get_conv_msgs(
                        conversation_id,
                        conn=lease_conn,
                    )
                    if pending_question is None
                    else None
                )
                return pending_question, conversation_data

        try:
            pending, data = _scoped_snapshot()
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return {"status": "invalid_request", "error": str(exc)}
        if pending is None:
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
            try:
                p, data = _scoped_snapshot()
            except ConversationLeaseLost:
                return {
                    "status": "lease_lost",
                    "conversation_id": conversation_id,
                }
            if p is None:
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

    def conversation_receive(
        conversation_id: str,
        consumer: str,
        generation: str,
        timeout_seconds: int | None = None,
        agent_session_id: str | None = None,
    ) -> dict:
        """Receive the oldest unacked user turn for one leased consumer."""
        try:
            _verify_cowork_read_scope(
                conversation_id,
                consumer,
                generation,
                agent_session_id,
            )
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return {"status": "invalid_request", "error": str(exc)}
        return _receive_user(
            conversation_id,
            consumer,
            generation,
            timeout_seconds=0 if timeout_seconds is None else timeout_seconds,
        )

    def conversation_ack(
        conversation_id: str,
        consumer: str,
        generation: str,
        message_id: str,
        agent_session_id: str | None = None,
    ) -> dict:
        """Acknowledge exactly the currently delivered oldest user turn."""
        try:
            _verify_cowork_read_scope(
                conversation_id,
                consumer,
                generation,
                agent_session_id,
            )
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return {"status": "invalid_request", "error": str(exc)}
        return _ack_user(
            conversation_id,
            consumer,
            generation,
            message_id,
        )

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
    register_op("op.wb.conversation_receive", conversation_receive)
    register_op("op.wb.conversation_ack", conversation_ack)
    register_op("op.wb.conversation_close", conversation_close)
    register_op("op.wb.conversation_list", conversation_list)


_register()
