---
name: Conversation Management Directions
kind: directions
description: When and how to use agent-user conversations — decision guide, response types, behavioral notes. The `threads` namespace is reserved for the universal-entity primitive.
summary: 'Use conversations for: explaining plans before executing, multi-step decisions, progress updates, follow-up dialogue. conversation_create opens the dashboard sidebar automatically. conversation_ask blocks up to 110s.'
trigger: user wants to start a conversation, or agent needs to explain a plan, ask multi-step questions, or send progress updates
command: wb-conversation
capabilities:
- conversations/conversation_create
- conversations/conversation_send
- conversations/conversation_ask
- conversations/conversation_poll
- conversations/conversation_receive
- conversations/conversation_ack
- conversations/conversation_close
- conversations/conversation_list
tags:
- conversations
- conversation
- dashboard
- chat
- directions
aliases:
- conversation usage
- manage conversations
- start conversation
- chat thread
- dashboard sidebar
parents:
- conversations
---

Start or manage an agent-user conversation via the dashboard sidebar.

Argument: $ARGUMENTS (optional -- conversation_id to resume, or blank to create new)

## Conversation operations

mcp__work-buddy__wb_run("conversation_create", {"title": "...", "message": "..."})
mcp__work-buddy__wb_run("conversation_send", {"conversation_id": "...", "message": "..."})
mcp__work-buddy__wb_run("conversation_ask", {"conversation_id": "...", "question": "...", "response_type": "boolean"})
mcp__work-buddy__wb_run("conversation_poll", {"conversation_id": "..."})
mcp__work-buddy__wb_run("conversation_receive", {"conversation_id": "...", "consumer": "...", "generation": "...", "timeout_seconds": 110})
mcp__work-buddy__wb_run("conversation_ack", {"conversation_id": "...", "consumer": "...", "generation": "...", "message_id": "..."})
mcp__work-buddy__wb_run("conversation_close", {"conversation_id": "..."})
mcp__work-buddy__wb_run("conversation_list")

## When to use conversations

- Explaining plans before executing: create a conversation, describe the plan, ask approval
- Multi-step decisions: ask a sequence of questions in one back-and-forth
- Progress updates: send status messages during long tasks
- Follow-up dialogue: when a notification needs back-and-forth

## Response types for conversation_ask

| Type | User sees | Response value |
|------|-----------|---------------|
| freeform (default) | Text input | User's text |
| boolean | Yes/No buttons | "true" or "false" |
| choice | Labeled buttons | The choice key |

## Behavior

- conversation_create opens the chat sidebar on the dashboard automatically
- conversation_ask with timeout_seconds blocks until response (max 110s)
- conversation_poll accepts an exact `message_id`; omission discovers the
  current question. Once a wait starts, only that question can complete it,
  even when a newer question appears. Content-bearing pending/answer results
  are disclosure-accounted for scoped hosted workers before return
- conversation_receive is for a leased long-running driver: it returns the
  oldest unacknowledged user turn without advancing the durable cursor
- conversation_ack advances that cursor only over the exact delivered message;
  call it after the turn's reply and side effects succeed
- a leased driver must pass the same `consumer` and `generation` to
  send, ask, poll, receive and acknowledge tools; this fences late access after a
  restart or close
- when the lease carries a validated execution snapshot, assistant messages
  receive their provider/model producer provenance from that exact lease; a
  caller cannot assert or override it
- a `lease_lost` result from receive, acknowledge, send, or ask means a newer
  driver owns the conversation; stop immediately
- canonical question answers carry an exact `in_reply_to`; scoped disclosure
  stages the question and that canonical user answer, then checks the current
  lease and binding again after accounting. Historical unlinked answers fail
  closed rather than being guessed into an association
- Sidebar auto-polls every 3s for new messages

## Hosted form conversations

Form agents receive a pre-bound conversation and may not create, list or close
arbitrary conversations. Consume the bound initial context first, then receive
each authored turn and consume its exact form snapshot. Only then propose
allowlisted edits and send a durable reply/question before acknowledgement.
Use plain messages for open-ended questions; finite choices use stable question
IDs, and ordinary composer text does not consume them. Stop on scope, policy,
expiry or disclosure failure. See `services/dashboard/react/assisted-drafts`.

## Naming note

In v5 the term **Thread** is reserved for work-buddy's universal entity for 'context that may need an action'. The agent-user dialogue subsystem you're using here is called a **Conversation** to free the name.
