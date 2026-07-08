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
- conversation_poll checks the latest question without sending a new message
- Sidebar auto-polls every 3s for new messages

## Naming note

In v5 the term **Thread** is reserved for work-buddy's universal entity for 'context that may need an action'. The agent-user dialogue subsystem you're using here is called a **Conversation** to free the name.
