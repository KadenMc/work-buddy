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
    threads: () => loadThreads(),
    today: () => loadToday(),
    tasks: () => loadTasks(),
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
//   p    — Projects selected project; uses the stable integer ``id`` so
//          the URL survives slug renames (e.g. ``aexp``→``agentic-experiments``)
//   ntf  — workflow view ID (paired with tab=ntf); maps to wv-<id> internally
//
// The legacy `#view/<id>` deep-link format is still handled by the existing
// hashchange route in core/workflows.py — `_initFromHash` stays out of its
// way so old links keep working.

function _persistHash() {
    if (window._wbHashInitInProgress) return;
    const params = new URLSearchParams();
    const active = document.querySelector('.tab-btn.active');
    let tab = active ? active.dataset.tab : 'today';
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
        } else if (tab === 'chats' && typeof chatsState !== 'undefined') {
            // Selected chat ID (existing).
            if (chatsState.selectedId) {
                const sid = chatsState.selectedId;
                const shortId = sid.slice(0, 8);
                const matches = (chatsState.chats || []).filter(c => c.short_id === shortId);
                params.set('ci', matches.length === 1 ? shortId : sid);
            }
            // Active search query — survives reloads + is shareable.
            // Strip the `(commit)` suffix that chatsCommitSearch adds
            // for display; the URL form is just the raw query string.
            if (chatsState.searchActive && chatsState.searchQuery) {
                const q = chatsState.searchQuery.replace(/\s*\(commit\)$/, '');
                if (q) params.set('q', q);
            }
            // Days window — only encode when non-default so the
            // typical view leaves the hash clean.
            const daysSel = document.getElementById('chats-days');
            const days = daysSel ? daysSel.value : '30';
            if (days && days !== '30') params.set('days', days);
        } else if (tab === 'tasks' && window._selectedNamespace) {
            params.set('tn', window._selectedNamespace);
        } else if (tab === 'projects'
                   && typeof _selectedProjectSlug !== 'undefined'
                   && _selectedProjectSlug) {
            // Projects use the stable integer ``id`` (not the slug) so the
            // URL survives slug renames. tabs/projects.py keeps
            // _projectsCache in sync; we look up the id from the cache.
            const cache = (typeof _projectsCache !== 'undefined') ? _projectsCache : [];
            const proj = cache.find(p => p.slug === _selectedProjectSlug);
            if (proj && proj.id != null) {
                params.set('p', String(proj.id));
            }
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
        } else if (tab === 'settings' && typeof WB_SETTINGS_SUBTAB !== 'undefined') {
            // Settings sub-tab: 'status' (control graph, default) or
            // 'activity' (bridge + logs). Only encode the non-default
            // value so the typical view leaves the hash clean.
            if (WB_SETTINGS_SUBTAB && WB_SETTINGS_SUBTAB !== 'status') {
                params.set('st', WB_SETTINGS_SUBTAB);
            }
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
        // No hash (or unknown hash) → default to the Today tab, then write
        // the canonical hash back so subsequent reloads have something to
        // honor (write #tab=today eagerly).
        switchTab('today');
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
        if (tab === 'status') {
            // There is no Status tab. The bridge chart, event log and
            // notification log live under Settings → Activity; route
            // bookmarked #tab=status links there.
            switchTab('settings');
            if (typeof switchSettingsSubtab === 'function') {
                switchSettingsSubtab('activity');
            }
        } else if (tab === 'ntf' && params.get('ntf')) {
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
                        switchTab('today');
                    }
                } catch (e) {
                    switchTab('today');
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

// ---- Header state (sidecar status + read-only flag) ----
// The #sidecar-status header indicator and the global _readOnly flag
// are tab-independent. The page shell owns refreshing them: once on
// init and again whenever the browser tab returns to the foreground.
async function refreshHeaderState() {
    const data = await fetchJSON('/api/state');
    if (!data) return;
    _readOnly = !!data.read_only;
    const el = document.getElementById('sidecar-status');
    if (el) {
        const roTag = _readOnly
            ? ' <span style="color:var(--warn);font-size:0.85em;opacity:0.8">(read-only)</span>'
            : '';
        const running = data.status === 'running';
        el.innerHTML = '<span class="status-dot ' + (running ? 'healthy' : 'stopped')
            + '"></span> sidecar ' + (running ? 'running' : 'stopped') + roTag;
    }
}

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
    refreshHeaderState();
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
// falls back to the Today tab when no hash is present.
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        _initFromHash();
        refreshHeaderState();
    });
} else {
    _initFromHash();
    refreshHeaderState();
}
// Pre-warm the Add-job picker's registry list so it's ready by the time
// the user clicks "Add job". The first call to /api/registry/list builds
// the dashboard process's registry (10-20s cold).
_loadJobRegistry();
"""


# ---------------------------------------------------------------------------
# Workflow views: polling + tab management
# ---------------------------------------------------------------------------
