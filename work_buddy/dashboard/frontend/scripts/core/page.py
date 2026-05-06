"""Dashboard main JS — tab switching and core tab loaders."""

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
// Expose globally so script_threads.py can register its loader at
// IIFE-execution time (loadThreads is defined later in the script
// concatenation order; this lets the threads module wire itself in
// without depending on script_main.py's exact placement).
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
// hashchange route in script_workflows.py — `_initFromHash` stays out of its
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
    // Legacy `#view/<id>` is owned by script_workflows.handleHashRoute.
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

// ---- Overview ----
async function loadOverview() {
    const data = await fetchJSON('/api/state');
    if (!data) return;
    _readOnly = !!data.read_only;

    const sidecarEl = document.getElementById('sidecar-status');
    const roTag = _readOnly ? ' <span style="color:var(--warn);font-size:0.85em;opacity:0.8">(read-only)</span>' : '';
    if (data.status === 'running') {
        sidecarEl.innerHTML = '<span class="status-dot healthy"></span> sidecar running' + roTag;
    } else {
        sidecarEl.innerHTML = '<span class="status-dot stopped"></span> sidecar stopped' + roTag;
    }

    const services = Object.values(data.services || {});
    const healthy = services.filter(s => s.status === 'healthy').length;

    document.getElementById('overview-cards').innerHTML = `
        <div class="card">
            <div class="card-label">Uptime</div>
            <div class="card-value">${formatUptime(data.uptime_seconds || 0)}</div>
        </div>
        <div class="card">
            <div class="card-label">Services</div>
            <div class="card-value">${healthy}<span class="unit"> / ${services.length} healthy</span></div>
        </div>
        <div class="card">
            <div class="card-label">Jobs</div>
            <div class="card-value">${(data.jobs || []).length}</div>
        </div>
        <div class="card">
            <div class="card-label">Last Tick</div>
            <div class="card-value small">${timeAgo(data.last_tick_at)}</div>
        </div>
    `;

}


