"""Reusable chat sidebar — slides in from the right, mounts a conversation.

Exposes ``window.wbChatSidebar`` as the sole opener / closer / probe API.
Any dashboard surface (the Jobs help button is the first consumer)
calls ``wbChatSidebar.open({conversation_id, title, bound_tab?})`` after
its backend creates a conversation and spawns the agent that drives it.

The renderer reuses ``attachConversationChat`` / ``detachConversationChat``
from ``tabs/conversations.py`` (already container-agnostic, mode='pane').
The sidebar itself is just the right-rail chrome plus a tab-binding
visibility coordinator.

Two-axis state:

* ``body.wb-chat-mounted`` — there is a live chat instance attached.
  Stays through tab switches when ``bound_tab`` is set.
* ``body.wb-chat-visible`` — the sidebar should currently be shown
  with the squish active. Removed when bound and the active tab does
  not match.

The chat instance keeps polling ``/api/conversations/<id>`` while
hidden, so messages already include the agent's latest output when
the user navigates back to the bound tab.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Chat sidebar — reusable right-rail conversation surface ----
(function () {
    let _state = null;  // { conversation_id, title, bound_tab, on_close } | null

    function _activeTabName() {
        const btn = document.querySelector('.tab-btn.active');
        return btn ? btn.dataset.tab : '';
    }

    function _evaluateVisibility() {
        if (!_state) return;
        const shouldShow = !_state.bound_tab
            || _state.bound_tab === _activeTabName();
        // Toggle on <html> (not body) — body padding is overridden by
        // some other layout rule in this codebase even with !important;
        // <html> padding squishes the page reliably.
        document.documentElement.classList.toggle('wb-chat-visible', shouldShow);
    }

    function _onTabClick(ev) {
        const btn = ev.target.closest('.tab-btn');
        if (!btn) return;
        // switchTab applies .active synchronously; defer to next tick so
        // _activeTabName() reflects the new tab.
        setTimeout(_evaluateVisibility, 0);
    }

    // Kick off visibility re-evaluation on any nav-bar click (covers both
    // static tabs and dynamically-injected wv-* workflow-tab buttons).
    document.addEventListener('click', _onTabClick, true);

    window.wbChatSidebar = {
        open(opts) {
            opts = opts || {};
            const cid = opts.conversation_id;
            if (!cid) {
                console.warn('wbChatSidebar.open requires conversation_id');
                return;
            }
            // If already mounted with a different conversation, swap.
            if (_state && _state.conversation_id !== cid) {
                this.close();
            }
            const title = opts.title || 'Chat';
            const titleEl = document.getElementById('wb-chat-title');
            const body = document.getElementById('wb-chat-body');
            if (!titleEl || !body) {
                console.warn('wb-chat-sidebar markup missing from page');
                return;
            }
            titleEl.textContent = title;
            // Reuse the existing conversation_chat renderer in pane mode —
            // it owns its own polling, message rendering, and input box.
            // Pass an onClosed hook so the sidebar slides itself away
            // when the agent ends the conversation. The 2.5s grace lets
            // the user read the agent's final message before the
            // sidebar disappears.
            window.attachConversationChat(body, cid, {
                mode: 'pane',
                onClosed: () => {
                    setTimeout(() => {
                        // Only auto-close if THIS conversation is still
                        // the one mounted — guards against the user
                        // having opened a different chat in the meantime.
                        if (_state && _state.conversation_id === cid) {
                            window.wbChatSidebar.close({ skipServerClose: true });
                        }
                    }, 2500);
                },
            });

            _state = {
                conversation_id: cid,
                title: title,
                bound_tab: opts.bound_tab || null,
                on_close: opts.on_close || null,
            };
            document.documentElement.classList.add('wb-chat-mounted');
            _evaluateVisibility();
        },

        close(opts) {
            if (!_state) return;
            const cid = _state.conversation_id;
            const onClose = _state.on_close;
            const skipServerClose = !!(opts && opts.skipServerClose);
            // Detach the chat (stops polling, clears interval).
            try {
                if (window.detachConversationChat) {
                    window.detachConversationChat(cid);
                }
            } catch (e) {
                console.warn('detachConversationChat threw', e);
            }
            // Close the conversation server-side so the agent's next
            // conversation_ask returns "closed" and it exits cleanly.
            // Skipped when the conversation is ALREADY closed (e.g.
            // agent-driven auto-close path) — re-closing is a no-op
            // but logs a 404 line; cleaner to not call.
            if (!skipServerClose) {
                try {
                    fetch('/api/conversations/' + encodeURIComponent(cid)
                          + '/close', { method: 'POST' });
                } catch (e) { /* best-effort */ }
            }

            document.documentElement.classList.remove('wb-chat-mounted');
            document.documentElement.classList.remove('wb-chat-visible');
            const body = document.getElementById('wb-chat-body');
            if (body) body.innerHTML = '';
            _state = null;
            if (typeof onClose === 'function') {
                try { onClose(); } catch (e) { console.warn(e); }
            }
        },

        isOpen() { return _state !== null; },
        isVisible() {
            return _state !== null
                && document.documentElement.classList.contains('wb-chat-visible');
        },
        currentConversationId() {
            return _state ? _state.conversation_id : null;
        },
    };
})();
"""


