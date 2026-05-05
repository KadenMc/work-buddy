"""Conversation chat component JS.

Renamed from ``script_threads.py`` — the ``Thread`` name is reserved
for the universal-entity primitive (rendered by ``script_threads.py``
in its current form). The chat UI here is for agent-user dialogue
and is rendered as a Conversation.
"""

from __future__ import annotations


def _conversation_chat_script() -> str:
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
                  + '<button onclick="conversationRespond(&#39;' + cid + '&#39;,&#39;true&#39;)">Yes</button>'
                  + '<button onclick="conversationRespond(&#39;' + cid + '&#39;,&#39;false&#39;)">No</button></div>';
            } else if (msg.response_type === 'choice' && msg.choices) {
                bubble += '<div class="msg-choices">';
                for (const c of msg.choices) {
                    bubble += '<button onclick="conversationRespond(&#39;' + cid + '&#39;,&#39;' + _esc(c.key) + '&#39;)">'
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

    function _render(inst, data) {
        const conv = data.conversation;
        const msgs = data.messages || [];
        const cid = conv.conversation_id;

        let html = '';
        for (const m of msgs) html += _msgHtml(m, cid);

        const pending = msgs.filter(m => m.status === 'pending');
        const hasPending = pending.length > 0;
        const isOpen = conv.status === 'open';

        let status = '';
        if (conv.status === 'closed') status = 'Conversation closed';
        else if (hasPending) status = 'Waiting for your response...';

        // Use the appropriate wrapper class. Keep the same CSS class names
        // (thread-chat-*) so existing styles continue to work without a
        // CSS rewrite. Classes are CSS-only labels at this point.
        const wrapClass = inst.mode === 'pane' ? 'thread-chat-pane' : 'thread-chat-standalone';
        inst.container.innerHTML =
            '<div class="' + wrapClass + '">'
            + '<div class="thread-chat-messages" id="tc-msgs-' + cid + '">' + html + '</div>'
            + (isOpen
                ? '<div class="thread-input" id="tc-input-' + cid + '">'
                  + '<input type="text" placeholder="Type a message..." '
                  + 'onkeydown="if(event.key===&#39;Enter&#39;&&!event.shiftKey){event.preventDefault();conversationSendInput(&#39;' + cid + '&#39;)}" />'
                  + '<button onclick="conversationSendInput(&#39;' + cid + '&#39;)">Send</button></div>'
                : '')
            + (status ? '<div class="thread-status-bar">' + _esc(status) + '</div>' : '')
            + '</div>';

        const el = document.getElementById('tc-msgs-' + cid);
        if (el) el.scrollTop = el.scrollHeight;
        inst.lastCount = msgs.length;
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
        if (data && (data.messages || []).length !== (inst.lastCount || 0)) {
            _render(inst, data);
        }
    }

    // ---- Public API ----

    /**
     * Mount a ConversationChat into a container element.
     * @param {HTMLElement} container - DOM element to render into
     * @param {string} conversationId - conversation to display
     * @param {object} opts - { mode: 'standalone'|'pane' } (default: standalone)
     */
    window.attachConversationChat = function(container, conversationId, opts) {
        opts = opts || {};
        const inst = {
            container: container,
            mode: opts.mode || 'standalone',
            lastCount: 0,
            pollInterval: null,
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
        const input = el.querySelector('input');
        if (!input || !input.value.trim()) return;
        conversationRespond(cid, input.value.trim());
        input.value = '';
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
