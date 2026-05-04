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
        // Wave B: auto-dismiss any pending Resolution Surface toast
        // for this thread when the user opens it. The user is now
        // looking at the thread directly; the multi-surface toast
        // is redundant noise. (Telegram/Obsidian remain — those
        // surfaces don't see the user's dashboard navigation.)
        try { _autoDismissResolutionToasts(segment); } catch(e) {}
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    // 2026-05-04: open a sub-thread AND focus the right-pane editor on
    // a specific action in one click. Mirrors the v4 affordance where
    // the user could see proposed actions on each card and jump
    // straight to editing one without first entering the thread. The
    // navigation still pushes the path (so the breadcrumb / URL hash /
    // back-button all work normally), and we set ``focusedId`` on the
    // sub-thread's per-card state so its right-pane renders the action
    // editor immediately.
    window.threadsOpenSubThreadAction = function (threadId, actionId) {
        if (!threadId) return;
        if (typeof window.threadCardFocus === "function" && actionId) {
            try { window.threadCardFocus(threadId, actionId); } catch(e) {}
        }
        // threadCardFocus already triggers a re-render via
        // _renderActiveThread, but the path push has to happen too so
        // the rest of the dashboard (breadcrumb, hash, back-nav) is
        // consistent. Push *after* focus so the renderer sees the
        // focusedId on the very first frame after navigation.
        window._threadsState.path.push(threadId);
        window._threadsState.inspect = null;
        try { _autoDismissResolutionToasts(threadId); } catch(e) {}
        if (typeof _persistHash === "function") _persistHash();
        renderThreads();
    };

    // Auto-dismiss any toast or workflow-tab chip for this
    // thread. Used when the user navigates into a thread — the
    // toast becomes redundant once they're looking at the card
    // directly. (Telegram/Obsidian remain — those surfaces don't
    // see the user's dashboard navigation.)
    function _autoDismissResolutionToasts(threadId) {
        if (!threadId) return;
        const wantedViewId = "resolution-" + threadId;
        // Use the canonical dismissAndRemoveTab if it exists —
        // it cleans up the tab, panel, and the in-memory view
        // tracker. Falls back to direct DOM removal otherwise.
        if (typeof window.dismissAndRemoveTab === "function") {
            try { window.dismissAndRemoveTab(wantedViewId); } catch(e) {}
        } else {
            document.querySelectorAll('[data-view-id="' + wantedViewId + '"]')
                .forEach(el => el.remove());
            fetch('/api/workflow-views/' + wantedViewId + '/dismiss',
                  { method: 'POST' }).catch(() => {});
        }
    }

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
        // Wave D — use morphdom when available so SSE-triggered
        // re-renders preserve focus, scroll, and in-flight inputs
        // (e.g. the user typing in the search box). Falls back to
        // innerHTML when morphdom isn't loaded.
        if (typeof window._wbMorphReplace === "function") {
            window._wbMorphReplace(panel, html);
        } else {
            panel.innerHTML = html;
        }
    }

    function renderBreadcrumbs(path) {
        // Wave E: show thread titles in the breadcrumb instead of
        // the raw th-IDs. Falls back to ID when the title isn't yet
        // cached. The cache is populated by renderThreadDetail's
        // fetch, so by the time the breadcrumb renders fully, the
        // titles are usually available.
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
            const cached = (window._threadDetailCache || {})[segment];
            // Use the title if known; truncate so long titles don't
            // wrap the breadcrumb. Hover shows the full title +
            // the thread ID.
            let label = segment;
            let fullTitle = segment;
            if (cached && cached.title) {
                fullTitle = cached.title + ' · ' + segment;
                label = cached.title.length > 50
                    ? cached.title.slice(0, 47) + '…'
                    : cached.title;
            }
            const titleAttr = ' title="' + _esc(fullTitle) + '"';
            if (isLast) {
                html += '<span class="threads-v5-crumb threads-v5-crumb-current"'
                      + titleAttr + '>'
                      + _esc(label) + '</span>';
            } else {
                const subPath = path.slice(0, i + 1);
                const json = JSON.stringify(subPath).replace(/"/g, "&quot;");
                html += '<a href="#" class="threads-v5-crumb"' + titleAttr + ' '
                      + 'onclick="event.preventDefault();threadsSetPath('
                      + json + ')">' + _esc(label) + '</a>';
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
            urgency: '',               // '' | 'surface_now' | 'defer'
            show_later: false,
            include_mid_process: false, // Phase 4: show in-flight states
            has_cleanup: false,        // Wave C: cleanup-applicable only
            show_all: false,           // Wave F: show non-actionable states (terminal, proposed, ...)
        };
    }

    function _filterParams() {
        const f = window._topLevelFilters;
        const params = new URLSearchParams();
        if (f.q) params.set('q', f.q);
        if (f.state) params.set('state', f.state);
        if (f.subtype) params.set('subtype', f.subtype);
        if (f.urgency) params.set('urgency', f.urgency);
        if (f.show_later) params.set('show_later', '1');
        if (f.include_mid_process) params.set('include_mid_process', '1');
        if (f.has_cleanup) params.set('has_cleanup', '1');
        if (f.show_all) params.set('show_all', '1');
        return params.toString();
    }

    window.threadsSetFilter = function (key, value) {
        window._topLevelFilters[key] = value;
        window._topLevelCache = null;  // invalidate; refetch
        renderThreads();
    };

    // Empty-state quick actions. Helps the user get from "list is
    // empty" to "list has things to act on" without leaving the
    // dashboard.

    window.threadsClearFilters = function () {
        window._topLevelFilters = {
            q: '',
            state: '',
            subtype: '',
            urgency: '',
            show_later: false,
            include_mid_process: false,
            has_cleanup: false,
        };
        window._topLevelCache = null;
        renderThreads();
    };

    window.threadsToggleMidProcess = function () {
        const f = window._topLevelFilters || {};
        f.include_mid_process = !f.include_mid_process;
        window._topLevelCache = null;
        renderThreads();
    };

    // Run journal_v5_scan via the dashboard's MCP-style endpoint.
    // Falls back to a friendly message on error so the empty-state
    // CTA never throws an unhandled rejection.
    window.threadsRunJournalScan = function () {
        const btns = document.querySelectorAll('.threads-v5-empty-cta');
        for (const b of btns) { b.disabled = true; }
        // The dashboard exposes `/api/run/<capability>` as a
        // gateway shim so we can trigger capabilities from the UI.
        // If that route doesn't exist (the capability has to come
        // from the MCP gateway), we surface a helpful message.
        fetch('/api/run/journal_v5_scan', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({}),
        })
            .then(r => r.ok ? r.json() : Promise.reject(r.status))
            .then(data => {
                window._topLevelCache = null;
                renderThreads();
            })
            .catch(err => {
                alert('Could not run scan: ' + err
                      + '. Run "journal_v5_scan" via the MCP gateway '
                      + '(e.g. wb_run from an agent), or open Obsidian '
                      + 'and ensure the work-buddy plugin is enabled.');
                for (const b of btns) { b.disabled = false; }
            });
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
        // Wave C: urgency filter chip per UX.md §10.3.
        const urgencyOpts = [
            ['', 'Any urgency'],
            ['surface_now', 'Surface now'],
            ['defer', 'Defer'],
        ];
        html += '<select class="threads-v5-filter-select" '
              + 'title="Filter by urgency level — surface_now is the '
              + 'subset of threads that have requested immediate '
              + 'attention via the inciting context." '
              + 'onchange="threadsSetFilter(\'urgency\', this.value)">';
        for (const [v, label] of urgencyOpts) {
            html += '<option value="' + _esc(v) + '"'
                  + (f.urgency === v ? ' selected' : '') + '>'
                  + _esc(label) + '</option>';
        }
        html += '</select>';
        html += '<label class="threads-v5-show-later">'
              + '<input type="checkbox"'
              + (f.show_later ? ' checked' : '')
              + ' onchange="threadsSetFilter(\'show_later\', this.checked)">'
              + ' Show deferred</label>';
        // Wave C: has-cleanup filter chip per UX.md §10.3.
        html += '<label class="threads-v5-show-later" '
              + 'title="Show only threads whose inciting source has a '
              + 'registered cleanup adapter (you can hit Clean Up to '
              + 'mutate the source).">'
              + '<input type="checkbox"'
              + (f.has_cleanup ? ' checked' : '')
              + ' onchange="threadsSetFilter(\'has_cleanup\', this.checked)">'
              + ' Cleanup-applicable</label>';
        // Wave F: master "show all" toggle. Disables the default
        // actionable-only filter so terminal threads (done /
        // dismissed / handed_off) also show up. Useful for audit
        // and review.
        html += '<label class="threads-v5-show-later" '
              + 'title="Disable the default actionable-only filter — '
              + 'show ALL threads including done, dismissed, and '
              + 'pre-PROPOSED states.">'
              + '<input type="checkbox"'
              + (f.show_all ? ' checked' : '')
              + ' onchange="threadsSetFilter(\'show_all\', this.checked)">'
              + ' Show all states</label>';
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
        html += '<p class="threads-v5-kbd-hint">'
              + '<kbd>j</kbd>/<kbd>k</kbd> nav · '
              + '<kbd>Enter</kbd> open · '
              + '<kbd>/</kbd> search · '
              + '<kbd>?</kbd> help'
              + '</p>';
        if (!Array.isArray(threads) || threads.length === 0) {
            const f = window._topLevelFilters || {};
            const filtered = !!(f.q || f.state || f.subtype);
            if (filtered) {
                html += '<p class="threads-v5-empty-state">'
                      + 'No Threads match the current filters. '
                      + '<a href="#" onclick="threadsClearFilters();return false;">Clear filters</a>'
                      + '</p>';
            } else {
                // Calls-to-action: bridge between "list is empty"
                // and "what should I do." Surfaces the two real
                // source pipelines so the user can produce some
                // threads without leaving the dashboard.
                html += '<div class="threads-v5-empty-state">';
                html += '<p>No active Threads. As source scanners run, '
                      + 'they\'ll surface here.</p>';
                html += '<div class="threads-v5-empty-cta-row">';
                html += '<button class="threads-v5-empty-cta" '
                      + 'onclick="threadsRunJournalScan()" '
                      + 'title="Segment today\'s journal Running Notes '
                      + 'into v5 Threads via the journal_v5_scan capability">'
                      + 'Scan today\'s journal'
                      + '</button>';
                html += '<button class="threads-v5-empty-cta" '
                      + 'onclick="threadsToggleMidProcess()" '
                      + 'title="Show in-flight states (inferring, executing, '
                      + 'monitoring) — useful for auditing what the agent '
                      + 'is doing right now">'
                      + 'Show mid-process'
                      + '</button>';
                html += '</div>';
                html += '</div>';
            }
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

    // Wave F — same friendlier state copy as the detail view
    // header. Mirrored here to avoid a cross-module import.
    function _friendlyStateTop(state) {
        return ({
            "awaiting_intent_confirmation": "Confirm intent",
            "awaiting_intent_clarification": "Clarify intent",
            "awaiting_context_confirmation": "Confirm context",
            "awaiting_context_clarification": "Clarify context",
            "awaiting_action_clarification": "Clarify action",
            "awaiting_confirmation": "Approve action",
            "awaiting_review": "Review result",
            "awaiting_redirect": "Redirect needed",
            "awaiting_inference": "Queued",
            "inferring_intent": "Inferring intent",
            "inferring_context": "Inferring context",
            "inferring_action": "Inferring action",
            "executing": "Executing",
            "monitoring": "Monitoring",
            "cleaning_up": "Cleaning up",
            "done_cleanup_unsuccessful": "Cleanup failed",
            "done_cleanup_successful": "Done · cleaned",
            "done": "Done",
            "dismissed": "Dismissed",
            "handed_off": "Handed off",
            "proposed": "Proposed",
        }[state]) || (state || "").replace(/_/g, " ");
    }

    function _renderTopLevelCard(t) {
        const urgent = t.urgency === "surface_now";
        const hasLater = !!t.has_been_later;
        const stateLabel = _friendlyStateTop(t.fsm_state);
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
            +   (t.risk_highlight
                    ? '<span class="threads-v5-toplist-risk-dot '
                    +   _esc(t.risk_highlight) + '" '
                    +   'title="Risk level: ' + _esc(t.risk_highlight)
                    +   '"></span>'
                    : '')
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
            // Stage 5: group-relationship parents render through the
            // multi-column group view instead of the single
            // confirmation card. Decompose-parents and leaf threads
            // keep the standard renderer.
            if (cached.parent_relationship === "group"
                && typeof window.renderGroupView === "function") {
                return window.renderGroupView(cached);
            }
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
                // 2026-05-03 fix: don't poison the cache with a fake
                // "(fetch failed)" thread. Park the error in a parallel
                // map so the renderer can show a real retry card while
                // a future click re-fetches cleanly.
                window._threadDetailErrors = window._threadDetailErrors || {};
                window._threadDetailErrors[threadId] = String(err);
                if (typeof window._renderActiveThread === 'function') {
                    window._renderActiveThread();
                }
            });
        // Render a fetch-error card if we already failed once.
        const failed = (window._threadDetailErrors || {})[threadId];
        if (failed) {
            return '<div class="threads-v5-detail threads-v5-fetch-error">'
                 + '<h2>Couldn’t load this thread</h2>'
                 + '<p class="threads-v5-error-msg">' + _esc(failed) + '</p>'
                 + '<p class="threads-v5-error-hint">'
                 +   'Usually a transient sidecar restart. Retry, or pick '
                 +   'another thread from the list.'
                 + '</p>'
                 + '<button class="threads-v5-retry-btn" '
                 +   'onclick="(function(){'
                 +     'delete (window._threadDetailErrors||{})[\'' + _esc(threadId) + '\'];'
                 +     'delete window._threadDetailCache[\'' + _esc(threadId) + '\'];'
                 +     'window._renderActiveThread && window._renderActiveThread();'
                 +   '})()">Retry</button>'
                 + '</div>';
        }
        return '<div class="threads-v5-loading">Loading thread '
             + _esc(threadId) + '...</div>';
    }

    window.invalidateThreadCache = function (threadId) {
        if (threadId) {
            delete window._threadDetailCache[threadId];
            if (window._threadDetailErrors) delete window._threadDetailErrors[threadId];
        } else {
            window._threadDetailCache = {};
            window._threadDetailErrors = {};
        }
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
        popup.className = 'threads-v5-later-popup threads-v5-later-popup-row';
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
        // Position above the anchor, right-aligned with the parent
        // card. User-feedback iterations:
        //   v1 (column, below anchor) — right options slid off-screen.
        //   v2 (row, centered on anchor) — popped far to the left of
        //       the Later button, awkward to reach.
        //   v3 (this) — render above, right-edge aligned to the
        //       enclosing card with a small padding so the rightmost
        //       durations sit close to where the user's mouse is. Falls
        //       back to viewport-right if no card ancestor is found.
        document.body.appendChild(popup);
        const rect = anchorEl.getBoundingClientRect();
        const popRect = popup.getBoundingClientRect();
        const margin = 6;
        const cardPad = 12;
        let top = rect.top - popRect.height - margin;
        if (top < margin) {
            // No room above — fall back to below the anchor.
            top = rect.bottom + margin;
        }
        // Find the nearest card ancestor and right-align with it. A
        // little padding from the card's right edge keeps the popup
        // visually inside its container instead of brushing the edge.
        const card = anchorEl.closest(
            '.threads-v5-card, .threads-v5-mini-card'
        );
        let rightEdge;
        if (card) {
            const cardRect = card.getBoundingClientRect();
            rightEdge = cardRect.right - cardPad;
        } else {
            rightEdge = window.innerWidth - margin;
        }
        let left = rightEdge - popRect.width;
        // Clamp so the popup never falls off the left edge of the
        // viewport (small viewport / very narrow card).
        if (left < margin) left = margin;
        const maxLeft = window.innerWidth - popRect.width - margin;
        if (left > maxLeft) left = maxLeft;
        popup.style.position = 'fixed';
        popup.style.top = top + 'px';
        popup.style.left = left + 'px';

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
        // Wave E — visual loading state on the triggering button
        // (and its siblings, since the user shouldn't double-click).
        const card = document.querySelector(
            '.threads-v5-card[data-thread-id="' + threadId + '"]'
        );
        const buttons = card
            ? card.querySelectorAll('.threads-v5-card-footer button')
            : [];
        for (const b of buttons) { b.disabled = true; }
        // Wave G — bundle any edits the user made in the right pane
        // (intent rewrite, action parameter overrides) into the
        // request body for the accept/redirect path. The accept
        // route's _v5_post_action merges body data into the
        // transition data that lands in the state_transition event,
        // so edits become part of the durable audit log even when
        // the action dispatcher doesn't yet act on them.
        let mergedBody = body || {};
        if (action === 'accept' || action === 'redirect') {
            try {
                if (typeof window.threadCardCollectEdits === 'function') {
                    const edits = window.threadCardCollectEdits(threadId);
                    if (edits) {
                        mergedBody = Object.assign({}, mergedBody, edits);
                    }
                }
            } catch(e) { /* edits are optional */ }
        }
        try {
            const resp = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(mergedBody),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                console.warn('Thread action failed:', err);
                _toast('error', (err.error || resp.statusText)
                       || 'Action failed');
                return false;
            }
            window.invalidateThreadCache(threadId);
            window.invalidateTopLevelCache();
            // Wave E — confirmation toast for the user. Distinct
            // copy per action so the user sees what just landed.
            const verb = {
                accept: 'Accepted',
                dismiss: 'Dismissed',
                cleanup: 'Cleanup queued',
                later: 'Deferred',
                redirect: 'Redirected to inference',
                'retry-cleanup': 'Cleanup retried',
                'accept-cleanup-failure': 'Failure accepted',
            }[action] || (action + ' completed');
            _toast('ok', verb);
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
            _toast('error', 'Action failed: ' + (e.message || e));
            return false;
        } finally {
            for (const b of buttons) { b.disabled = false; }
        }
    };

    // Wave E — lightweight self-dismissing toast for confirmation
    // and error feedback on commit actions. Auto-dismisses after
    // 3.5s; click to dismiss early. Stacks if multiple fire in
    // quick succession.
    function _toast(kind, message) {
        let host = document.getElementById("threads-v5-toast-host");
        if (!host) {
            host = document.createElement("div");
            host.id = "threads-v5-toast-host";
            document.body.appendChild(host);
        }
        const t = document.createElement("div");
        t.className = "threads-v5-toast threads-v5-toast-" + (kind || "ok");
        t.textContent = String(message || "");
        t.addEventListener("click", () => t.remove());
        host.appendChild(t);
        setTimeout(() => {
            try { t.classList.add("threads-v5-toast-fading"); } catch(e) {}
            setTimeout(() => { try { t.remove(); } catch(e) {} }, 400);
        }, 3500);
    }

    function renderInspector(itemId) {
        // Wave C (2026-05-03): event-log inspector. UX.md §11.1
        // says the inspector ID convention is ``ev-N`` for events.
        // For now we ship the event-log inspector (the most useful
        // one); per-context-item inspector is on the right pane in
        // the card view, not here. ``ci-N`` and ``act-N`` IDs
        // round-trip through the URL but currently fall back to
        // the generic inspector.
        if (/^ev-/.test(itemId)) {
            return _renderEventLogInspector();
        }
        if (/^evlog$/.test(itemId)) {
            // Special: open the full event log for the active
            // thread. Triggered from the card's "View timeline"
            // affordance.
            return _renderEventLogInspector();
        }
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
            +     '<p>Click an event in the thread\'s timeline to '
            +     'inspect it (timeline button on the card body).</p>'
            +   '</div>'
            + '</div>'
            + '</div>'
        );
    }

    // Event-log inspector — surfaces the full event log for the
    // active thread in a modal. Useful for debugging, audit, and
    // for users who want to see the agent's full reasoning chain.
    function _renderEventLogInspector() {
        const state = window._threadsState || { path: [] };
        const tid = state.path[state.path.length - 1] || '';
        let html = '<div class="threads-v5-modal-backdrop" '
                 + 'onclick="threadsCloseInspector()">'
                 + '<div class="threads-v5-modal threads-v5-modal-wide" '
                 +   'onclick="event.stopPropagation()">'
                 + '<div class="threads-v5-modal-header">'
                 +   '<span>Event log: <code>' + _esc(tid) + '</code></span>'
                 +   '<button onclick="threadsCloseInspector()" '
                 +     'class="threads-v5-modal-close">&times;</button>'
                 + '</div>'
                 + '<div class="threads-v5-modal-body">'
                 +   '<div id="threads-v5-evlog-content">Loading...</div>'
                 + '</div>'
                 + '</div></div>';
        // Lazy-fetch the events on first render. The inspector
        // markup is a placeholder; the events go inside
        // #threads-v5-evlog-content.
        if (tid) {
            setTimeout(() => _loadEventLog(tid), 0);
        }
        return html;
    }

    function _loadEventLog(threadId) {
        fetch('/api/threads/' + encodeURIComponent(threadId) + '/events')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                const target = document.getElementById('threads-v5-evlog-content');
                if (!target) return;
                target.innerHTML = _renderEventLogList(data.events || []);
            })
            .catch(err => {
                const target = document.getElementById('threads-v5-evlog-content');
                if (target) {
                    target.innerHTML = '<p class="threads-v5-empty-state">'
                                     + 'Failed to load event log: '
                                     + _esc(String(err)) + '</p>';
                }
            });
    }

    function _renderEventLogList(events) {
        if (events.length === 0) {
            return '<p class="threads-v5-empty-state">No events yet.</p>';
        }
        let html = '<p class="threads-v5-evlog-hint">'
                 + 'Click any row to expand its full payload (model, tier, '
                 + 'and all event data).'
                 + '</p>';
        html += '<table class="threads-v5-evlog-table">';
        html += '<thead><tr>'
              + '<th></th>'  // expander column
              + '<th>#</th>'
              + '<th>When</th>'
              + '<th>Kind</th>'
              + '<th>Actor</th>'
              + '<th>Tier / Model</th>'
              + '<th>Summary</th>'
              + '</tr></thead><tbody>';
        for (const e of events) {
            const summary = _summariseEvent(e);
            const ts = e.timestamp || '';
            const rel = ts ? ('<span title="' + _esc(ts) + '">'
                              + _summariseTime(ts) + '</span>') : '';
            const tierModel = _tierModelCell(e);
            const evid = _esc(String(e.id));
            // Main row: click anywhere to toggle the detail row.
            html += '<tr class="threads-v5-evlog-row" '
                  +   'onclick="threadsToggleEvlogRow(\'' + evid + '\')">'
                  + '<td class="threads-v5-evlog-toggle" '
                  +   'aria-label="Expand row">'
                  +   '<span class="threads-v5-evlog-caret" '
                  +     'id="ev-caret-' + evid + '">&#x25B8;</span>'
                  + '</td>'
                  + '<td><code>' + evid + '</code></td>'
                  + '<td>' + rel + '</td>'
                  + '<td><code>' + _esc(e.kind) + '</code></td>'
                  + '<td>' + _esc(e.actor || '') + '</td>'
                  + '<td class="threads-v5-evlog-tier">' + tierModel + '</td>'
                  + '<td class="threads-v5-evlog-summary">' + summary + '</td>'
                  + '</tr>';
            // Hidden detail row: pretty-printed JSON payload.
            const data = e.data || {};
            const detail = _esc(JSON.stringify(data, null, 2));
            html += '<tr class="threads-v5-evlog-detail" '
                  +   'id="ev-detail-' + evid + '" '
                  +   'style="display:none">'
                  + '<td colspan="7" class="threads-v5-evlog-detail-cell">'
                  +   '<pre class="threads-v5-evlog-payload">' + detail + '</pre>'
                  + '</td>'
                  + '</tr>';
        }
        html += '</tbody></table>';
        return html;
    }

    // Toggle an event-log row's detail panel. Idempotent — clicking
    // again re-collapses it. Updates the caret glyph too.
    window.threadsToggleEvlogRow = function (eventId) {
        const detail = document.getElementById('ev-detail-' + eventId);
        const caret = document.getElementById('ev-caret-' + eventId);
        if (!detail || !caret) return;
        const open = detail.style.display !== 'none';
        detail.style.display = open ? 'none' : 'table-row';
        caret.innerHTML = open ? '&#x25B8;' : '&#x25BE;';
    };

    // Pull the model + tier hint for the Tier/Model column. Inferred
    // events carry both ``inference_tier`` (engine column) and
    // ``data.model_used`` / ``data.tier_used``.
    function _tierModelCell(e) {
        const d = e.data || {};
        const tier = d.tier_used || e.inference_tier || '';
        const model = d.model_used || '';
        if (!tier && !model) return '';
        const parts = [];
        if (tier) parts.push('<code>' + _esc(tier) + '</code>');
        if (model) parts.push('<span class="threads-v5-evlog-model" '
                            + 'title="model used">'
                            + _esc(model) + '</span>');
        return parts.join('<br>');
    }

    function _summariseEvent(e) {
        const d = e.data || {};
        if (e.kind === 'state_transition') {
            return _esc((d.from || '?') + ' → ' + (d.to || '?')
                        + ' via ' + (d.trigger || ''));
        }
        if (e.kind === 'auto_advance_decision') {
            const adv = d.advance ? '✓ advance' : '⊘ surface';
            const conf = d.confidence != null
                ? ' (' + Math.round(d.confidence * 100) + '%)' : '';
            return _esc(d.target + ': ' + adv + conf);
        }
        if (e.kind === 'intent_inferred') {
            const intent = (d.payload && d.payload.intent) || '';
            const conf = d.confidence != null
                ? ' (' + Math.round(d.confidence * 100) + '%)' : '';
            return _esc(intent + conf);
        }
        if (e.kind === 'context_inferred') {
            const refs = ((d.payload && d.payload.associated_refs) || []).length;
            return _esc(refs + ' refs');
        }
        if (e.kind === 'action_inferred') {
            const p = d.payload || {};
            return _esc((p.kind || '?') + ' / ' + (p.name || '(unnamed)'));
        }
        if (e.kind === 'inciting_event') {
            // The user noticed the inciting_event row was being
            // truncated mid-line. Surface the description / label
            // (whichever is most informative) and lean on the
            // detail row for the full payload.
            return _esc(d.description || d.label || d.title || d.line_text || '(see full payload)');
        }
        return _esc(JSON.stringify(d).slice(0, 80));
    }

    function _summariseTime(iso) {
        if (!iso) return '';
        try {
            const t = new Date(iso).getTime();
            const delta = Math.max(0, (Date.now() - t) / 1000);
            if (delta < 30) return 'just now';
            if (delta < 60) return Math.floor(delta) + 's ago';
            if (delta < 3600) return Math.floor(delta / 60) + 'm ago';
            if (delta < 86400) return Math.floor(delta / 3600) + 'h ago';
            return Math.floor(delta / 86400) + 'd ago';
        } catch(e) { return ''; }
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

    // Wave C — keyboard navigation. Per the user's MEMORY note,
    // j is UP and k is DOWN (inverted vim convention). Other
    // shortcuts:
    //   Enter / o       — open the focused thread
    //   Escape          — close inspector / go back
    //   /               — focus the search box
    //   t               — open timeline (event log) for current
    //   ?               — show keyboard hint help
    // We only intercept when the active element isn't an input/
    // textarea (so users can type "j" in the search box without
    // triggering navigation).
    if (!window._threadsKbdInstalled) {
        window._threadsKbdInstalled = true;
        document.addEventListener("keydown", function (ev) {
            // Only act when on the Threads tab
            const activeTab = (window._threadsState
                && window._threadsState.path !== undefined)
                ? "threads" : null;
            if (activeTab !== "threads") return;
            const panel = document.getElementById("panel-threads");
            if (!panel || !panel.classList.contains("active")) {
                if (panel && panel.style.display === "none") return;
            }
            // Skip if user is typing into an input/textarea
            const tag = (ev.target && ev.target.tagName) || "";
            if (tag === "INPUT" || tag === "TEXTAREA"
                || tag === "SELECT") return;
            if (ev.metaKey || ev.ctrlKey || ev.altKey) return;

            const k = ev.key;
            if (k === "j") {
                ev.preventDefault();
                _kbdMove(-1);
            } else if (k === "k") {
                ev.preventDefault();
                _kbdMove(1);
            } else if (k === "Enter" || k === "o") {
                ev.preventDefault();
                _kbdOpenFocused();
            } else if (k === "Escape") {
                // Order: 1) close inspector, 2) clear right-pane
                // focus (un-select the focused element so the right
                // pane disappears), 3) navigate back.
                if (window._threadsState
                    && window._threadsState.inspect) {
                    ev.preventDefault();
                    window.threadsCloseInspector();
                } else if (window._threadsState
                    && window._threadsState.path.length > 0
                    && window.threadCardState
                    && _activeThreadHasFocus()) {
                    ev.preventDefault();
                    const tid = window._threadsState.path[
                        window._threadsState.path.length - 1
                    ];
                    if (typeof window.threadCardFocus === "function") {
                        window.threadCardFocus(tid, null);
                    }
                } else if (window._threadsState
                    && window._threadsState.path.length > 0) {
                    ev.preventDefault();
                    window.threadsBack();
                }
            } else if (k === "/") {
                ev.preventDefault();
                const search = document.querySelector(
                    ".threads-v5-search"
                );
                if (search) search.focus();
            } else if (k === "t") {
                // Open timeline for the active thread (if any)
                if (window._threadsState
                    && window._threadsState.path.length > 0) {
                    ev.preventDefault();
                    window.threadsOpenInspector("evlog");
                }
            } else if (k === "?") {
                ev.preventDefault();
                _kbdToggleHelp();
            }
        });
    }

    // Keyboard-focus index into the top-level threads list.
    // Persists across renders so j/k stay in place.
    if (window._threadsKbdIndex === undefined) {
        window._threadsKbdIndex = -1;
    }

    function _activeThreadHasFocus() {
        // True iff the current thread's card-state has a
        // focused element in the right pane.
        const path = (window._threadsState || {}).path || [];
        if (path.length === 0) return false;
        const tid = path[path.length - 1];
        const s = (window.threadCardState || {})[tid];
        return !!(s && s.focusedId);
    }

    function _kbdMove(delta) {
        const cards = document.querySelectorAll(
            ".threads-v5-toplist-card"
        );
        if (cards.length === 0) return;
        let idx = window._threadsKbdIndex;
        idx = (idx < 0) ? 0 : (idx + delta);
        if (idx < 0) idx = 0;
        if (idx >= cards.length) idx = cards.length - 1;
        window._threadsKbdIndex = idx;
        // Apply visual focus
        cards.forEach((c, i) => {
            c.classList.toggle("threads-v5-kbd-focus", i === idx);
        });
        cards[idx].scrollIntoView({block: "nearest", behavior: "smooth"});
    }

    function _kbdOpenFocused() {
        const idx = window._threadsKbdIndex;
        const cards = document.querySelectorAll(
            ".threads-v5-toplist-card"
        );
        if (idx >= 0 && cards[idx]) cards[idx].click();
    }

    function _kbdToggleHelp() {
        let el = document.getElementById("threads-v5-kbd-help");
        if (el) { el.remove(); return; }
        el = document.createElement("div");
        el.id = "threads-v5-kbd-help";
        el.className = "threads-v5-modal-backdrop";
        el.onclick = () => el.remove();
        el.innerHTML = '<div class="threads-v5-modal" '
            + 'onclick="event.stopPropagation()" '
            + 'style="max-width:480px">'
            + '<div class="threads-v5-modal-header">'
            + '<span>Keyboard shortcuts</span>'
            + '<button class="threads-v5-modal-close" '
            + 'onclick="document.getElementById(\'threads-v5-kbd-help\').remove()">&times;</button>'
            + '</div>'
            + '<div class="threads-v5-modal-body">'
            + '<table class="threads-v5-ci-table">'
            + '<tr><th><kbd>j</kbd></th><td>Move focus up (inverted vim)</td></tr>'
            + '<tr><th><kbd>k</kbd></th><td>Move focus down (inverted vim)</td></tr>'
            + '<tr><th><kbd>Enter</kbd> or <kbd>o</kbd></th><td>Open focused thread</td></tr>'
            + '<tr><th><kbd>Escape</kbd></th><td>Close inspector / go back</td></tr>'
            + '<tr><th><kbd>/</kbd></th><td>Focus the search box</td></tr>'
            + '<tr><th><kbd>t</kbd></th><td>View timeline (when in a thread)</td></tr>'
            + '<tr><th><kbd>?</kbd></th><td>Toggle this help</td></tr>'
            + '</table>'
            + '</div></div>';
        document.body.appendChild(el);
    }

    // Wave D — server-pushed updates.
    //
    // The bootstrap layer (work_buddy/threads/bootstrap.py) emits
    // ``thread.state_changed`` on every FSM transition. We subscribe
    // here, invalidate the top-level cache, and re-render so the
    // dashboard reflects state changes without manual refresh.
    //
    // Best-effort: if the event bus isn't yet ready (race during
    // page load), we wire on the next animation frame.
    function _wireThreadStateBus() {
        if (!window.eventBus || typeof window.eventBus.on !== "function") {
            requestAnimationFrame(_wireThreadStateBus);
            return;
        }
        window.eventBus.on("thread.state_changed", (payload) => {
            try {
                // Invalidate caches so the next render fetches fresh.
                window._topLevelCache = null;
                if (window._threadDetailCache && payload && payload.thread_id) {
                    delete window._threadDetailCache[payload.thread_id];
                }
                // Invalidate ALL sub-thread caches — without the
                // changed thread's parent_id we can't be more
                // surgical, and the cardinality is small.
                window._subThreadCache = {};
                renderThreads();
            } catch (e) {
                console.warn("[threads-v5] state_changed handler:", e);
            }
        });
    }
    _wireThreadStateBus();

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

.threads-v5-empty-state a {
    color: var(--accent, #4a7fc1);
    text-decoration: none;
}
.threads-v5-empty-state a:hover { text-decoration: underline; }

.threads-v5-empty-cta-row {
    display: flex;
    gap: 12px;
    margin-top: 18px;
    flex-wrap: wrap;
}

.threads-v5-empty-cta {
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 10px 16px;
    font-size: 13px;
    font-style: normal;
    cursor: pointer;
    transition: border-color 80ms, background 80ms;
}
.threads-v5-empty-cta:hover:not(:disabled) {
    border-color: var(--accent, #4a7fc1);
    background: var(--bg, #0f0f0f);
}
.threads-v5-empty-cta:disabled {
    opacity: 0.6;
    cursor: wait;
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

/* Stage 4.10 — Later hover popup. 2026-05-03: rendered as a
 * horizontal row by default (was a vertical column) and positioned
 * above the anchor so the right-edge entries stay on-screen. The
 * `.threads-v5-later-popup-row` modifier flips the column layout
 * back to row + tightens padding for a more compact ribbon. */
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
.threads-v5-later-popup-row {
    flex-direction: row;
    flex-wrap: nowrap;
    min-width: 0;
    gap: 2px;
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
    white-space: nowrap;
}
.threads-v5-later-popup-row .threads-v5-later-option {
    text-align: center;
    padding: 6px 10px;
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

/* Wave C — wide modal for event-log inspector */
.threads-v5-modal-wide {
    max-width: 1100px;
    width: 90%;
}

.threads-v5-evlog-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}
.threads-v5-evlog-table th {
    text-align: left;
    padding: 8px 12px 8px 0;
    color: var(--text-muted, #888);
    font-weight: 500;
    border-bottom: 1px solid var(--border, #333);
}
.threads-v5-evlog-table td {
    padding: 6px 12px 6px 0;
    border-bottom: 1px solid rgba(60,60,60,0.4);
    color: var(--text, #ddd);
    vertical-align: top;
    word-break: break-word;
}
.threads-v5-evlog-table code {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    background: var(--bg-secondary, #1a1a1a);
    padding: 1px 5px;
    border-radius: 3px;
    color: var(--text-muted, #aaa);
}

/* Event log — expandable rows. User-feedback (2026-05-03): the
 * single-line summary truncated long inciting events and never
 * surfaced model/tier metadata that's useful for debugging. Rows
 * are now click-to-expand with a caret glyph and a detail row that
 * pretty-prints the full event payload. */
.threads-v5-evlog-hint {
    font-size: 12px;
    color: var(--text-muted, #888);
    margin: 0 0 10px 0;
}
.threads-v5-evlog-row {
    cursor: pointer;
}
.threads-v5-evlog-row:hover {
    background: rgba(255,255,255,0.03);
}
.threads-v5-evlog-toggle {
    width: 18px;
    color: var(--text-muted, #888);
    user-select: none;
}
.threads-v5-evlog-caret {
    display: inline-block;
    transition: transform 80ms ease;
}
.threads-v5-evlog-tier {
    font-size: 12px;
    color: var(--text-muted, #888);
}
.threads-v5-evlog-model {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: var(--text-muted, #aaa);
}
.threads-v5-evlog-summary {
    /* Allow long words but don't aggressively truncate — the detail
     * row carries the full picture so we don't need to clamp here. */
    max-width: 480px;
}
.threads-v5-evlog-detail-cell {
    background: var(--bg, #0a0a0a);
    padding: 0 0 6px 0 !important;
    border-bottom: 1px solid var(--border, #333) !important;
}
.threads-v5-evlog-payload {
    margin: 4px 12px 4px 36px;
    padding: 10px 12px;
    background: var(--bg-secondary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
    color: var(--text-muted, #aaa);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 360px;
    overflow: auto;
}

/* Wave C — keyboard navigation. The currently-focused row in the
   threads list gets a subtle highlight so j/k feel responsive. */
.threads-v5-toplist-card.threads-v5-kbd-focus {
    outline: 2px solid var(--accent, #4a7fc1);
    outline-offset: -1px;
}

.threads-v5-kbd-hint {
    margin: 8px 0 0 0;
    font-size: 11px;
    color: var(--text-muted, #666);
    text-align: right;
}
.threads-v5-kbd-hint kbd {
    background: var(--bg-secondary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 1px 5px;
    font-family: ui-monospace, monospace;
    font-size: 10px;
    color: var(--text, #ccc);
}

/* Wave E — toast confirmation feedback for commit actions */
#threads-v5-toast-host {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 8px;
    pointer-events: none;
}
.threads-v5-toast {
    pointer-events: auto;
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    min-width: 200px;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    opacity: 1;
    transition: opacity 350ms ease;
    border-left: 3px solid var(--accent, #4a7fc1);
}
.threads-v5-toast.threads-v5-toast-ok {
    border-left-color: #66cc66;
}
.threads-v5-toast.threads-v5-toast-error {
    border-left-color: #ff5555;
    color: #ffaaaa;
}
.threads-v5-toast.threads-v5-toast-fading {
    opacity: 0;
}

/* Wave E — risk indicator on top-level list cards.
   Mirrors the detail-view risk pill so the user can scan the
   list and see which threads have risky actions queued. */
.threads-v5-toplist-risk-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-left: 6px;
    vertical-align: middle;
}
.threads-v5-toplist-risk-dot.high { background: #ff5555; }
.threads-v5-toplist-risk-dot.medium { background: #ff9955; }
.threads-v5-toplist-risk-dot.low { background: #66cc66; }
"""
