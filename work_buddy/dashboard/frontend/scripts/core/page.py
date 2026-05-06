"""Dashboard page-shell JS — tab switching, URL-hash routing, init.

Owns the cross-tab coordination: the ``staticLoaders`` registry mapping
tab name to per-tab loader, the ``switchTab`` dispatcher,
``_persistHash`` / ``_initFromHash`` URL-hash routing, the header clock,
the ``visibilitychange`` listener that re-runs the active panel after a
backgrounded window returns to focus, and the init block that boots
the page (``_initFromHash`` plus the ``_loadJobRegistry`` pre-warm).

The ``// ---- Refresh model ----`` comment block documents WHY there is
no global panel-refresh timer (server-pushed events via
``core/event_bus`` drive surgical updates instead) and why each prior
attempt to add one was destructive.

Tab loaders themselves live in ``scripts/tabs/<name>.py`` — this file
just dispatches to them.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Tab switching ----
const staticLoaders = {
    overview: () => loadOverview(),
    threads: () => loadThreads(),
    today: () => loadToday(),
    tasks: () => loadTasks(),
    review: () => loadReview(),
    status: () => loadStatus(),
    jobs: () => loadJobs(),
    chats: () => loadChats(),
    contracts: () => loadContracts(),
    projects: () => loadProjects(),
    costs: () => loadCosts(),
    settings: () => loadSettings(),
};
// Expose globally so tabs/threads/main.py can register its loader at
// IIFE-execution time (loadThreads is defined later in the script
// concatenation order; this lets the threads module wire itself in
// without depending on core/page.py's exact placement).
window.staticLoaders = staticLoaders;

function switchTab(tabName) {
    // Update all tab buttons (static + dynamic)
    document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tabName);
        // Clear flash on the clicked tab
        if (b.dataset.tab === tabName) b.classList.remove('flash');
    });
    document.querySelectorAll('.tab-panel').forEach(p =>
        p.classList.toggle('active', p.id === 'panel-' + tabName)
    );

    // Lazy-load: static tabs or workflow view loader
    if (staticLoaders[tabName]) {
        staticLoaders[tabName]();
    } else if (tabName.startsWith('wv-') && typeof loadWorkflowView === 'function') {
        loadWorkflowView(tabName.replace('wv-', ''));
    }
    _persistHash();
}

document.querySelectorAll('.tab-btn').forEach(btn =>
    btn.addEventListener('click', () => switchTab(btn.dataset.tab))
);


// ---- URL hash state (Decision 2) ----
//
// Encode 7 high-leverage state keys in `window.location.hash` so that any
// real page reload (Cmd-R, Werkzeug --dev restart, browser tab restore) can
// rehydrate the UI to its last in-memory state. This is the persistence
// layer; the data-only auto-refresh below removes the routine destructive
// re-render that previously made these reloads visible.
//
// Keys (URLSearchParams-style, in the hash fragment only):
//   tab  — active tab id; 'ntf' is a synthetic value for workflow views
//   cp   — Costs project filter
//   cr   — Costs range pill (today/7/30/90/all)
//   ca   — Costs activity pill (only meaningful when project=work-buddy)
//   ci   — Chats selected session (short_id with collision fallback)
//   rs   — Review source-filter dropdown value
//   tn   — Tasks namespace drill-down
//   ntf  — workflow view ID (paired with tab=ntf); maps to wv-<id> internally
//
// The legacy `#view/<id>` deep-link format is still handled by the existing
// hashchange route in core/workflows.py — `_initFromHash` stays out of its
// way so old links keep working.

function _persistHash() {
    if (window._wbHashInitInProgress) return;
    const params = new URLSearchParams();
    const active = document.querySelector('.tab-btn.active');
    let tab = active ? active.dataset.tab : 'overview';
    if (tab.startsWith('wv-')) {
        params.set('tab', 'ntf');
        params.set('ntf', tab.slice(3));
    } else {
        params.set('tab', tab);
        if (tab === 'costs' && typeof costsState !== 'undefined') {
            if (costsState.project) params.set('cp', costsState.project);
            if (costsState.range) params.set('cr', costsState.range);
            // Only encode `ca` when the activity pill row is visible
            // (project=work-buddy) and the user is on a non-default pill.
            const isWB = (costsState.project || '').toLowerCase() === 'work-buddy';
            if (isWB && costsState.activity && costsState.activity !== 'all') {
                params.set('ca', costsState.activity);
            }
        } else if (tab === 'chats' && typeof chatsState !== 'undefined' && chatsState.selectedId) {
            const sid = chatsState.selectedId;
            const shortId = sid.slice(0, 8);
            const matches = (chatsState.chats || []).filter(c => c.short_id === shortId);
            params.set('ci', matches.length === 1 ? shortId : sid);
        } else if (tab === 'review') {
            const rsEl = document.getElementById('review-source-filter');
            const rs = rsEl && rsEl.value;
            if (rs) params.set('rs', rs);
        } else if (tab === 'tasks' && window._selectedNamespace) {
            params.set('tn', window._selectedNamespace);
        } else if (tab === 'threads' && typeof window._threadsState === 'object'
                   && window._threadsState) {
            // Threads tab state encoding:
            //   tpath=th-abc/th-def  — slash-separated thread path
            //   inspect=ci-7         — modal inspector (independent of tpath)
            const tpath = window._threadsState.path;
            if (Array.isArray(tpath) && tpath.length) {
                params.set('tpath', tpath.join('/'));
            }
            const insp = window._threadsState.inspect;
            if (insp) params.set('inspect', insp);
        }
    }
    history.replaceState(null, '', '#' + params.toString());
}

async function _initFromHash() {
    const hash = window.location.hash || '';
    // Legacy `#view/<id>` is owned by core/workflows.handleHashRoute.
    if (/^#view\//.test(hash)) return;

    const params = new URLSearchParams(hash.slice(1));
    if (!params.has('tab')) {
        // No hash (or unknown hash) → default to overview, then write the
        // canonical hash back so subsequent reloads have something to honor
        // (Decision Q3: write #tab=overview eagerly).
        switchTab('overview');
        return;
    }

    window._wbHashInitInProgress = true;
    try {
        // Apply restorable state synchronously *before* switchTab runs the
        // tab loader, so the loader picks up the right defaults.
        if (typeof costsState !== 'undefined') {
            if (params.has('cp')) costsState.project = params.get('cp') || '';
            if (params.has('cr')) costsState.range = params.get('cr');
            if (params.has('ca')) costsState.activity = params.get('ca');
        }
        if (params.has('tn')) {
            window._selectedNamespace = params.get('tn');
        }
        if (params.has('rs')) {
            const rsEl = document.getElementById('review-source-filter');
            if (rsEl) rsEl.value = params.get('rs');
        }
        // Threads tab state: stash before switchTab fires loadThreads
        // so the loader picks up the right path/inspect on first render.
        if (params.has('tpath') || params.has('inspect') || params.get('tab') === 'threads') {
            const tpath = params.get('tpath') || '';
            window._threadsState = {
                path: tpath ? tpath.split('/').filter(Boolean) : [],
                inspect: params.get('inspect') || null,
            };
        }
        // ci needs the chat list to exist before we can resolve short→full,
        // so loadChats() consumes it from window._urlState below.
        window._urlState = Object.fromEntries(params);

        let tab = params.get('tab');
        if (tab === 'ntf' && params.get('ntf')) {
            const viewId = params.get('ntf');
            const tabName = 'wv-' + viewId;
            if (document.querySelector('.tab-btn[data-tab="' + tabName + '"]')) {
                switchTab(tabName);
            } else {
                // Tab not yet created — fetch the view and create it. Mirrors
                // the legacy handleHashRoute() flow.
                try {
                    const resp = await fetch('/api/workflow-views/' + viewId);
                    const view = resp.ok ? await resp.json() : null;
                    if (view && view.status === 'active'
                        && typeof createWorkflowTab === 'function') {
                        createWorkflowTab(view);
                        switchTab(tabName);
                    } else {
                        switchTab('overview');
                    }
                } catch (e) {
                    switchTab('overview');
                }
            }
        } else {
            switchTab(tab);
        }
    } finally {
        window._wbHashInitInProgress = false;
        // Now that init has settled, persist the canonical hash.
        _persistHash();
    }
}


// ---- Clock ----
function updateClock() {
    const el = document.getElementById('clock');
    if (el) el.textContent = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}
setInterval(updateClock, 10000);
updateClock();


// ---- Global state ----
let _readOnly = false;

// ---- Refresh model ----
//
// The dashboard previously ran a 30s setInterval that called
// switchTab(activeTab), which re-ran the full loader and rewrote
// panel.innerHTML. That destroyed any in-flight UI state (filters,
// scroll, model-chip hover, drawer contents, ESPECIALLY focused
// textareas) and was the canonical "dashboard refresh bug." A second
// attempt (cd73918) tried to make the timer "data-only" via a
// dataRefreshers table that aliased back to load*() in most cases,
// re-introducing the same destructive rewrite for those tabs.
//
// Both are gone. The dashboard now updates from the server-pushed
// event bus (see core/event_bus.py + work_buddy/dashboard/events.py
// + the SSE endpoint /api/events). The smart-refresh policy in the
// bus dispatcher refreshes the active tab when an event affects it,
// AND defers when the user is typing in an input/textarea inside the
// panel (drained on focusout). Tab switches still refresh on switch
// (switchTab calls staticLoaders[tab]()), and the visibilitychange
// listener below refreshes the active tab when the browser tab
// returns to foreground after being hidden.

// ---- visibilitychange refresh ----
// When the browser tab becomes visible again after being backgrounded,
// re-run the active panel's loader once. Without this the SSE-only
// model would only update what changed *while the tab was watching*;
// long backgrounded periods leave the page stale even though the
// EventSource buffered events while hidden.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    const activeTab = document.querySelector('.tab-btn.active');
    if (!activeTab) return;
    const tab = activeTab.dataset.tab;
    if (tab.startsWith('wv-')) return;  // workflow-view tabs poll on their own
    const loader = staticLoaders[tab];
    if (loader) loader();
});

// ---- Init ----
// Set dynamic Obsidian vault links
if (WB_VAULT_NAME) {
    const mtl = document.getElementById('master-task-link');
    if (mtl) mtl.href = `obsidian://open?vault=${encodeURIComponent(WB_VAULT_NAME)}&file=tasks%2Fmaster-task-list.md`;
}
// _initFromHash decides which tab/state to load based on the URL hash;
// falls back to overview when no hash is present.
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initFromHash);
} else {
    _initFromHash();
}
// Pre-warm the Add-job picker's registry list so it's ready by the time
// the user clicks "Add job". The first call to /api/registry/list builds
// the dashboard process's registry (10-20s cold).
_loadJobRegistry();
"""


# ---------------------------------------------------------------------------
# Workflow views: polling + tab management
# ---------------------------------------------------------------------------
