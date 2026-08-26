---
name: Dashboard Chat Sidebar
kind: concept
description: "Retained root-dashboard right-rail conversation surface; React authoring uses shared chat primitives and widget-native assisted drafts."
tags:
- dashboard
- chat
- sidebar
- conversations
- reusable
aliases:
- wbChatSidebar
- chat sidebar
- sidebar chat
- help me create
parents:
- services/dashboard
- services/dashboard
---

Retained right-rail conversation surface for the Python-generated root dashboard. Its `window.wbChatSidebar` API and `conversation_chat` renderer are compatibility infrastructure, not the extension path for new React forms.

React authoring reuses `services/dashboard/react/chat-primitives` and `services/dashboard/react/assisted-drafts`: the host owns the draft, the assistant proposes typed field changes, and only the user can submit. Jobs authoring uses `/app/jobs`. The old `/api/user_jobs/help` endpoint returns a migration response without creating a conversation or spawning an agent, and `dashboard_interact` cannot drive the Jobs form.

## API

```js
window.wbChatSidebar.open({
    conversation_id,   // required
    title,             // header text
    bound_tab,         // optional — only show while this tab is active
    on_close,          // optional callback
});
window.wbChatSidebar.close();         // detach + slide closed + POST /close
window.wbChatSidebar.isOpen();        // mounted, regardless of visibility
window.wbChatSidebar.isVisible();     // mounted AND currently shown
window.wbChatSidebar.currentConversationId();
```

## Two-axis state

- ``html.wb-chat-mounted`` — there is a live chat instance attached. The 3-second poll loop in ``attachConversationChat`` is running. Stays through tab switches when ``bound_tab`` is set.
- ``html.wb-chat-visible`` — the sidebar should currently be shown with the squish active. Removed when ``bound_tab`` is set and the active tab does not match.

Tab-binding mechanics: on every nav-bar click, the sidebar re-evaluates visibility against the active tab. The chat instance is *not* unmounted while hidden — message history accumulates in the SQLite store and the next time the user returns to the bound tab, the latest messages are already present.

## Squish behavior

The sidebar uses ``position: fixed; right: 0`` and floats above the viewport's right edge. The squish is implemented as ``html { padding-right: var(--wb-chat-sidebar-width) }`` rather than ``.tab-panel { margin-right: ... }`` so it does not collide with the existing ``.tab-panel { margin: 0 auto }`` centering rule. The variable + class lives on ``<html>`` (not ``<body>``) because body padding is overridden by another layout rule in this codebase even with ``!important`` — html padding squishes reliably.

## Lifecycle and conversation handling

The sidebar's static markup lives in ``html.py`` next to ``review-drawer`` so CSS targets it from page load (no flash on first open). ``open()`` populates the title, calls ``attachConversationChat(body, cid, {mode:'pane'})``, adds ``wb-chat-mounted``, and evaluates initial visibility. ``close()`` calls ``detachConversationChat(cid)``, posts to ``/api/conversations/<id>/close`` so the agent's next ``conversation_ask`` returns 'closed' and exits cleanly, then removes both classes.

## Agent liveness — typing indicator and the 'stopped' state

The chat surface relies on a real OS-level process check, not a time-based guess. Each chat-spawning endpoint registers the driving subprocess's PID via ``work_buddy.conversations.agents.register(conversation_id, pid)``. ``GET /api/conversations/<id>`` then includes ``conversation.agent_alive`` (``true`` / ``false`` / ``null``):

* ``true`` — process is up. Renderer shows the three-dot typing indicator while the agent is mid-flow (last message is from the user, OR last message is agent text-not-question).
* ``false`` — process exited (budget cap, crash, kill). Renderer drops the typing indicator, shows a red-bordered "Agent stopped responding" notice in the messages pane, and disables the input + Send button. The user's only path forward is closing the sidebar.
* ``null`` — no driving process was registered (e.g. user-driven chat with no spawned agent). Renderer falls back to a minimal heuristic: show the indicator after the user's last message, hide after any agent message.

``unregister`` is called on ``/api/conversations/<id>/close`` and on conversation_close failure so the registry doesn't leak.

## Retained spawn helper

The Jobs-specific helper in `jobs_help.py` remains as compatibility code but is not invoked by `/api/user_jobs/help`. Its process and budget settings are not the policy for React authoring, which uses the shared assistance runtime and explicit model opt-in.

## Retained legacy mounting protocol

1. Dashboard endpoint POSTs through ``_reject_read_only()``, calls ``conversations.store.create_conversation(...)`` directly (NOT the ``conversation_create`` capability — that fires a CHAT toast and a workflow-view tab via ``_notify_conversation_created``, double-mounting the conversation). The seed message is added with ``message_type='question'`` and ``response_type='freeform'`` so the spawned agent's ``conversation_poll`` returns the user's first reply directly — without this, ``conversation_poll`` returns ``no_pending_question`` and the agent sends a duplicate greeting.
2. Endpoint fire-and-forgets a Claude session via ``sidecar.dispatch.executor.spawn_headless_agent_detached`` with a brief that primes the agent to drive the conversation_id to its goal. The brief is composed of (a) a short static-prose preamble describing the consumer's role and (b) a generated structural section from ``interact_brief.render_form_section(schema)`` describing the form the agent will drive (see ``services/dashboard/form-bridge``).
3. **Register the spawned PID** via ``work_buddy.conversations.agents.register(conversation_id, pid)`` so the sidebar's typing-indicator and 'stopped' state work correctly.
4. Endpoint returns ``{ok, conversation_id, title}``; on failure, closes the conversation so it does not dangle.
5. Frontend opens the sidebar with the returned conversation_id and an optional ``bound_tab`` matching the calling tab.

## Live updates while chatting

The root dashboard's conversation surface and domain views share the existing event bus. A domain event can refresh the relevant view without replacing the conversation. This does not confer submission authority on a React draft assistant.

## Jobs authoring boundary

Legacy Jobs launch links lead to `/app/jobs`. The visible-field assistance mechanism there uses the shared React widget and chat contracts, not `wbChatSidebar`, `jobs_help.py`, or `dashboard_interact`. Existing job management and editing remain in the root Jobs tab.