def styles() -> str:
    return r"""
/* ---- Chat sidebar (reusable right-rail) ---- */
:root {
    --wb-chat-sidebar-width: 0px;
    transition: padding-right 0.22s ease-out;
    padding-right: var(--wb-chat-sidebar-width);
}

/* When visible, give the sidebar its width. Removing wb-chat-visible
   automatically restores the page to full width without unmounting the
   chat instance. We toggle on <html> (not body) — body padding is
   overridden by another layout rule in this codebase, but <html>
   padding squishes reliably. The sidebar uses position: fixed and
   floats above the right edge of the viewport, so it's not affected
   by html's padding. */
html.wb-chat-visible {
    --wb-chat-sidebar-width: 420px;
}

.wb-chat-sidebar {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: 420px;
    max-width: 100vw;
    background: var(--bg-secondary);
    border-left: 1px solid var(--border);
    box-shadow: -4px 0 12px rgba(0, 0, 0, 0.4);
    transform: translateX(100%);
    transition: transform 0.22s ease-out;
    display: flex;
    flex-direction: column;
    z-index: 100;
    color: var(--text-primary);
}

html.wb-chat-visible .wb-chat-sidebar {
    transform: translateX(0);
}

.wb-chat-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    background: var(--bg-tertiary);
}

.wb-chat-title {
    font-weight: 600;
    font-size: 14px;
    color: var(--text-primary);
}

.wb-chat-close {
    background: none;
    border: none;
    font-size: 22px;
    line-height: 1;
    cursor: pointer;
    color: var(--text-secondary);
    padding: 0 4px;
}

.wb-chat-close:hover {
    color: var(--text-primary);
}

.wb-chat-body {
    flex: 1;
    min-height: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
}

/* The mounted conversation_chat tree fills the body. ``thread-chat-pane``
   only ships flex+column layout when it sits inside ``thread-split-
   layout`` (its companion selector); standing alone in our sidebar it
   needs the same shape applied here so its internal
   ``.thread-chat-messages { flex: 1; overflow-y: auto }`` gets a
   bounded parent and actually scrolls. Without this rule the pane
   grows to fit its content and overflows the sidebar. */
.wb-chat-body > .thread-chat-pane {
    flex: 1;
    min-height: 0;
    width: auto;
    display: flex;
    flex-direction: column;
    border-left: none;
}

/* Catch-all for non-pane mounts (defensive). */
.wb-chat-body > *:not(.thread-chat-pane) {
    flex: 1;
    min-height: 0;
}
"""
