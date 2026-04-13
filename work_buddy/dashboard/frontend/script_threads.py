"""Thread chat component JS."""

from __future__ import annotations


def _thread_chat_script() -> str:
    return """
// ---- ThreadChat: reusable chat component ----
// Mount into any container. Used by the thread_chat view renderer
// (standalone tab) and available for any view via attachThreadChat().
(function() {
    const _instances = {};  // threadId → { container, pollInterval, lastCount }

    function _esc(s) {
        if (!s) return '';
        return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function _time(iso) {
        if (!iso) return '';
        try { return new Date(iso).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
        catch(e) { return ''; }
    }

    function _msgHtml(msg, tid) {
        const rawRole = msg.role || 'agent';
        // Map thread roles to shared chat-msg classes: agent→assistant
        const cssRole = rawRole === 'agent' ? 'assistant' : rawRole;
        const t = _time(msg.created_at);

        let bubble = _esc(msg.content);

        // Choice buttons inside the bubble (only for pending questions)
        if (msg.status === 'pending' && msg.message_type === 'question') {
            if (msg.response_type === 'boolean') {
                bubble += '<div class="msg-choices">'
                  + '<button onclick="threadRespond(\\'' + tid + '\\',\\'true\\')">Yes</button>'
                  + '<button onclick="threadRespond(\\'' + tid + '\\',\\'false\\')">No</button></div>';
            } else if (msg.response_type === 'choice' && msg.choices) {
                bubble += '<div class="msg-choices">';
                for (const c of msg.choices) {
                    bubble += '<button onclick="threadRespond(\\'' + tid + '\\',\\'' + _esc(c.key) + '\\')">'
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
        const thread = data.thread;
        const msgs = data.messages || [];
        const tid = thread.thread_id;

        let html = '';
        for (const m of msgs) html += _msgHtml(m, tid);

        const pending = msgs.filter(m => m.status === 'pending');
        const hasPending = pending.length > 0;
        const isOpen = thread.status === 'open';

        let status = '';
        if (thread.status === 'closed') status = 'Thread closed';
        else if (hasPending) status = 'Waiting for your response...';

        // Use the appropriate wrapper class
        const wrapClass = inst.mode === 'pane' ? 'thread-chat-pane' : 'thread-chat-standalone';
        inst.container.innerHTML =
            '<div class="' + wrapClass + '">'
            + '<div class="thread-chat-messages" id="tc-msgs-' + tid + '">' + html + '</div>'
            + (isOpen
                ? '<div class="thread-input" id="tc-input-' + tid + '">'
                  + '<input type="text" placeholder="Type a message..." '
                  + 'onkeydown="if(event.key===\\'Enter\\'&&!event.shiftKey){event.preventDefault();threadSendInput(\\'' + tid + '\\')}" />'
                  + '<button onclick="threadSendInput(\\'' + tid + '\\')">Send</button></div>'
                : '')
            + (status ? '<div class="thread-status-bar">' + _esc(status) + '</div>' : '')
            + '</div>';

        const el = document.getElementById('tc-msgs-' + tid);
        if (el) el.scrollTop = el.scrollHeight;
        inst.lastCount = msgs.length;
    }

    async function _fetch(tid) {
        const r = await fetch('/api/threads/' + tid);
        if (!r.ok) return null;
        return await r.json();
    }

    async function _poll(tid) {
        const inst = _instances[tid];
        if (!inst) return;
        const data = await _fetch(tid);
        if (data && (data.messages || []).length !== (inst.lastCount || 0)) {
            _render(inst, data);
        }
    }

    // ---- Public API ----

    /**
     * Mount a ThreadChat into a container element.
     * @param {HTMLElement} container - DOM element to render into
     * @param {string} threadId - thread to display
     * @param {object} opts - { mode: 'standalone'|'pane' } (default: standalone)
     */
    window.attachThreadChat = function(container, threadId, opts) {
        opts = opts || {};
        const inst = {
            container: container,
            mode: opts.mode || 'standalone',
            lastCount: 0,
            pollInterval: null,
        };
        _instances[threadId] = inst;

        _fetch(threadId).then(data => {
            if (!data) { container.innerHTML = '<div class="empty-state">Thread not found</div>'; return; }
            _render(inst, data);
            inst.pollInterval = setInterval(() => _poll(threadId), 3000);
        }).catch(() => {
            container.innerHTML = '<div class="empty-state">Failed to load thread</div>';
        });
    };

    /** Unmount and stop polling for a thread. */
    window.detachThreadChat = function(threadId) {
        const inst = _instances[threadId];
        if (!inst) return;
        if (inst.pollInterval) clearInterval(inst.pollInterval);
        delete _instances[threadId];
    };

    window.threadRespond = async function(tid, value) {
        if (_readOnly) return;
        try {
            const r = await fetch('/api/threads/' + tid + '/respond', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({value: value}),
            });
            if (r.ok) { const d = await _fetch(tid); if (d) _render(_instances[tid], d); }
        } catch(e) { console.error('Thread respond failed:', e); }
    };

    window.threadSendInput = function(tid) {
        const el = document.getElementById('tc-input-' + tid);
        if (!el) return;
        const input = el.querySelector('input');
        if (!input || !input.value.trim()) return;
        threadRespond(tid, input.value.trim());
        input.value = '';
    };

    // ---- Workflow view renderer: split layout (context + chat) ----
    if (typeof registerViewRenderer === 'function') {
        registerViewRenderer('thread_chat', function(container, viewId, payload) {
            const tid = payload && payload.thread_id;
            if (!tid) { container.innerHTML = '<div class="empty-state">Missing thread_id</div>'; return; }

            container.innerHTML = '';
            const layout = document.createElement('div');
            layout.className = 'thread-split-layout';

            const contentPane = document.createElement('div');
            contentPane.className = 'thread-split-content';
            contentPane.innerHTML = '<div style="color:var(--text-muted);font-size:13px">'
                + '<p style="margin-bottom:8px;font-weight:600;color:var(--text)">'
                + _esc(payload.title || 'Thread') + '</p>'
                + '<p>This panel will show related context — task details, '
                + 'notification content, or workflow state.</p></div>';
            layout.appendChild(contentPane);

            const chatPane = document.createElement('div');
            layout.appendChild(chatPane);

            container.appendChild(layout);
            attachThreadChat(chatPane, tid, { mode: 'pane' });
        });
    }

    // Cleanup on tab removal
    const _origRemove = window.removeWorkflowTab;
    if (typeof _origRemove === 'function') {
        window.removeWorkflowTab = function(viewId) {
            if (viewId && viewId.startsWith('thread-')) {
                detachThreadChat(viewId.substring(7));
            }
            return _origRemove(viewId);
        };
    }
})();
"""