// ---- Contracts ----
async function loadContracts() {
    const data = await fetchJSON('/api/contracts');
    if (!data) return;

    const contracts = data.contracts || [];
    if (contracts.length === 0) {
        document.getElementById('contracts-table').innerHTML = '<div class="empty-state">No active contracts</div>';
        return;
    }

    let rows = contracts.map(c => {
        const noteLink = c.vault_path
            ? `<a href="obsidian://open?vault=${encodeURIComponent(WB_VAULT_NAME)}&file=${encodeURIComponent(c.vault_path)}" title="Open contract in Obsidian" style="text-decoration:none;cursor:pointer;margin-left:6px;">&#x1F4D3;</a>`
            : '';
        return `
        <tr>
            <td><strong>${c.title}</strong>${noteLink}</td>
            <td>${statusBadge(c.status)}</td>
            <td>${c.type || '—'}</td>
            <td>${c.deadline || '—'}</td>
            <td>${c.priority || '—'}</td>
        </tr>
    `;
    }).join('');

    document.getElementById('contracts-table').innerHTML = `
        <table class="data-table">
            <thead><tr><th>Contract</th><th>Status</th><th>Type</th><th>Deadline</th><th>Priority</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}


// ---- Projects ----
let _projectsCache = [];
let _selectedProjectSlug = null;

async function loadProjects() {
    const data = await fetchJSON('/api/projects');
    if (!data) return;
    _projectsCache = data.projects || [];
    renderProjectList(_projectsCache);
}

function renderProjectList(projects) {
    const container = document.getElementById('projects-list');
    if (projects.length === 0) {
        container.innerHTML = '<div class="empty-state">No projects found</div>';
        return;
    }

    const groups = {active: [], inferred: [], paused: [], future: [], past: []};
    projects.forEach(p => {
        const g = groups[p.status] || groups['active'];
        g.push(p);
    });

    let html = '';
    for (const [status, items] of Object.entries(groups)) {
        if (items.length === 0) continue;
        html += '<div style="margin-bottom:16px;">';
        html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px; padding-left:4px;">' + status + ' (' + items.length + ')</div>';
        items.forEach(p => {
            html += '<div class="proj-card" data-slug="' + p.slug + '" style="padding:10px 12px; margin-bottom:4px; border-radius:6px; cursor:pointer; border:1px solid var(--border); background:var(--bg-secondary); transition:background 0.15s;">';
            html += '<div style="display:flex; justify-content:space-between; align-items:center;">';
            html += '<strong style="font-size:14px;">' + (p.name || p.slug) + '</strong>';
            html += statusBadge(p.status);
            html += '</div>';
            if (p.description) {
                html += '<div style="font-size:12px; color:var(--text-muted); margin-top:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + p.description + '</div>';
            }
            html += '</div>';
        });
        html += '</div>';
    }
    container.innerHTML = html;

    // Attach click handlers via event delegation (avoids inline onclick quoting issues)
    container.querySelectorAll('.proj-card').forEach(card => {
        card.addEventListener('click', () => selectProject(card.dataset.slug));
    });

    // Re-apply highlight after re-render so auto-refresh doesn't visually
    // unselect the user's active project (the right-hand detail pane is
    // never re-rendered by loadProjects, only this left list is).
    if (_selectedProjectSlug) {
        const activeCard = container.querySelector(
            '.proj-card[data-slug="' + _selectedProjectSlug + '"]');
        if (activeCard) {
            activeCard.style.background = 'var(--bg-tertiary)';
            activeCard.style.borderColor = 'var(--accent)';
        }
    }
}

async function selectProject(slug) {
    _selectedProjectSlug = slug;
    // Highlight selected card
    document.querySelectorAll('.proj-card').forEach(c => {
        c.style.background = c.dataset.slug === slug ? 'var(--bg-tertiary)' : 'var(--bg-secondary)';
        c.style.borderColor = c.dataset.slug === slug ? 'var(--accent)' : 'var(--border)';
    });

    const detail = document.getElementById('project-detail');
    detail.innerHTML = '<div class="loading">Loading project details...</div>';

    const data = await fetchJSON('/api/projects/' + slug);
    if (!data || data.error) {
        detail.innerHTML = '<div class="empty-state">' + (data?.error || 'Failed to load') + '</div>';
        return;
    }

    const statusOptions = ['active', 'paused', 'past', 'future', 'inferred'].map(s =>
        '<option value="' + s + '"' + (s === data.status ? ' selected' : '') + '>' + s + '</option>'
    ).join('');

    let html = '<div style="max-width:700px;">';

    // Header
    html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">';
    html += '<h2 style="margin:0; font-size:20px;">' + (data.name || data.slug) + '</h2>';
    html += statusBadge(data.status);
    html += '</div>';

    // Editable fields
    html += '<div style="margin-bottom:16px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Name</label>';
    html += '<input id="proj-name" type="text" value="' + (data.name || '').replace(/"/g, '&quot;') + '" style="width:100%; padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px;" />';
    html += '</div>';

    html += '<div style="margin-bottom:16px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Status</label>';
    html += '<select id="proj-status" style="padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px;">' + statusOptions + '</select>';
    html += '</div>';

    html += '<div style="margin-bottom:16px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Description</label>';
    html += '<textarea id="proj-desc" rows="3" style="width:100%; padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px; resize:vertical;">' + (data.description || '') + '</textarea>';
    html += '</div>';

    html += '<div style="margin-bottom:24px;">';
    html += '<button id="proj-save-btn" class="nb-btn nb-btn-approve" style="margin-right:8px;">Save Changes</button>';
    html += '<span id="proj-save-status" style="font-size:12px; color:var(--text-muted);"></span>';
    html += '</div>';

    // Memory section
    html += '<div style="border-top:1px solid var(--border); padding-top:16px; margin-top:16px;">';
    html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">Project Memory (Hindsight)</div>';
    if (data.memory) {
        html += '<div style="font-size:13px; line-height:1.6; color:var(--text-secondary); white-space:pre-wrap; max-height:300px; overflow-y:auto; background:var(--bg-tertiary); padding:12px; border-radius:6px;">' + escapeHtml(String(data.memory)) + '</div>';
    } else {
        html += '<div style="color:var(--text-muted); font-size:13px;">No project memories yet. Add observations below or use project_observe via MCP.</div>';
    }
    html += '</div>';

    // Add observation
    html += '<div style="margin-top:16px;">';
    html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">Add Observation</div>';
    html += '<textarea id="proj-obs" rows="3" placeholder="Record a decision, pivot, blocker, or insight about this project..." style="width:100%; padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px; resize:vertical;"></textarea>';
    html += '<button id="proj-obs-btn" class="nb-btn nb-btn-neutral" style="margin-top:8px;">Retain Observation</button>';
    html += '<span id="proj-obs-status" style="font-size:12px; color:var(--text-muted); margin-left:8px;"></span>';
    html += '</div>';

    // Observations log (loaded async)
    html += '<div style="border-top:1px solid var(--border); padding-top:16px; margin-top:24px;">';
    html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">Observations</div>';
    html += '<div id="proj-observations-log"><div class="loading">Loading observations...</div></div>';
    html += '</div>';

    // Metadata
    html += '<div style="margin-top:24px; padding-top:16px; border-top:1px solid var(--border); font-size:11px; color:var(--text-muted);">';
    html += 'Created: ' + (data.created_at || '—').slice(0, 10) + ' &middot; Updated: ' + (data.updated_at || '—').slice(0, 10) + ' &middot; Slug: <code>' + data.slug + '</code>';
    html += '</div>';

    html += '</div>';
    detail.innerHTML = html;

    // Attach handlers (avoids inline onclick quoting issues in Python string templates)
    document.getElementById('proj-save-btn').addEventListener('click', () => saveProject(slug));
    document.getElementById('proj-obs-btn').addEventListener('click', () => addObservation(slug));

    // Load observations log asynchronously
    loadProjectObservations(slug);
}

async function loadProjectObservations(slug) {
    const container = document.getElementById('proj-observations-log');
    if (!container) return;

    const data = await fetchJSON('/api/projects/' + slug + '/memories?limit=30');
    if (!data || !data.memories) {
        container.innerHTML = '<div class="empty-state">Could not load observations</div>';
        return;
    }

    const memories = data.memories;
    if (memories.length === 0) {
        container.innerHTML = '<div class="empty-state" style="padding:12px 0;">No observations yet</div>';
        return;
    }

    const logHtml = memories.map(m => {
        const dt = m.date ? new Date(m.date) : null;
        const time = dt ? dt.toLocaleDateString([], {month:'short', day:'numeric'}) + ' ' + dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
        const ft = m.fact_type || 'memory';
        const ftClass = ft === 'observation' ? 'warn' : ft === 'world' ? 'info' : 'info';
        const source = (m.tags || []).filter(t => t.startsWith('source:')).map(t => t.slice(7)).join(', ') || '';
        return '<div class="log-entry ' + ftClass + '">' +
            '<span class="log-ts">' + time + '</span>' +
            '<span class="log-kind">' + ft + (source ? ' (' + source + ')' : '') + '</span>' +
            '<span class="log-msg">' + escapeHtml(m.text) + '</span>' +
        '</div>';
    }).join('');

    container.innerHTML = '<div class="log-container">' + logHtml + '</div>';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function saveProject(slug) {
    const name = document.getElementById('proj-name').value.trim();
    const status = document.getElementById('proj-status').value;
    const description = document.getElementById('proj-desc').value.trim();
    const statusEl = document.getElementById('proj-save-status');

    statusEl.textContent = 'Saving...';
    statusEl.style.color = 'var(--text-muted)';

    try {
        const resp = await fetch('/api/projects/' + slug, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, status, description}),
        });
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = 'Error: ' + data.error;
            statusEl.style.color = 'var(--red)';
        } else {
            statusEl.textContent = 'Saved!';
            statusEl.style.color = 'var(--green)';
            setTimeout(() => statusEl.textContent = '', 2000);
            // Refresh the list to show updated name/status
            loadProjects();
        }
    } catch (e) {
        statusEl.textContent = 'Failed to save';
        statusEl.style.color = 'var(--red)';
    }
}

async function addObservation(slug) {
    const textarea = document.getElementById('proj-obs');
    const content = textarea.value.trim();
    if (!content) return;

    const statusEl = document.getElementById('proj-obs-status');
    statusEl.textContent = 'Retaining...';
    statusEl.style.color = 'var(--text-muted)';

    try {
        const resp = await fetch('/api/projects/' + slug + '/observe', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content}),
        });
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = 'Error: ' + data.error;
            statusEl.style.color = 'var(--red)';
        } else {
            statusEl.textContent = 'Retained!';
            statusEl.style.color = 'var(--green)';
            textarea.value = '';
            setTimeout(() => statusEl.textContent = '', 2000);
            // Refresh detail to show new memory
            selectProject(slug);
        }
    } catch (e) {
        statusEl.textContent = 'Failed to retain';
        statusEl.style.color = 'var(--red)';
    }
}


// ---- Log actions ----
function copyLog() {
    const events = window._logEvents || [];
    if (!events.length) return;
    const text = events.map(e => {
        const dt = new Date(e.ts * 1000);
        const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const kind = (e.kind || '').replace(/_/g, ' ').padEnd(16);
        const level = (e.level || 'info').toUpperCase().padEnd(5);
        return `${time}  ${level}  ${kind}  ${e.source}: ${e.summary}`;
    }).join('\\n');
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.querySelector('.log-toolbar-btn');
        if (btn) { btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy Log', 1500); }
    });
}

async function investigateEvent(idx) {
    if (_readOnly) return;
    const e = (window._logEvents || [])[idx];
    if (!e) return;

    const btn = event.target;
    btn.textContent = 'Launching...';
    btn.disabled = true;

    try {
        const r = await fetch('/api/investigate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({event: e}),
        });
        const data = await r.json();
        if (data.success) {
            btn.textContent = 'Launched';
            btn.style.background = 'var(--green-subtle)';
            btn.style.borderColor = 'var(--green)';
            btn.style.color = 'var(--green)';
        } else {
            btn.textContent = data.error || 'Failed';
            btn.disabled = false;
        }
    } catch (err) {
        btn.textContent = 'Error';
        btn.disabled = false;
        console.error('Investigate failed:', err);
    }
}


async function launchSetupAgent(componentId, mode, btn) {
    if (_readOnly) return;
    const origText = btn.textContent;
    btn.textContent = 'Launching...';
    btn.disabled = true;

    const prompt = '/wb-setup diagnose ' + componentId;

    try {
        const r = await fetch('/api/launch-agent', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                prompt: prompt,
                mode: mode,
                context: {source: 'setup_wizard', component_id: componentId}
            }),
        });
        const data = await r.json();
        if (data.success) {
            btn.textContent = 'Launched \u2713';
            btn.style.background = 'var(--green-subtle)';
            btn.style.borderColor = 'var(--green)';
            btn.style.color = 'var(--green)';
        } else {
            btn.textContent = data.error || 'Failed';
            btn.disabled = false;
        }
    } catch (err) {
        btn.textContent = 'Error';
        btn.disabled = false;
        console.error('Setup agent launch failed:', err);
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
// event bus (see script_event_bus.py + work_buddy/dashboard/events.py
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
