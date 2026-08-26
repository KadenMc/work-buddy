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
    from contextlib import contextmanager
    from work_buddy.conversations.store import (
        ConversationLeaseLost,
        create_conversation as _create_conversation,
        get_conversation as _get_conversation,
        get_message as _get_message,
        get_conversation_with_messages as _get_conv_msgs,
        add_message as _add_msg,
        send_agent_message_idempotent as _send_agent_idempotent,
        get_pending_question as _get_pending,
        respond_to_conversation as _respond_conv,
        receive_user_message as _receive_user,
        ack_user_message as _ack_user,
        bind_action_snapshot_reply as _bind_action_snapshot_reply,
        targeted_reply_context as _targeted_reply_context,
        close_conversation as _close_conversation,
        list_conversations as _list_conversations,
        conversation_agent_write_guard as _agent_write_guard,
    )
    from work_buddy.conversations.execution import (
        producer_for_lease as _producer_for_lease,
    )

    def _is_assistance_worker(agent_session_id):
        from work_buddy.dashboard.assistance.execution_identity import (
            assistance_generation_from_session,
        )

        return assistance_generation_from_session(agent_session_id) is not None

    def _request_failure(exc):
        from work_buddy.dashboard.assistance.contracts import AssistanceError

        return {
            "status": exc.code if isinstance(exc, AssistanceError) else "invalid_request",
            "error": str(exc),
        }

    def _reject_foreign_assistance(conversation_id, agent_session_id, conn=None):
        if _is_assistance_worker(agent_session_id):
            return
        conversation = _get_conversation(conversation_id, conn=conn)
        if conversation is not None and (
            conversation.source.startswith("assisted-draft:")
            or "assistedDraft" in (conversation.metadata or {})
        ):
            raise ConversationLeaseLost("lease_lost")

    @contextmanager
    def _write_fence(
        conversation_id,
        consumer,
        generation,
        agent_session_id,
    ):
        if _is_assistance_worker(agent_session_id):
            from work_buddy.dashboard.assistance.service import assert_worker_scope

            if consumer is None or generation is None:
                raise ValueError("Form assistance requires consumer and generation")
            with _agent_write_guard(conversation_id, consumer, generation) as conn:
                assert_worker_scope(
                    agent_session_id=agent_session_id,
                    conversation_id=conversation_id,
                    consumer=consumer,
                    generation=generation,
                    conn=conn,
                )
                yield conn
            return

        _reject_foreign_assistance(conversation_id, agent_session_id)
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
            yield None
            return
        if consumer is None or generation is None:
            raise ValueError("consumer and generation must be provided together")
        with _agent_write_guard(conversation_id, consumer, generation) as conn:
            yield conn

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
            (
                cowork_generation_from_session(agent_session_id) is not None
                or _is_assistance_worker(agent_session_id)
            )
            and producer is None
        ):
            raise ConversationLeaseLost("lease_lost")
        return producer

    def _assistance_output_producer(
        *, conversation_id, consumer, generation, agent_session_id,
        message_id, content, producer, conn,
    ):
        if not _is_assistance_worker(agent_session_id):
            return producer
        from work_buddy.dashboard.assistance.service import bind_worker_output

        return bind_worker_output(
            agent_session_id=agent_session_id,
            conversation_id=conversation_id,
            consumer=consumer,
            generation=generation,
            message_id=message_id,
            content=content,
            conn=conn,
        )

    def _cowork_output_authority(
        *,
        conversation_id,
        consumer,
        generation,
        agent_session_id,
        producer,
        lease_conn,
        message_id,
        content,
        reply_context,
    ):
        """Bind one Co-work assistant turn to its ordered worker inputs.

        Generic conversations intentionally bypass this seam. A server-issued
        Co-work execution session, however, may persist an assistant turn only
        with a caller-stable message identity and a live Sources-backed input
        manifest.
        """

        from work_buddy.cowork.conversations import (
            document_binding_for_conversation,
        )
        from work_buddy.cowork.execution_identity import (
            cowork_generation_from_session,
        )
        from work_buddy.cowork.worker_disclosure import (
            CoworkWorkerRun,
            get_cowork_worker_disclosure,
        )
        from work_buddy.truth.identity import sha256_text

        session_generation = cowork_generation_from_session(agent_session_id)
        if session_generation is None:
            return None
        if (
            not isinstance(message_id, str)
            or not message_id.strip()
        ):
            raise ValueError(
                "Co-work assistant output requires a caller-stable message_id"
            )
        if (
            generation != session_generation
            or not isinstance(consumer, str)
            or producer is None
        ):
            raise ConversationLeaseLost("lease_lost")
        binding = document_binding_for_conversation(
            conversation_id,
            conn=lease_conn,
        )
        if binding is None:
            raise ConversationLeaseLost("lease_lost")
        provider_id = str(producer.get("provider_id") or "").strip()
        model_id = str(producer.get("model_id") or "").strip()
        if not provider_id or not model_id:
            raise ConversationLeaseLost("lease_lost")
        run = CoworkWorkerRun(
            run_id=agent_session_id,
            worker_session_id=agent_session_id,
            provider_id=provider_id,
            model_id=model_id,
            authorization_ref=(
                f"cowork-document-agent:{conversation_id}:{generation}"
            ),
            purpose="cowork_document_agent",
        )
        content_sha256 = sha256_text(content)
        output_binding = get_cowork_worker_disclosure().bind_output(
            run,
            output_ref=(
                f"cowork-chat-message:{conversation_id}:{message_id}:"
                f"{content_sha256}"
            ),
            idempotency_key=f"cowork-chat-output:{message_id}",
        )

        frozen_markdown = None
        action_snapshot_id = (
            reply_context.get("action_snapshot_id")
            if isinstance(reply_context, dict)
            else None
        )
        if isinstance(action_snapshot_id, str) and action_snapshot_id:
            try:
                from work_buddy.cowork.chat_targets import action_snapshot_view
                from work_buddy.truth.registry import TruthStoreRegistry

                snapshot = action_snapshot_view(
                    TruthStoreRegistry().open_store(binding.store_id),
                    document_id=binding.document_id,
                    action_snapshot_id=action_snapshot_id,
                )
                candidate = snapshot.get("frozen_markdown")
                if isinstance(candidate, str):
                    frozen_markdown = candidate
            except Exception:
                # Classification becomes semantic/review-required below. An
                # unavailable quote witness must never be guessed as exact.
                frozen_markdown = None
        return binding, output_binding.manifest_sha256, frozen_markdown

    def _record_cowork_output_dependency(
        authority,
        *,
        conversation_id,
        message_id,
        content,
    ) -> None:
        if authority is None:
            return
        from work_buddy.cowork.conversation_source_dependencies import (
            record_conversation_source_dependency,
        )

        binding, manifest_sha256, frozen_markdown = authority
        record_conversation_source_dependency(
            store_id=binding.store_id,
            document_id=binding.document_id,
            conversation_id=conversation_id,
            message_id=message_id,
            role="agent",
            content=content,
            frozen_markdown=frozen_markdown,
            input_manifest_sha256=manifest_sha256,
        )

    def _verify_scoped_read_scope(
        conversation_id,
        consumer,
        generation,
        agent_session_id,
    ) -> None:
        """Bind hosted conversation reads to their exact active lease."""

        if _is_assistance_worker(agent_session_id):
            with _write_fence(
                conversation_id, consumer, generation, agent_session_id,
            ):
                pass
            return
        _reject_foreign_assistance(conversation_id, agent_session_id)

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

    def _account_scoped_read_payload(
        *,
        conversation_id,
        consumer,
        generation,
        agent_session_id,
        payload,
        tool_call_id,
    ) -> None:
        """Account exact conversation bytes before a scoped worker sees them."""

        if _is_assistance_worker(agent_session_id):
            from work_buddy.dashboard.assistance.service import account_worker_payload

            account_worker_payload(
                agent_session_id=agent_session_id,
                conversation_id=conversation_id,
                consumer=consumer,
                generation=generation,
                payload=payload,
                tool_call_id=tool_call_id,
            )
            return

        from work_buddy.cowork.conversations import (
            document_binding_for_conversation,
        )
        from work_buddy.cowork.execution_identity import (
            cowork_generation_from_session,
        )
        from work_buddy.cowork.worker_disclosure import (
            CoworkWorkerRun,
            get_cowork_worker_disclosure,
        )
        from work_buddy.sources.conversation import (
            ConversationMessageProvider,
            conversation_origin,
        )
        from work_buddy.sources.models import (
            canonical_json,
            canonical_sha256,
            sha256_bytes,
        )
        from work_buddy.sources.providers import (
            ProviderRegistry,
            source_capture_from_origin,
        )

        session_generation = cowork_generation_from_session(agent_session_id)
        if session_generation is None:
            return
        if (
            not isinstance(consumer, str)
            or not isinstance(generation, str)
            or generation != session_generation
            or not isinstance(agent_session_id, str)
        ):
            raise ConversationLeaseLost("lease_lost")
        with _agent_write_guard(
            conversation_id,
            consumer,
            generation,
        ) as lease_conn:
            producer = _trusted_producer(
                conversation_id,
                consumer,
                generation,
                lease_conn,
                agent_session_id,
            )
            binding = document_binding_for_conversation(
                conversation_id,
                conn=lease_conn,
            )
            if binding is None or producer is None:
                raise ConversationLeaseLost("lease_lost")
            provider_id = str(producer.get("provider_id") or "").strip()
            model_id = str(producer.get("model_id") or "").strip()
            if not provider_id or not model_id:
                raise ConversationLeaseLost("lease_lost")
            # Resolve exact native occurrences while the conversation binding
            # is fenced. Sources capture opens its own connection and must
            # remain outside this destination write transaction.
            native_messages: dict[str, str] = {}
            message = payload.get("message")
            if isinstance(message, dict):
                native = _get_message(
                    conversation_id, message.get("message_id"), conn=lease_conn,
                )
                if native is None or native.content != message.get("content"):
                    raise ValueError(
                        "The exact native conversation message is unavailable"
                    )
                native_messages[native.message_id] = native.content
            elif "question" in payload or "response" in payload:
                question_id = payload.get("message_id")
                question = _get_message(
                    conversation_id, question_id, conn=lease_conn,
                )
                if question is None or question.message_type != "question":
                    raise ValueError(
                        "The exact native conversation question is unavailable"
                    )
                native_messages[question.message_id] = question.content
                if "question" in payload and payload["question"] != question.content:
                    raise ValueError(
                        "The exact native conversation question is unavailable"
                    )
                if "response" in payload:
                    # Answers are separate immutable user messages. Never
                    # infer historical linkage from matching text or order.
                    answers = lease_conn.execute(
                        """SELECT message_id, content FROM messages
                           WHERE conversation_id = ? AND role = 'user'
                             AND CASE WHEN json_valid(context_json)
                                 THEN json_extract(context_json, '$.in_reply_to')
                                 END = ?""",
                        (conversation_id, question_id),
                    ).fetchall()
                    if (
                        question.status != "answered"
                        or question.response != payload["response"]
                        or len(answers) != 1
                        or answers[0]["content"] != payload["response"]
                    ):
                        raise ValueError(
                            "The exact native conversation answer is unavailable"
                        )
                    answer = answers[0]
                    native_messages[answer["message_id"]] = answer["content"]
        exact = canonical_json(dict(payload)).encode("utf-8")
        disclosure = get_cowork_worker_disclosure()
        derivation_refs: list[str] = []
        # Both received turns and question results quote native occurrences.
        # Retain their exact lineage so redaction reaches the released JSON.
        for message_id, content in native_messages.items():
            registry = ProviderRegistry()
            registry.register(
                ConversationMessageProvider(
                    principal=disclosure.sources.issuer,
                    authorization_fingerprint=canonical_sha256(
                        {
                            "purpose": "cowork_document_agent",
                            "conversation_id": conversation_id,
                            "message_id": message_id,
                        }
                    ),
                )
            )
            source_ref = source_capture_from_origin(
                disclosure.sources.store,
                registry,
                provider_id="work-buddy-conversation",
                origin_ref=conversation_origin(
                    conversation_id=conversation_id,
                    message_id=message_id,
                ),
                principal=disclosure.sources.issuer,
                purpose="cowork_document_agent",
                tenant_scope_id=disclosure.sources.tenant_scope_id,
                originating_surface="cowork_document_chat",
                namespace=conversation_id,
                expected_digest=sha256_bytes(content.encode("utf-8")),
            )
            derivation_refs.append(source_ref.uri)
        disclosure.account_payload(
            CoworkWorkerRun(
                run_id=agent_session_id,
                worker_session_id=agent_session_id,
                provider_id=provider_id,
                model_id=model_id,
                authorization_ref=(
                    f"cowork-document-agent:{conversation_id}:{generation}"
                ),
                purpose="cowork_document_agent",
            ),
            payload=payload,
            source_role="conversation_message",
            tool_call_id=tool_call_id,
            idempotency_key=(
                f"{tool_call_id}:{conversation_id}:{sha256_bytes(exact)}"
            ),
            derivation_refs=derivation_refs,
        )
        # Accounting is write-ahead, not permission to return to a worker that
        # was stopped or rebound while Sources work ran outside the lock.
        # Failure leaves the disclosure conservatively possibly_sent.
        with _agent_write_guard(
            conversation_id, consumer, generation,
        ) as lease_conn:
            current_producer = _trusted_producer(
                conversation_id, consumer, generation, lease_conn,
                agent_session_id,
            )
            current_binding = document_binding_for_conversation(
                conversation_id, conn=lease_conn,
            )
            if current_producer != producer or current_binding != binding:
                raise ConversationLeaseLost("lease_lost")

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
        consumption_receipt_id: str | None = None,
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
                producer = _assistance_output_producer(
                    conversation_id=conversation_id,
                    consumer=consumer,
                    generation=generation,
                    agent_session_id=agent_session_id,
                    message_id=message_id,
                    content=message,
                    producer=producer,
                    conn=lease_conn,
                )
                if (
                    consumption_receipt_id is not None
                    and message_id is None
                ):
                    raise ValueError(
                        "a targeted reply requires a caller-stable message_id"
                    )
                reply_context = (
                    None
                    if lease_conn is None
                    or consumer is None
                    or generation is None
                    else _targeted_reply_context(
                        conversation_id,
                        consumer,
                        generation,
                        message_id or "__unkeyed_reply__",
                        consumption_receipt_id,
                        conn=lease_conn,
                    )
                )
                output_authority = _cowork_output_authority(
                    conversation_id=conversation_id,
                    consumer=consumer,
                    generation=generation,
                    agent_session_id=agent_session_id,
                    producer=producer,
                    lease_conn=lease_conn,
                    message_id=message_id,
                    content=message,
                    reply_context=reply_context,
                )
                if message_id is None:
                    msg = _add_msg(
                        conversation_id,
                        "agent",
                        message,
                        conn=lease_conn,
                        producer=producer,
                        context=reply_context,
                    )
                    created = msg is not None
                else:
                    msg, created = _send_agent_idempotent(
                        conversation_id,
                        message,
                        message_id,
                        conn=lease_conn,
                        producer=producer,
                        context=reply_context,
                    )
                if (
                    msg is not None
                    and consumption_receipt_id is not None
                    and lease_conn is not None
                ):
                    persisted_receipt_id = None
                    if isinstance(reply_context, dict):
                        persisted_consumption = reply_context.get("consumption")
                        if isinstance(persisted_consumption, dict):
                            candidate_receipt_id = persisted_consumption.get(
                                "receipt_id"
                            )
                            if isinstance(candidate_receipt_id, str):
                                persisted_receipt_id = candidate_receipt_id
                    if (
                        persisted_receipt_id is not None
                        and persisted_receipt_id != consumption_receipt_id
                    ):
                        # Reconcile the predecessor receipt if its reply
                        # committed immediately before a generation restart.
                        _bind_action_snapshot_reply(
                            persisted_receipt_id,
                            msg.message_id,
                            conn=lease_conn,
                        )
                    _bind_action_snapshot_reply(
                        consumption_receipt_id,
                        msg.message_id,
                        conn=lease_conn,
                    )
                if msg is not None:
                    _record_cowork_output_dependency(
                        output_authority,
                        conversation_id=conversation_id,
                        message_id=msg.message_id,
                        content=msg.content,
                    )
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return _request_failure(exc)
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
        message_id: str | None = None,
    ) -> dict:
        if _is_assistance_worker(agent_session_id) and choices is not None and (
            not isinstance(choices, list)
            or any(not isinstance(choice, (str, dict)) for choice in choices)
        ):
            return {"status": "invalid_request", "error": "Invalid conversation choices"}
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
                existing_question = None
                if _is_assistance_worker(agent_session_id):
                    if response_type not in {"boolean", "choice"}:
                        raise ValueError("Use conversation_send for an open-ended question")
                    if response_type == "choice":
                        if not choice_dicts or not 2 <= len(choice_dicts) <= 16:
                            raise ValueError("A choice question needs two to sixteen choices")
                        keys = []
                        for choice in choice_dicts:
                            if (
                                set(choice) - {"key", "label", "description"}
                                or not isinstance(choice.get("key"), str)
                                or not choice["key"].strip()
                                or len(choice["key"]) > 128
                                or not isinstance(choice.get("label"), str)
                                or not choice["label"].strip()
                                or len(choice["label"]) > 500
                                or (
                                    "description" in choice
                                    and (
                                        not isinstance(choice["description"], str)
                                        or len(choice["description"]) > 1000
                                    )
                                )
                            ):
                                raise ValueError("Invalid conversation choice")
                            keys.append(choice["key"])
                        if len(set(keys)) != len(keys):
                            raise ValueError("Conversation choice keys must be unique")
                    elif choice_dicts:
                        raise ValueError("A boolean question does not accept choices")
                    if message_id is not None:
                        existing_question = _get_message(
                            conversation_id, message_id, conn=lease_conn,
                        )
                    if existing_question is not None:
                        if (
                            existing_question.role != "agent"
                            or existing_question.message_type != "question"
                            or existing_question.content != question
                            or existing_question.response_type != response_type
                            or existing_question.choices != choice_dicts
                        ):
                            raise ValueError("message_id was reused for a different question")
                    elif _get_pending(conversation_id, conn=lease_conn) is not None:
                        raise ValueError("A structured question is already pending")
                if existing_question is not None:
                    # A replay reads the original question; it is not a new
                    # publication against whichever user turn is current now.
                    msg = existing_question
                else:
                    producer = _trusted_producer(
                        conversation_id,
                        consumer,
                        generation,
                        lease_conn,
                        agent_session_id,
                    )
                    producer = _assistance_output_producer(
                        conversation_id=conversation_id,
                        consumer=consumer,
                        generation=generation,
                        agent_session_id=agent_session_id,
                        message_id=message_id,
                        content=question,
                        producer=producer,
                        conn=lease_conn,
                    )
                    output_authority = _cowork_output_authority(
                        conversation_id=conversation_id,
                        consumer=consumer,
                        generation=generation,
                        agent_session_id=agent_session_id,
                        producer=producer,
                        lease_conn=lease_conn,
                        message_id=message_id,
                        content=question,
                        reply_context=None,
                    )
                    msg = _add_msg(
                        conversation_id,
                        "agent",
                        question,
                        message_type="question",
                        response_type=response_type,
                        choices=choice_dicts,
                        conn=lease_conn,
                        message_id=message_id,
                        producer=producer,
                    )
                    if msg is not None:
                        _record_cowork_output_dependency(
                            output_authority,
                            conversation_id=conversation_id,
                            message_id=msg.message_id,
                            content=msg.content,
                        )
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return _request_failure(exc)
        if msg is None:
            return {
                "error": f"Conversation not found or closed: {conversation_id}",
            }
        if msg.status == "answered" or timeout_seconds is not None:
            result = _poll_question(
                conversation_id=conversation_id,
                message_id=msg.message_id,
                timeout_seconds=timeout_seconds,
                consumer=consumer,
                generation=generation,
                agent_session_id=agent_session_id,
                tool_call_id="conversation_ask",
            )
            return {"conversation_id": conversation_id, **result}
        return {
            "message_id": msg.message_id,
            "conversation_id": conversation_id,
            "status": "pending",
        }

    def _poll_question(
        *, conversation_id, message_id, timeout_seconds, consumer,
        generation, agent_session_id, tool_call_id,
    ):
        def _scoped_question(exact_id):
            with _write_fence(
                conversation_id, consumer, generation, agent_session_id,
            ) as lease_conn:
                if exact_id is not None:
                    question = _get_message(
                        conversation_id, exact_id, conn=lease_conn,
                    )
                    if question is None or question.message_type != "question":
                        raise ValueError("The exact conversation question is unavailable")
                    return question
                pending = _get_pending(conversation_id, conn=lease_conn)
                if pending is not None:
                    return pending
                data = _get_conv_msgs(conversation_id, conn=lease_conn)
                if data is None:
                    raise ValueError("The conversation is unavailable")
                answered = [
                    item for item in data["messages"]
                    if item.get("message_type") == "question"
                    and item.get("status") == "answered"
                ]
                return (
                    _get_message(
                        conversation_id, answered[-1]["message_id"], conn=lease_conn,
                    )
                    if answered else None
                )

        def _account_result(result):
            if "question" in result or "response" in result:
                _account_scoped_read_payload(
                    conversation_id=conversation_id,
                    consumer=consumer,
                    generation=generation,
                    agent_session_id=agent_session_id,
                    payload=result,
                    tool_call_id=tool_call_id,
                )
            return result

        try:
            question = _scoped_question(message_id)
            if question is None:
                return {"status": "no_pending_question"}
            exact_id = question.message_id
            if question.status == "answered":
                return _account_result({
                    "status": "answered",
                    "message_id": exact_id,
                    "response": question.response,
                })
            if timeout_seconds is None:
                return _account_result({
                    "status": "pending",
                    "message_id": exact_id,
                    "question": question.content,
                })
            if type(timeout_seconds) is not int or timeout_seconds < 0:
                raise ValueError("timeout_seconds must be a nonnegative integer")
            wait_seconds = min(timeout_seconds, 110)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                # Once chosen, only this question can complete the wait. A
                # newer question neither blocks nor answers an older one.
                question = _scoped_question(exact_id)
                if question.status == "answered":
                    return _account_result({
                        "status": "answered",
                        "message_id": exact_id,
                        "response": question.response,
                    })
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(3, remaining))
            return {
                "status": "timeout",
                "message_id": exact_id,
                "waited_seconds": wait_seconds,
            }
        except ConversationLeaseLost:
            return {"status": "lease_lost", "conversation_id": conversation_id}
        except ValueError as exc:
            return _request_failure(exc)

    def conversation_poll(
        conversation_id: str,
        timeout_seconds: int | None = None,
        consumer: str | None = None,
        generation: str | None = None,
        agent_session_id: str | None = None,
        message_id: str | None = None,
    ) -> dict:
        """Inspect or await one exact question, defaulting to the current one."""
        return _poll_question(
            conversation_id=conversation_id,
            message_id=message_id,
            timeout_seconds=timeout_seconds,
            consumer=consumer,
            generation=generation,
            agent_session_id=agent_session_id,
            tool_call_id="conversation_poll",
        )

    def conversation_receive(
        conversation_id: str,
        consumer: str,
        generation: str,
        timeout_seconds: int | None = None,
        agent_session_id: str | None = None,
    ) -> dict:
        """Receive the oldest unacked user turn for one leased consumer."""
        try:
            _verify_scoped_read_scope(
                conversation_id,
                consumer,
                generation,
                agent_session_id,
            )
            result = _receive_user(
                conversation_id,
                consumer,
                generation,
                timeout_seconds=0 if timeout_seconds is None else timeout_seconds,
            )
            if result.get("status") == "message":
                _account_scoped_read_payload(
                    conversation_id=conversation_id,
                    consumer=consumer,
                    generation=generation,
                    agent_session_id=agent_session_id,
                    payload=result,
                    tool_call_id="conversation_receive",
                )
            return result
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return _request_failure(exc)

    def conversation_ack(
        conversation_id: str,
        consumer: str,
        generation: str,
        message_id: str,
        action_snapshot_id: str | None = None,
        consumption_receipt_id: str | None = None,
        agent_session_id: str | None = None,
    ) -> dict:
        """Acknowledge the exact turn and any attached action snapshot."""
        try:
            with _write_fence(
                conversation_id,
                consumer,
                generation,
                agent_session_id,
            ) as lease_conn:
                if _is_assistance_worker(agent_session_id):
                    from work_buddy.dashboard.assistance.service import (
                        assert_worker_turn_consumed,
                    )

                    assert_worker_turn_consumed(
                        agent_session_id=agent_session_id,
                        conversation_id=conversation_id,
                        consumer=consumer,
                        generation=generation,
                        message_id=message_id,
                        conn=lease_conn,
                    )
                return _ack_user(
                    conversation_id,
                    consumer,
                    generation,
                    message_id,
                    action_snapshot_id=action_snapshot_id,
                    consumption_receipt_id=consumption_receipt_id,
                    conn=lease_conn,
                )
        except ConversationLeaseLost:
            return {
                "status": "lease_lost",
                "conversation_id": conversation_id,
            }
        except ValueError as exc:
            return _request_failure(exc)

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
