"""Threads tab — v5 dashboard surface.

Stage 4.0 shipped a placeholder. Stage 4.1 wires:
- URL routing for nested thread paths and inspect modals.
- Recursive UI scaffold: top-level list, breadcrumbs, depth ≥ 1 detail.
- Stub list + card content (real card layouts land in 4.2+).

UX.md §2 (navigation) and §11 (URLs) are the spec.
"""

from __future__ import annotations


def _threads_v5_script() -> str:
    return r"""
// ===========================================================================
// Threads tab v5 — Stage 4.1 URL routing + recursive UI scaffold
// ===========================================================================

(function () {
    if (typeof window.loadThreads === "function") return;

    // ----- State ---------------------------------------------------------
    //
    // window._threadsState is the single source of truth. _persistHash
    // (in script_main.py) reads it to encode the URL; _initFromHash writes
    // it to seed the initial render.
    //
    // Shape:
    //   { path: ['th-abc', 'th-def', ...],  // depth-N nested
    //     inspect: 'ci-7' | 'ev-42' | null }
    if (!window._threadsState) {
        window._threadsState = { path: [], inspect: null };
    }

    function _esc(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // Public navigation helpers — exposed on window so other modules can
    // navigate (e.g., notification toast → "open in Threads tab").

    window.threadsPushPath = function (segment) {
        if (!segment) return;
        window._threadsState.path.push(segment);
        window._threadsState.inspect = null;  // clear inspector on nav
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    window.threadsSetPath = function (parts) {
        window._threadsState.path = (parts || []).slice();
        window._threadsState.inspect = null;
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    window.threadsBack = function () {
        if (window._threadsState.path.length === 0) return;
        window._threadsState.path.pop();
        window._threadsState.inspect = null;
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    window.threadsOpenInspector = function (itemId) {
        if (!itemId) return;
        window._threadsState.inspect = itemId;
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    window.threadsCloseInspector = function () {
        window._threadsState.inspect = null;
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    // ----- Rendering -----------------------------------------------------

    function renderThreads() {
        const panel = document.getElementById("panel-threads");
        if (!panel) return;
        const state = window._threadsState || { path: [], inspect: null };

        let html = "";
        html += renderBreadcrumbs(state.path);

        if (state.path.length === 0) {
            html += renderTopLevel();
        } else {
            html += renderThreadDetail(state.path[state.path.length - 1]);
        }
        if (state.inspect) {
            html += renderInspector(state.inspect);
        }
        panel.innerHTML = html;
    }

    function renderBreadcrumbs(path) {
        // Stage 4.1: minimal breadcrumb with back button. Polishing
        // (visual styling, click-to-jump-to-depth) lands in 4.2+.
        let html = '<nav class="threads-v5-breadcrumbs">';
        html += '<button class="threads-v5-back" '
              + 'onclick="threadsBack()" '
              + (path.length === 0 ? 'disabled' : '')
              + ' title="Back">&larr; Back</button>';
        html += '<span class="threads-v5-crumb-sep">Threads</span>';
        for (let i = 0; i < path.length; i++) {
            html += '<span class="threads-v5-crumb-sep">/</span>';
            const isLast = (i === path.length - 1);
            const segment = path[i];
            if (isLast) {
                html += '<span class="threads-v5-crumb threads-v5-crumb-current">'
                      + _esc(segment) + '</span>';
            } else {
                // Click to jump to this depth
                const subPath = path.slice(0, i + 1);
                const json = JSON.stringify(subPath).replace(/"/g, "&quot;");
                html += '<a href="#" class="threads-v5-crumb" '
                      + 'onclick="event.preventDefault();threadsSetPath('
                      + json + ')">' + _esc(segment) + '</a>';
            }
        }
        html += '</nav>';
        return html;
    }

    if (!window._topLevelCache) window._topLevelCache = null;
    if (!window._topLevelFilters) {
        window._topLevelFilters = {
            q: '',                    // search query
            state: '',                 // FSM state filter
            subtype: '',               // '' | 'task'
            show_later: false,
            include_mid_process: false, // Phase 4: show in-flight states
        };
    }

    function _filterParams() {
        const f = window._topLevelFilters;
        const params = new URLSearchParams();
        if (f.q) params.set('q', f.q);
        if (f.state) params.set('state', f.state);
        if (f.subtype) params.set('subtype', f.subtype);
        if (f.show_later) params.set('show_later', '1');
        if (f.include_mid_process) params.set('include_mid_process', '1');
        return params.toString();
    }

    window.threadsSetFilter = function (key, value) {
        window._topLevelFilters[key] = value;
        window._topLevelCache = null;  // invalidate; refetch
        renderThreads();
    };

    function renderTopLevel() {
        // Stage 4.8: pass filter chips + search query.
        if (window._topLevelCache !== null) {
            return _renderTopLevelHtml(window._topLevelCache);
        }
        const qs = _filterParams();
        const url = '/api/threads' + (qs ? '?' + qs : '');
        fetch(url)
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                window._topLevelCache = data.threads || [];
                if (typeof window._renderActiveThread === 'function') {
                    window._renderActiveThread();
                }
            })
            .catch(err => {
                window._topLevelCache = [];
                console.warn('Top-level threads fetch failed:', err);
                if (typeof window._renderActiveThread === 'function') {
                    window._renderActiveThread();
                }
            });
        return '<div class="threads-v5-loading">Loading Threads...</div>';
    }

    function _renderFilterBar() {
        const f = window._topLevelFilters;
        const stateOpts = [
            ['', 'All states'],
            ['awaiting_intent_confirmation', 'Awaiting intent confirmation'],
            ['awaiting_context_confirmation', 'Awaiting context confirmation'],
            ['awaiting_intent_clarification', 'Awaiting intent clarification'],
            ['awaiting_context_clarification', 'Awaiting context clarification'],
            ['awaiting_action_clarification', 'Awaiting action clarification'],
            ['awaiting_confirmation', 'Awaiting consent'],
            ['awaiting_review', 'Awaiting review'],
            ['awaiting_redirect', 'Awaiting redirect'],
            ['executing', 'Executing'],
            ['cleaning_up', 'Cleaning up'],
            ['done', 'Done'],
            ['dismissed', 'Dismissed'],
        ];
        const subtypeOpts = [
            ['', 'All'],
            ['task', 'Tasks only'],
        ];
        let html = '<div class="threads-v5-filter-bar">';
        html += '<input type="text" class="threads-v5-search" '
              + 'placeholder="Search threads..." '
              + 'value="' + _esc(f.q) + '" '
              + 'oninput="threadsSetFilter(\'q\', this.value)">';
        html += '<select class="threads-v5-filter-select" '
              + 'onchange="threadsSetFilter(\'state\', this.value)">';
        for (const [v, label] of stateOpts) {
            html += '<option value="' + _esc(v) + '"'
                  + (f.state === v ? ' selected' : '') + '>'
                  + _esc(label) + '</option>';
        }
        html += '</select>';
        html += '<select class="threads-v5-filter-select" '
              + 'onchange="threadsSetFilter(\'subtype\', this.value)">';
        for (const [v, label] of subtypeOpts) {
            html += '<option value="' + _esc(v) + '"'
                  + (f.subtype === v ? ' selected' : '') + '>'
                  + _esc(label) + '</option>';
        }
        html += '</select>';
        html += '<label class="threads-v5-show-later">'
              + '<input type="checkbox"'
              + (f.show_later ? ' checked' : '')
              + ' onchange="threadsSetFilter(\'show_later\', this.checked)">'
              + ' Show deferred</label>';
        // Phase 4: surface in-flight states (AWAITING_INFERENCE,
        // INFERRING_*, EXECUTING, MONITORING, CLEANING_UP). Off by
        // default — these are agent-internal states the user can't
        // act on. Useful for "what's the agent doing right now?"
        // and for debugging.
        html += '<label class="threads-v5-show-mid-process" '
              + 'title="Show threads currently being inferred or executing — useful for auditing what the agent is doing without surfacing a card.">'
              + '<input type="checkbox"'
              + (f.include_mid_process ? ' checked' : '')
              + ' onchange="threadsSetFilter(\'include_mid_process\', this.checked)">'
              + ' Show mid-process</label>';
        html += '</div>';
        return html;
    }

    window.invalidateTopLevelCache = function () {
        window._topLevelCache = null;
    };

    function _renderTopLevelHtml(threads) {
        let html = '<div class="threads-v5-top">';
        html += '<h2>Threads <span class="threads-v5-count">('
              + (Array.isArray(threads) ? threads.length : 0) + ')</span></h2>';
        html += _renderFilterBar();
        if (!Array.isArray(threads) || threads.length === 0) {
            const f = window._topLevelFilters || {};
            const filtered = !!(f.q || f.state || f.subtype);
            html += '<p class="threads-v5-empty-state">'
                  + (filtered
                        ? 'No Threads match the current filters.'
                        : 'No active Threads. As source scanners (journal, '
                          + 'Chrome, email) run, they\'ll surface here.')
                  + '</p>';
            html += '</div>';
            return html;
        }
        html += '<ul class="threads-v5-toplist">';
        for (const t of threads) {
            html += _renderTopLevelCard(t);
        }
        html += '</ul>';
        html += '</div>';
        return html;
    }

    function _renderTopLevelCard(t) {
        const urgent = t.urgency === "surface_now";
        const hasLater = !!t.has_been_later;
        const stateLabel = (t.fsm_state || "").replace(/_/g, " ");
        const intent = (t.intent && t.intent.text) || t.title || t.thread_id;
        // Phase 4: mid_process display_mode → muted styling, no
        // action affordances. The user can still click through to
        // the detail view to inspect the event log.
        const isMidProcess = t.display_mode === "mid_process";
        const midProcessClass = isMidProcess
            ? ' threads-v5-mid-process' : '';
        return (
            '<li class="threads-v5-toplist-card'
            + (urgent ? ' threads-v5-urgent' : '')
            + midProcessClass + '" '
            +   'onclick="threadsPushPath(\'' + _esc(t.thread_id) + '\')">'
            + '<div class="threads-v5-toplist-meta">'
            +   (urgent
                    ? '<span class="threads-v5-urgency-pill high">!</span>'
                    : '')
            +   (hasLater
                    ? '<span class="threads-v5-later-icon" '
                    +   'title="This thread has been deferred at least once">'
                    +   '<svg width="12" height="12" viewBox="0 0 24 24" '
                    +     'fill="none" stroke="currentColor" stroke-width="2" '
                    +     'stroke-linecap="round" stroke-linejoin="round">'
                    +     '<circle cx="12" cy="12" r="10"></circle>'
                    +     '<polyline points="12 6 12 12 16 14"></polyline>'
                    +   '</svg>'
                    +   '</span>'
                    : '')
            +   '<span class="threads-v5-toplist-state">'
            +     _esc(stateLabel) + '</span>'
            + '</div>'
            + '<div class="threads-v5-toplist-title">'
            +   _esc(t.title || t.thread_id) + '</div>'
            + '<div class="threads-v5-toplist-intent">'
            +   _esc(intent.length > 200
                        ? intent.slice(0, 197) + '...' : intent)
            + '</div>'
            + '<div class="threads-v5-toplist-row-actions" '
            +   'onclick="event.stopPropagation()">'
            +   '<button class="threads-v5-btn-icon" '
            +     'title="Later — left-click defers 6h; right-click for options" '
            +     'onclick="threadCommitAction(\'' + _esc(t.thread_id)
            +     '\', \'later\', {hours: 6})" '
            +     'oncontextmenu="event.preventDefault();'
            +     'threadsShowLaterPopup(this, \'' + _esc(t.thread_id) + '\')">'
            +     '<svg width="14" height="14" viewBox="0 0 24 24" '
            +       'fill="none" stroke="currentColor" stroke-width="2" '
            +       'stroke-linecap="round" stroke-linejoin="round">'
            +       '<circle cx="12" cy="12" r="10"></circle>'
            +       '<polyline points="12 6 12 12 16 14"></polyline>'
            +     '</svg>'
            +   '</button>'
            + '</div>'
            + '</li>'
        );
    }

    // Per-thread fetch cache so re-renders don't re-fetch.
    if (!window._threadDetailCache) window._threadDetailCache = {};

    function renderThreadDetail(threadId) {
        // Stage 4.3: real backend fetch. Cached after first fetch
        // until the user commits something (Accept / Redirect /
        // etc.) which calls invalidateThreadCache.
        if (typeof window.renderConfirmationCard !== "function") {
            return '<div class="threads-v5-detail">'
                 + '<p>Card module not loaded.</p></div>';
        }
        const cached = window._threadDetailCache[threadId];
        if (cached) {
            return window.renderConfirmationCard(cached);
        }
        // Trigger async fetch and re-render
        fetch('/api/threads/' + encodeURIComponent(threadId))
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                window._threadDetailCache[threadId] = data;
                if (typeof window._renderActiveThread === 'function') {
                    window._renderActiveThread();
                }
            })
            .catch(err => {
                window._threadDetailCache[threadId] = {
                    thread_id: threadId,
                    title: "(fetch failed)",
                    fsm_state: "(unknown)",
                    intent: { text: 'Failed to load: ' + err, editable: false },
                    context_items: [],
                    actions: [],
                    namespace_tags: [],
                    can_clean_up: false,
                    sub_thread_count: 0,
                };
                if (typeof window._renderActiveThread === 'function') {
                    window._renderActiveThread();
                }
            });
        return '<div class="threads-v5-loading">Loading thread '
             + _esc(threadId) + '...</div>';
    }

    window.invalidateThreadCache = function (threadId) {
        if (threadId) delete window._threadDetailCache[threadId];
        else window._threadDetailCache = {};
    };

    // ----- Later hover popup ------------------------------------------
    //
    // UX.md §13.3 — quick-pick durations. The Later button on
    // every card has a hover-popup with: +1h / +3h / +6h / +12h /
    // +24h / next week. Default click (if popup not engaged) =
    // 6h (matches the backend default).

    const _laterDurations = [
        { hours: 1, label: '+1h' },
        { hours: 3, label: '+3h' },
        { hours: 6, label: '+6h (default)' },
        { hours: 12, label: '+12h' },
        { hours: 24, label: '+24h' },
        { hours: 24 * 7, label: 'next week' },
    ];

    window.threadsShowLaterPopup = function (anchorEl, threadId) {
        // Remove any existing popup
        document.querySelectorAll('.threads-v5-later-popup').forEach(p => p.remove());
        const popup = document.createElement('div');
        popup.className = 'threads-v5-later-popup';
        for (const d of _laterDurations) {
            const btn = document.createElement('button');
            btn.className = 'threads-v5-later-option';
            btn.textContent = d.label;
            btn.onclick = (e) => {
                e.stopPropagation();
                popup.remove();
                threadCommitAction(threadId, 'later', { hours: d.hours });
            };
            popup.appendChild(btn);
        }
        // Position near the anchor
        const rect = anchorEl.getBoundingClientRect();
        popup.style.position = 'fixed';
        popup.style.top = (rect.bottom + 4) + 'px';
        popup.style.left = rect.left + 'px';
        document.body.appendChild(popup);

        // Click-outside-to-close
        const close = (ev) => {
            if (!popup.contains(ev.target) && ev.target !== anchorEl) {
                popup.remove();
                document.removeEventListener('mousedown', close);
            }
        };
        // Defer attach so the click that opened the popup doesn't
        // immediately close it
        setTimeout(() => document.addEventListener('mousedown', close), 50);
    };

    // Wire footer button clicks into the backend.
    window.threadCommitAction = async function (threadId, action, body) {
        const url = '/api/threads/' + encodeURIComponent(threadId)
                  + '/' + action;
        try {
            const resp = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body || {}),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                console.warn('Thread action failed:', err);
                alert('Action failed: ' + (err.error || resp.statusText));
                return false;
            }
            window.invalidateThreadCache(threadId);
            window.invalidateTopLevelCache();
            // After Accept/Dismiss/etc, navigate up if we were inside the
            // Thread; otherwise just refresh the list.
            const state = window._threadsState || { path: [] };
            if (state.path.length > 0
                && state.path[state.path.length - 1] === threadId) {
                threadsBack();
            } else {
                renderThreads();
            }
            return true;
        } catch (e) {
            console.warn('Thread action exception:', e);
            return false;
        }
    };

    function renderInspector(itemId) {
        // Stage 4.1: minimal modal scaffold. Stage 4.5 + 4.6 implement
        // the real per-item-type modals (context items, actions, events).
        return (
            '<div class="threads-v5-modal-backdrop" '
            +   'onclick="threadsCloseInspector()">'
            + '<div class="threads-v5-modal" onclick="event.stopPropagation()">'
            +   '<div class="threads-v5-modal-header">'
            +     '<span>Inspector: ' + _esc(itemId) + '</span>'
            +     '<button onclick="threadsCloseInspector()" '
            +       'class="threads-v5-modal-close">&times;</button>'
            +   '</div>'
            +   '<div class="threads-v5-modal-body">'
            +     '<p>Stage 4.1 — inspector scaffold.</p>'
            +     '<p>The real per-item-type modals land in 4.5/4.6.</p>'
            +   '</div>'
            + '</div>'
            + '</div>'
        );
    }

    window.loadThreads = function (_opts) {
        // Apply any state seeded by _initFromHash (script_main.py)
        renderThreads();
    };

    // Exposed so card-state mutators (in script_threads_v5_card.py)
    // can trigger a re-render after toggling X-flags or changing
    // focus, without re-implementing renderThreads here.
    window._renderActiveThread = renderThreads;

    // Register with staticLoaders if available
    try {
        if (typeof window.staticLoaders === "object" && window.staticLoaders) {
            window.staticLoaders.threads = window.loadThreads;
        }
    } catch (e) { /* deferred to script_main's pre-registration */ }

    // Hashchange listener: when the user uses browser back/forward, the
    // hash changes; re-extract state and re-render iff currently on the
    // Threads tab. (script_main.py already calls _initFromHash on
    // pageload; this handles in-session hash changes.)
    window.addEventListener("hashchange", function () {
        const params = new URLSearchParams(
            (window.location.hash || "").replace(/^#/, "")
        );
        if (params.get("tab") === "threads") {
            const tpath = params.get("tpath") || "";
            window._threadsState = {
                path: tpath ? tpath.split("/").filter(Boolean) : [],
                inspect: params.get("inspect") || null,
            };
            renderThreads();
        }
    });
})();
"""


