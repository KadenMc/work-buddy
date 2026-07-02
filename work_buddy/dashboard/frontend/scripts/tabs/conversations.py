"""Conversation chat component JS.

Renamed from ``tabs/threads/main.py`` — the ``Thread`` name is reserved
for the universal-entity primitive (rendered by ``tabs/threads/main.py``
in its current form). The chat UI here is for agent-user dialogue
and is rendered as a Conversation.
"""

from __future__ import annotations


def script() -> str:
    return """
// ---- ConversationChat: reusable chat component ----
// Mount into any container. Used by the conversation_chat view renderer
// (standalone tab) and available for any view via attachConversationChat().
(function() {
    const _instances = {};  // conversationId → { container, pollInterval, lastCount }

    function _esc(s) {
        if (!s) return '';
        return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function _time(iso) {
        if (!iso) return '';
        try { return new Date(iso).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
        catch(e) { return ''; }
    }

    function _msgHtml(msg, cid) {
        const rawRole = msg.role || 'agent';
        // Map roles to shared chat-msg classes: agent→assistant
        const cssRole = rawRole === 'agent' ? 'assistant' : rawRole;
        const t = _time(msg.created_at);

        let bubble = _esc(msg.content);

        // Choice buttons inside the bubble (only for pending questions)
        if (msg.status === 'pending' && msg.message_type === 'question') {
            if (msg.response_type === 'boolean') {
                bubble += '<div class="msg-choices">'
                  + '<button ' + wbActAttrs('conversationRespondYes', {conversationId: cid}) + '>Yes</button>'
                  + '<button ' + wbActAttrs('conversationRespondNo', {conversationId: cid}) + '>No</button></div>';
            } else if (msg.response_type === 'choice' && msg.choices) {
                bubble += '<div class="msg-choices">';
                for (const c of msg.choices) {
                    bubble += '<button ' + wbActAttrs('conversationRespondChoice', {conversationId: cid, choiceKey: c.key}) + '>'
                       + _esc(c.label) + '</button>';
                }
                bubble += '</div>';
            }
        }

        let h = '<div class="chat-msg ' + cssRole + '">'
              + '<div class="chat-msg-bubble">' + bubble + '</div>';
        if (t) h += '<div class="chat-msg-meta">' + t + '</div>';
        return h + '</div>';
    }

    // Typing-indicator HTML — three pulsing dots in an assistant-styled
    // bubble. Shown when the conversation is open, no question is
    // pending, and the most-recent message is from the user (i.e. the
    // agent is processing). Without this the user sees their own
    // message land and then nothing for several seconds.
    function _typingIndicatorHtml() {
        return '<div class="chat-msg assistant chat-msg-typing">'
             +   '<div class="chat-msg-bubble">'
             +     '<span class="typing-dot"></span>'
             +     '<span class="typing-dot"></span>'
             +     '<span class="typing-dot"></span>'
             +   '</div>'
             + '</div>';
    }

    // ``conversation.agent_alive`` is the authoritative liveness
    // signal: ``true`` if the driving agent process is alive,
    // ``false`` if it exited (budget cap, crash, kill), or
    // ``null`` if no agent was ever registered for this conversation
    // (e.g. user-driven chats with no spawned driver, in which case
    // we can't tell — fall back to "show indicator after user msg").
    function _computeAgentTyping(conv, msgs) {
        const isOpen = conv && conv.status === 'open';
        if (!isOpen) return false;
        const pending = msgs.filter(m => m.status === 'pending');
        if (pending.length > 0) return false;  // status bar handles this
        const last = msgs.length ? msgs[msgs.length - 1] : null;
        if (!last) return false;
        const aliveSignal = conv.agent_alive;
        if (aliveSignal === false) return false;  // process is dead
        // Agent's last message is a question → they're explicitly
        // waiting for the user, no need for the indicator.
        if (last.role !== 'user' && last.message_type === 'question') {
            return false;
        }
        // For agent-text or user-reply messages: alive=true → show
        // dots; alive=null (no driver registered) → fall back to
        // "show after user reply only" so non-agent-driven chats
        // don't display a misleading dancing indicator.
        if (aliveSignal === true) return true;
        return last.role === 'user';
    }

    function _isAgentDead(conv) {
        return conv && conv.status === 'open' && conv.agent_alive === false;
    }

    function _render(inst, data) {
        const conv = data.conversation;
        const msgs = data.messages || [];
        const cid = conv.conversation_id;

        // Detect status flip to 'closed' on this render cycle so
        // mount-side consumers (e.g. the chat sidebar) can react —
        // the typical reaction is to slide the surface away after a
        // brief grace so the user reads the final message.
        if (conv.status === 'closed' && inst.lastStatus !== 'closed') {
            inst.lastStatus = 'closed';
            if (typeof inst.onClosed === 'function' && !inst.onClosedFired) {
                inst.onClosedFired = true;
                try { inst.onClosed(); } catch (e) { console.warn(e); }
            }
        } else {
            inst.lastStatus = conv.status;
        }

        let html = '';
        for (const m of msgs) html += _msgHtml(m, cid);

        const pending = msgs.filter(m => m.status === 'pending');
        const hasPending = pending.length > 0;
        const isOpen = conv.status === 'open';
        // Typing indicator fires whenever the agent appears to still be
        // working: either it hasn't responded to the user's last reply
        // yet (last role=user), OR its most recent message is a status
        // line (text, not a question) — agents legitimately send several
        // messages back-to-back ("Looking that up...", "Found it...",
        // then a question), and without the indicator the user can't
        // tell whether more is coming or the agent is done.
        //
        // BUT: a headless ``claude --print`` process can also exit
        // silently (budget cap, crash) without calling conversation_close.
        // Without a time bound, the indicator would dance forever in
        // that case. Apply a grace window past the last message; if it
        // elapses, assume the agent stopped and hide the indicator.
        // The polling re-render (see _poll) catches the boundary.
        const agentTyping = _computeAgentTyping(conv, msgs);
        const agentDead = _isAgentDead(conv);
        if (agentTyping) html += _typingIndicatorHtml();
        if (agentDead) {
            html += '<div class="chat-agent-stopped">'
                  + 'Agent stopped responding. Close this chat and start a '
                  + 'new one to continue.'
                  + '</div>';
        }

        let status = '';
        if (conv.status === 'closed') status = 'Conversation closed';
        else if (agentDead) status = 'Agent stopped — please close this chat';
        else if (hasPending) status = 'Waiting for your response...';
        else if (agentTyping) status = 'Agent is thinking...';

        // Use the appropriate wrapper class. Keep the same CSS class names
        // (thread-chat-*) so existing styles continue to work without a
        // CSS rewrite. Classes are CSS-only labels at this point.
        const wrapClass = inst.mode === 'pane' ? 'thread-chat-pane' : 'thread-chat-standalone';
        // Input element: textarea (not <input>) so Shift+Enter inserts a
        // real newline. Plain Enter submits via the keydown handler.
        // Cap matches .thread-input textarea max-height so JS-driven
        // growth and CSS max-height agree; beyond the cap the
        // textarea's overflow-y kicks in (the user can scroll within).
        // When the driving agent is dead, the input is rendered
        // disabled — typing into it would have no audience and
        // ``conversationRespond`` would silently land messages no one
        // reads. The user's only recovery is closing the sidebar.
        const inputDisabled = agentDead ? ' disabled' : '';
        const inputPlaceholder = agentDead
            ? 'Agent stopped — close this chat to continue'
            : 'Type a message...';
        inst.container.innerHTML =
            '<div class="' + wrapClass + '">'
            + '<div class="thread-chat-messages" id="tc-msgs-' + cid + '">' + html + '</div>'
            + (isOpen
                ? '<div class="thread-input" id="tc-input-' + cid + '">'
                  + '<textarea rows="1" placeholder="' + inputPlaceholder + '"'
                  + inputDisabled
                  + ' ' + wbActAttrs('conversationInputAuto', {conversationId: cid}, 'input') + '"></textarea>'
                  + '<button' + inputDisabled
                  + ' ' + wbActAttrs('conversationSendClick', {conversationId: cid}) + '>Send</button></div>'
                : '')
            + (status ? '<div class="thread-status-bar">' + _esc(status) + '</div>' : '')
            + '</div>';

        const el = document.getElementById('tc-msgs-' + cid);
        if (el) el.scrollTop = el.scrollHeight;
        inst.lastCount = msgs.length;
        inst.lastTyping = agentTyping;
        inst.lastAlive = conv.agent_alive;
    }

    async function _fetch(cid) {
        const r = await fetch('/api/conversations/' + cid);
        if (!r.ok) return null;
        return await r.json();
    }

    async function _poll(cid) {
        const inst = _instances[cid];
        if (!inst) return;
        const data = await _fetch(cid);
        if (!data) return;
        const newCount = (data.messages || []).length;
        const newStatus = data.conversation && data.conversation.status;
        // Re-render on a new message, a status flip, a typing-indicator
        // transition, OR an agent_alive transition. The last catches
        // the moment a driving ``claude --print`` exits — flipping the
        // input from "Agent is thinking..." to disabled with a clear
        // "Agent stopped responding" notice.
        const newTyping = _computeAgentTyping(
            data.conversation, data.messages || [],
        );
        const newAlive = data.conversation && data.conversation.agent_alive;
        if (
            newCount !== (inst.lastCount || 0)
            || newStatus !== inst.lastStatus
            || newTyping !== inst.lastTyping
            || newAlive !== inst.lastAlive
        ) {
            _render(inst, data);
        }
    }

    // ---- Event delegation adapters ----
    window.wbAction('conversationRespondYes', function(el) {
        const cid = el.dataset.conversationId;
        conversationRespond(cid, 'true');
    });
    window.wbAction('conversationRespondNo', function(el) {
        const cid = el.dataset.conversationId;
        conversationRespond(cid, 'false');
    });
    window.wbAction('conversationRespondChoice', function(el) {
        const cid = el.dataset.conversationId;
        const key = el.dataset.choiceKey;
        conversationRespond(cid, key);
    });
    window.wbAction('conversationInputAuto', function(el) {
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 96) + 'px';
    });
    window.wbAction('conversationSendClick', function(el) {
        const cid = el.dataset.conversationId;
        conversationSendInput(cid);
    });

    // ---- Public API ----

    /**
     * Mount a ConversationChat into a container element.
     * @param {HTMLElement} container - DOM element to render into
     * @param {string} conversationId - conversation to display
     * @param {object} opts - { mode: 'standalone'|'pane' (default: standalone),
     *                          onClosed: () => void  // fired once when the
     *                          conversation's status flips to 'closed' }
     */
    window.attachConversationChat = function(container, conversationId, opts) {
        opts = opts || {};
        const inst = {
            container: container,
            mode: opts.mode || 'standalone',
            lastCount: 0,
            pollInterval: null,
            onClosed: typeof opts.onClosed === 'function' ? opts.onClosed : null,
            onClosedFired: false,
            lastStatus: null,
        };
        _instances[conversationId] = inst;

        _fetch(conversationId).then(data => {
            if (!data) { container.innerHTML = '<div class="empty-state">Conversation not found</div>'; return; }
            _render(inst, data);
            inst.pollInterval = setInterval(() => _poll(conversationId), 3000);
        }).catch(() => {
            container.innerHTML = '<div class="empty-state">Failed to load conversation</div>';
        });
    };

    /** Unmount and stop polling for a conversation. */
    window.detachConversationChat = function(conversationId) {
        const inst = _instances[conversationId];
        if (!inst) return;
        if (inst.pollInterval) clearInterval(inst.pollInterval);
        delete _instances[conversationId];
    };

    window.conversationRespond = async function(cid, value) {
        if (_readOnly) return;
        try {
            const r = await fetch('/api/conversations/' + cid + '/respond', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({value: value}),
            });
            if (r.ok) { const d = await _fetch(cid); if (d) _render(_instances[cid], d); }
        } catch(e) { console.error('Conversation respond failed:', e); }
    };

    window.conversationSendInput = function(cid) {
        const el = document.getElementById('tc-input-' + cid);
        if (!el) return;
        // The renderer ships a <textarea> for Shift+Enter newline support.
        // Fall back to <input> for any consumer that's still on the
        // legacy single-line shape.
        const input = el.querySelector('textarea') || el.querySelector('input');
        if (!input || !input.value.trim()) return;
        conversationRespond(cid, input.value.trim());
        input.value = '';
        // Reset auto-grown textarea height so it snaps back to one
        // row after a multi-line send. Plain <input> ignores this.
        if (input.tagName === 'TEXTAREA') input.style.height = '';
    };

    // ---- Workflow view renderer: split layout (context + chat) ----
    if (typeof registerViewRenderer === 'function') {
        registerViewRenderer('conversation_chat', function(container, viewId, payload) {
            const cid = payload && payload.conversation_id;
            if (!cid) { container.innerHTML = '<div class="empty-state">Missing conversation_id</div>'; return; }

            container.innerHTML = '';
            const layout = document.createElement('div');
            layout.className = 'thread-split-layout';

            const contentPane = document.createElement('div');
            contentPane.className = 'thread-split-content';
            contentPane.innerHTML = '<div style="color:var(--text-muted);font-size:13px">'
                + '<p style="margin-bottom:8px;font-weight:600;color:var(--text)">'
                + _esc(payload.title || 'Conversation') + '</p>'
                + '<p>This panel will show related context — task details, '
                + 'notification content, or workflow state.</p></div>';
            layout.appendChild(contentPane);

            const chatPane = document.createElement('div');
            layout.appendChild(chatPane);

            container.appendChild(layout);
            attachConversationChat(chatPane, cid, { mode: 'pane' });
        });
    }

    // Cleanup on tab removal
    const _origRemove = window.removeWorkflowTab;
    if (typeof _origRemove === 'function') {
        window.removeWorkflowTab = function(viewId) {
            if (viewId && viewId.startsWith('conversation-')) {
                detachConversationChat(viewId.substring('conversation-'.length));
            }
            return _origRemove(viewId);
        };
    }
})();
"""