def _threads_v5_styles() -> str:
    return r"""
/* Stage 4.1 — placeholder + breadcrumbs + inspector modal scaffold */

#panel-threads {
    padding: 0;
}

.threads-v5-placeholder {
    max-width: 720px;
    margin: 3em auto;
    padding: 1.5em 2em;
    background: var(--bg-secondary, #1a1a1a);
    border-radius: 10px;
    border: 1px solid var(--border, #333);
    color: var(--text, #ddd);
}

.threads-v5-stage-note {
    color: var(--text-muted, #888);
    font-size: 13px;
    margin-top: 1em;
}

.threads-v5-stage-note code {
    background: var(--bg-tertiary, #2a2a2a);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 12px;
}

/* Breadcrumb bar */
.threads-v5-breadcrumbs {
    padding: 12px 20px;
    border-bottom: 1px solid var(--border, #333);
    background: var(--bg-secondary, #1a1a1a);
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text-muted, #888);
}

.threads-v5-back {
    background: var(--bg-tertiary, #2a2a2a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 12px;
    cursor: pointer;
    margin-right: 8px;
}

.threads-v5-back:disabled {
    opacity: 0.4;
    cursor: default;
}

.threads-v5-crumb {
    color: var(--accent, #4a7fc1);
    text-decoration: none;
    font-family: var(--font-mono, monospace);
}

.threads-v5-crumb:hover {
    text-decoration: underline;
}

.threads-v5-crumb-current {
    color: var(--text, #ddd);
    font-family: var(--font-mono, monospace);
    font-weight: 600;
}

.threads-v5-crumb-sep {
    color: var(--text-muted, #666);
}

/* Top-level + detail panes */
.threads-v5-top, .threads-v5-detail {
    max-width: 1100px;
    margin: 2em auto;
    padding: 1.5em 2em;
    background: var(--bg-secondary, #1a1a1a);
    border-radius: 10px;
    border: 1px solid var(--border, #333);
    color: var(--text, #ddd);
}

.threads-v5-top h2, .threads-v5-detail h2 {
    margin-top: 0;
}

.threads-v5-count {
    color: var(--text-muted, #888);
    font-weight: 400;
    font-size: 80%;
}

.threads-v5-empty-state {
    color: var(--text-muted, #888);
    font-style: italic;
    margin-top: 1.5em;
}

.threads-v5-loading {
    color: var(--text-muted, #888);
    text-align: center;
    padding: 2em;
}

/* Top-level Thread cards (list view) */
.threads-v5-toplist {
    list-style: none;
    padding: 0;
    margin: 0;
}

.threads-v5-toplist-card {
    background: var(--bg-tertiary, #0f0f0f);
    border: 1px solid var(--border, #333);
    border-left: 3px solid transparent;
    border-radius: 6px;
    padding: 12px 16px;
    margin-bottom: 8px;
    cursor: pointer;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 8px;
    align-items: center;
}

.threads-v5-toplist-card:hover {
    border-color: var(--accent, #4a7fc1);
    background: var(--bg-secondary, #1a1a1a);
}

.threads-v5-toplist-card.threads-v5-urgent {
    border-left-color: #c0392b;
}

/* Phase 4: mid-process cards. Visible only when "Show mid-process"
   is toggled on. Muted so they're clearly distinguishable from
   actionable cards the user is meant to act on. */
.threads-v5-toplist-card.threads-v5-mid-process {
    opacity: 0.55;
    border-left-style: dashed;
    border-left-color: var(--text-muted, #888);
    cursor: default;
}

.threads-v5-toplist-card.threads-v5-mid-process:hover {
    opacity: 0.75;
    border-color: var(--text-muted, #888);
}

.threads-v5-show-mid-process {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: var(--text-muted, #888);
    cursor: help;
}

.threads-v5-toplist-meta {
    grid-row: 1;
    grid-column: 1;
    display: flex;
    gap: 6px;
    align-items: center;
    margin-bottom: 4px;
    font-size: 11px;
    color: var(--text-muted, #888);
    text-transform: capitalize;
}

.threads-v5-toplist-state {
    text-transform: capitalize;
}

.threads-v5-later-icon {
    color: var(--text-muted, #666);
    line-height: 0;
}

.threads-v5-toplist-title {
    grid-row: 2;
    grid-column: 1;
    font-weight: 600;
    font-size: 14px;
    color: var(--text, #ddd);
}

.threads-v5-toplist-intent {
    grid-row: 3;
    grid-column: 1;
    font-size: 12px;
    color: var(--text-muted, #888);
    margin-top: 4px;
}

.threads-v5-toplist-row-actions {
    grid-row: 1 / span 3;
    grid-column: 2;
    display: flex;
    gap: 4px;
    align-items: center;
}

/* Stage 4.8 — search + filter bar */
.threads-v5-filter-bar {
    display: flex;
    gap: 10px;
    align-items: center;
    margin: 10px 0 16px 0;
    flex-wrap: wrap;
}

.threads-v5-search {
    flex: 1 1 320px;
    background: var(--bg-tertiary, #0f0f0f);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

.threads-v5-filter-select {
    background: var(--bg-tertiary, #0f0f0f);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

.threads-v5-show-later {
    color: var(--text-muted, #888);
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 4px;
}

/* Stage 4.15 — Legacy-tabs toggle */
.tab-btn-toggle {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 4px 9px;
    font-size: 11px;
    cursor: pointer;
    margin-left: 6px;
}
.tab-btn-toggle:hover {
    color: var(--text, #ddd);
    border-color: var(--accent, #4a7fc1);
}
.tab-btn-toggle.active {
    background: var(--accent, #4a7fc1);
    color: white;
    border-color: var(--accent, #4a7fc1);
}

/* Hide v4 legacy tabs when the toggle is OFF. */
body.hide-legacy-tabs .tab-btn[data-tab="today"],
body.hide-legacy-tabs .tab-btn[data-tab="review"],
body.hide-legacy-tabs .tab-btn[data-tab="review-queue"],
body.hide-legacy-tabs .tab-btn[data-tab="daily-log"],
body.hide-legacy-tabs .tab-btn[data-tab="engage"] {
    display: none !important;
}

/* Stage 4.10 — Later hover popup */
.threads-v5-later-popup {
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 4px;
    box-shadow: 0 4px 18px rgba(0, 0, 0, 0.6);
    z-index: 2000;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 140px;
}

.threads-v5-later-option {
    background: transparent;
    color: var(--text, #ddd);
    border: none;
    border-radius: 4px;
    padding: 6px 14px;
    text-align: left;
    font-size: 13px;
    cursor: pointer;
}
.threads-v5-later-option:hover {
    background: var(--bg-tertiary, #1a1a1a);
}

.threads-v5-demo-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 1em;
}

.threads-v5-demo-btn {
    background: var(--accent, #4a7fc1);
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 13px;
    cursor: pointer;
}

.threads-v5-demo-btn:hover {
    filter: brightness(1.1);
}

/* Modal scaffold */
.threads-v5-modal-backdrop {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0, 0, 0, 0.55);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
}

.threads-v5-modal {
    background: var(--bg, #0d0d0d);
    border: 1px solid var(--border, #333);
    border-radius: 10px;
    width: 80%;
    max-width: 640px;
    max-height: 80%;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.threads-v5-modal-header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border, #333);
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: var(--text, #ddd);
    font-weight: 600;
}

.threads-v5-modal-close {
    background: transparent;
    color: var(--text-muted, #888);
    border: none;
    font-size: 24px;
    cursor: pointer;
    line-height: 1;
}

.threads-v5-modal-body {
    padding: 18px 20px;
    color: var(--text, #ddd);
    overflow-y: auto;
}
"""
