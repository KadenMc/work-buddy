"""Dashboard main JS — tab switching and core tab loaders."""

from __future__ import annotations


def _script() -> str:
    return r"""
// ---- Tab switching ----
const staticLoaders = {
    overview: () => loadOverview(),
    tasks: () => loadTasks(),
    status: () => loadStatus(),
    chats: () => loadChats(),
    contracts: () => loadContracts(),
    projects: () => loadProjects(),
};

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
}

document.querySelectorAll('.tab-btn').forEach(btn =>
    btn.addEventListener('click', () => switchTab(btn.dataset.tab))
);


// ---- Clock ----
function updateClock() {
    const el = document.getElementById('clock');
    if (el) el.textContent = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}
setInterval(updateClock, 10000);
updateClock();


// ---- Helpers ----
function statusBadge(status, tooltip) {
    const map = {
        healthy: 'badge-green', running: 'badge-green', active: 'badge-green', ok: 'badge-green',
        unhealthy: 'badge-yellow', stalled: 'badge-yellow', waiting: 'badge-yellow', degraded: 'badge-yellow',
        crashed: 'badge-red', blocked: 'badge-red', error: 'badge-red',
        stopped: 'badge-muted', done: 'badge-muted', unknown: 'badge-muted', disabled: 'badge-muted',
        focused: 'badge-blue', next: 'badge-blue',
        inbox: 'badge-purple', someday: 'badge-muted',
        consent_required: 'badge-yellow',
    };
    const tip = tooltip ? ` title="${escapeHtml(tooltip)}" style="cursor:help"` : '';
    return `<span class="badge ${map[status] || 'badge-muted'}"${tip}>${status}</span>`;
}

function _healthDotClass(status) {
    if (status === 'healthy') return 'healthy';
    if (status === 'degraded' || status === 'unhealthy') return 'unhealthy';
    if (status === 'crashed') return 'crashed';
    return 'stopped';
}

function _healthChips(c) {
    const d = c.details || {};
    let chips = '';
    if (d.probe_ms) chips += `<span class="health-chip">${d.probe_ms}ms</span>`;
    if (d.sidecar_pid) chips += `<span class="health-chip">PID ${d.sidecar_pid}</span>`;
    if (d.crash_count > 0) chips += `<span class="health-chip warn">${d.crash_count} crashes</span>`;
    if (d.blocked_by) chips += `<span class="health-chip warn">blocked by ${d.blocked_by}</span>`;
    if (d.probe_reason && c.status !== 'healthy') chips += `<span class="health-chip reason">${d.probe_reason}</span>`;
    return chips;
}

function _reprobeBtn(c) {
    return `<button class="health-reprobe-btn" onclick="event.stopPropagation(); reprobeComponent('${c.id}', this)" title="Refresh status">\u21BB</button>`;
}

function _diagBtn(c) {
    if (c.status === 'healthy' || c.status === 'disabled') return '';
    return `<button class="health-diagnose-btn" onclick="event.stopPropagation(); diagnoseComponent('${c.id}', this)" title="Run diagnostics">Diagnose</button>`;
}

async function reprobeComponent(componentId, btnEl) {
    btnEl.classList.add('spinning');
    try {
        const resp = await fetch('/api/reprobe/' + componentId, { method: 'POST' });
        const data = await resp.json();
        btnEl.classList.remove('spinning');

        if (!data.health) return;
        const h = data.health;

        // Update the row in-place — works for top-level items and sub-rows
        const svcEl = document.getElementById('status-services');
        const el = svcEl && _findComponentEl(svcEl, componentId);
        if (!el) return;

        // For top-level .health-item, target the first .health-row child; for sub-rows, target el itself
        const row = el.classList.contains('health-item') ? el.querySelector('.health-row') : el;
        if (!row) return;

        // Update status dot
        const dot = row.querySelector('.status-dot');
        if (dot) dot.className = 'status-dot ' + _healthDotClass(h.status);

        // Update badge
        const badge = row.querySelector('.badge');
        if (badge) {
            badge.className = 'badge ' + (statusBadge(h.status).match(/badge-\\w+/)?.[0] || 'badge-muted');
            badge.textContent = h.status;
        }

        // Update chips
        const detailRow = row.querySelector('.health-row-detail');
        const newChips = _healthChips(h);
        if (detailRow) {
            detailRow.innerHTML = newChips;
        } else if (newChips) {
            row.insertAdjacentHTML('beforeend', '<div class="health-row-detail">' + newChips + '</div>');
        }

        // Show/hide diagnose button based on new status
        const existingDiag = row.querySelector('.health-diagnose-btn');
        if (h.status === 'healthy' || h.status === 'disabled') {
            if (existingDiag) existingDiag.remove();
        } else if (!existingDiag) {
            const reprobe = row.querySelector('.health-reprobe-btn');
            if (reprobe) reprobe.insertAdjacentHTML('afterend', _diagBtn(h));
        }

        // Flash the row briefly to signal the update
        row.style.transition = 'background 0.3s';
        row.style.background = h.status === 'healthy' ? '#23863626' : '#f851491a';
        setTimeout(() => { row.style.background = ''; }, 600);
    } catch (err) {
        btnEl.classList.remove('spinning');
        console.error('Reprobe failed:', err);
    }
}

// Track which component has an open diagnostic panel (prevents auto-refresh clobbering)
let _diagOpen = null;

function _renderDiagPanel(diag) {
    let html = '<div class="health-diag">';
    if (diag.steps_run && diag.steps_run.length > 0) {
        html += '<div class="health-diag-steps">';
        for (const step of diag.steps_run) {
            const icon = step.ok ? '\u2713' : '\u2717';
            const cls = step.ok ? '' : ' fail';
            html += '<div class="health-diag-step' + cls + '">'
                + '<span class="step-icon">' + icon + '</span>'
                + '<span class="step-desc">' + escapeHtml(step.description) + '</span>'
                + '<span class="step-detail">' + escapeHtml(step.detail) + '</span>'
                + '</div>';
        }
        html += '</div>';
    }
    if (diag.root_cause) {
        html += '<div class="health-diag-cause">'
            + '<div class="cause-label">Root cause</div>'
            + '<div class="cause-text">' + escapeHtml(diag.root_cause) + '</div>'
            + '</div>';
    }
    if (diag.fix_suggestion) {
        html += '<div class="health-diag-fix">'
            + '<div class="fix-label">How to fix</div>'
            + '<pre class="fix-text">' + escapeHtml(diag.fix_suggestion) + '</pre>'
            + '</div>';
    }
    // Wizard hint — always shown when there's a failure
    if (diag.status === 'failed' && diag.component_id) {
        html += '<div class="health-diag-wizard">'
            + '\ud83e\ude84 Run <code>/wb-setup ' + escapeHtml(diag.component_id) + '</code> in Claude Code for interactive diagnostics'
            + '</div>';
    }
    if (diag.status === 'passed') {
        html += '<div style="color: var(--text-muted); padding: 4px 0;">All checks passed.</div>';
    }
    html += '</div>';
    return html;
}

function toggleHealthItem(rowEl) {
    const item = rowEl.parentElement;
    item.classList.toggle('collapsed');
    // If collapsing and a diag panel is open inside, clean it up
    if (item.classList.contains('collapsed') && _diagOpen) {
        const diag = item.querySelector('.health-diag');
        if (diag) { diag.remove(); _diagOpen = null; }
    }
}

function _findComponentEl(svcEl, componentId) {
    // Try top-level .health-item[data-component] first, then any row with data-component
    return svcEl.querySelector(`.health-item[data-component="${componentId}"]`)
        || svcEl.querySelector(`[data-component="${componentId}"]`);
}

async function diagnoseComponent(componentId, btnEl) {
    const svcEl = document.getElementById('status-services');

    // Toggle: if already open for this component, close it
    if (_diagOpen === componentId) {
        const existing = svcEl && svcEl.querySelector('.health-diag');
        if (existing) existing.remove();
        _diagOpen = null;
        return;
    }

    // Remove any previously open diag panel
    const prev = svcEl && svcEl.querySelector('.health-diag');
    if (prev) prev.remove();

    _diagOpen = componentId;

    // Set spinner — find by component to be resilient to re-renders
    const startEl = _findComponentEl(svcEl, componentId);
    const startBtn = startEl && startEl.querySelector('.health-diagnose-btn');
    if (startBtn) { startBtn.classList.add('loading'); startBtn.innerHTML = '<span class="diag-spinner"></span>'; }

    try {
        const resp = await fetch('/api/diagnose/' + componentId);
        const diag = await resp.json();

        // Re-find after async gap (DOM may have changed)
        const el = _findComponentEl(svcEl, componentId);
        if (!el) { _diagOpen = null; return; }

        // Restore button — clear spinner
        const btn = el.querySelector('.health-diagnose-btn');
        if (btn) { btn.classList.remove('loading'); btn.textContent = 'Diagnose'; }

        // For top-level .health-item: insert after first .health-row child
        // For sub-row .health-row: insert directly after the row itself
        if (el.classList.contains('health-item')) {
            const row = el.querySelector('.health-row');
            if (row) {
                row.insertAdjacentHTML('afterend', _renderDiagPanel(diag));
            } else {
                el.insertAdjacentHTML('beforeend', _renderDiagPanel(diag));
            }
        } else {
            el.insertAdjacentHTML('afterend', _renderDiagPanel(diag));
        }
    } catch (err) {
        _diagOpen = null;
        // Re-find and restore on error too
        const el = _findComponentEl(svcEl, componentId);
        const btn = el && el.querySelector('.health-diagnose-btn');
        if (btn) { btn.classList.remove('loading'); btn.textContent = 'Diagnose'; }
        console.error('Diagnostics failed:', err);
    }
}

function _healthActions(c, extra) {
    return `<span class="health-actions">${_diagBtn(c)}${extra || ''}${_reprobeBtn(c)}</span>`;
}

function _healthRow(c, indent) {
    const chips = _healthChips(c);
    const cls = indent ? 'health-row sub' : 'health-row';
    const issueCls = c.status === 'healthy' ? '' : ' issue';
    return `
        <div class="${cls}${issueCls}" data-component="${c.id}">
            <div class="health-row-main">
                <span class="status-dot ${_healthDotClass(c.status)}"></span>
                <span class="health-name">${c.display_name}</span>
                ${statusBadge(c.status)}
                ${_healthActions(c)}
            </div>
            ${chips ? `<div class="health-row-detail">${chips}</div>` : ''}
        </div>
    `;
}

function renderHealthTree(health, requirements) {
    const comps = health.components || [];
    const summary = health.summary || {};
    const byId = {};
    comps.forEach(c => { byId[c.id] = c; });

    // Separate opted-out components from active ones
    const activeComps = comps.filter(c => c.wanted !== false);
    const optedOut = comps.filter(c => c.wanted === false);

    // Summary bar — count only active components
    const activeTotal = activeComps.length;
    const activeHealthy = activeComps.filter(c => c.status === 'healthy').length;
    const pctHealthy = activeTotal > 0 ? Math.round((activeHealthy / activeTotal) * 100) : 0;
    let html = `
        <div class="health-summary">
            <div class="health-bar">
                <div class="health-bar-fill" style="width: ${pctHealthy}%"></div>
            </div>
            <div class="health-counts">
                <span class="health-count healthy">${activeHealthy} healthy</span>
                <span class="health-count unhealthy">${activeTotal - activeHealthy} issue${(activeTotal - activeHealthy) !== 1 ? 's' : ''}</span>
                ${optedOut.length ? `<span class="health-count disabled">${optedOut.length} opted out</span>` : ''}
            </div>
        </div>
    `;

    // Requirements warning banner
    if (requirements && requirements.all && !requirements.all.all_required_pass) {
        const reqFails = requirements.all.failed_required || 0;
        html += `
            <div class="health-req-warn" style="margin:0.5rem 0;padding:0.5rem 0.75rem;background:var(--surface-2);border-left:3px solid var(--yellow);border-radius:4px;font-size:0.85rem;">
                <strong>\u26a0\ufe0f ${reqFails} requirement${reqFails !== 1 ? 's' : ''} failing</strong>
                <span style="opacity:0.7;margin-left:0.5rem;">Run <code>/wb-setup</code> to diagnose</span>
            </div>
        `;
    }

    // Tree layout rules:
    //   - "external" components (e.g. postgresql) are NEVER top-level —
    //     they only appear nested under components that depend on them.
    //   - "plugin" components are NEVER top-level — they nest under their
    //     parent integration (e.g. smart_connections under obsidian).
    //   - Everything else (integration, service) is top-level.
    //   - Sub-items = depends_on (dependencies shown beneath, e.g. postgresql
    //     under hindsight) + dependents with plugin/external category
    //     (e.g. plugins under obsidian).
    //   - Components with wanted=false are shown in a collapsed "Opted out"
    //     section at the bottom, not mixed into the main tree.

    // Build reverse map: parent_id -> [components that depend on it]
    const dependents = {};
    activeComps.forEach(c => {
        (c.depends_on || []).forEach(depId => {
            if (!dependents[depId]) dependents[depId] = [];
            dependents[depId].push(c.id);
        });
    });

    // Sub-items: depends_on (deps shown under me) + my dependents that are plugins
    const subItemMap = {};
    activeComps.forEach(c => {
        const subs = [];
        // My dependencies nest under me (e.g. postgresql under hindsight)
        (c.depends_on || []).forEach(depId => {
            if (byId[depId] && byId[depId].wanted !== false) subs.push(depId);
        });
        // Components that depend on me AND are plugins/external nest under me
        (dependents[c.id] || []).forEach(depId => {
            const dep = byId[depId];
            if (dep && (dep.category === 'plugin' || dep.category === 'external') && !subs.includes(depId)) {
                subs.push(depId);
            }
        });
        subItemMap[c.id] = subs;
    });

    // Top-level = not external, not plugin, and not opted out
    const topLevel = activeComps.filter(c => c.category !== 'external' && c.category !== 'plugin');

    // Render each top-level component as a collapsible item
    topLevel.forEach(c => {
        const subIds = subItemMap[c.id] || [];

        const hasSubs = subIds.length > 0;
        const chips = _healthChips(c);
        const issueCls = c.status === 'healthy' ? '' : ' issue';

        if (!hasSubs) {
            // Simple row, no expand
            html += `
                <div class="health-item" data-component="${c.id}">
                    <div class="health-row${issueCls}">
                        <div class="health-row-main">
                            <span class="status-dot ${_healthDotClass(c.status)}"></span>
                            <span class="health-name">${c.display_name}</span>
                            ${statusBadge(c.status)}
                            ${_healthActions(c)}
                        </div>
                        ${chips ? `<div class="health-row-detail">${chips}</div>` : ''}
                    </div>
                </div>
            `;
        } else {
            // Collapsible with sub-items — collapsed by default
            const subHtml = subIds.map(sid => _healthRow(byId[sid], true)).join('');
            // Aggregate sub-status for the count badge
            const subIssues = subIds.filter(sid => byId[sid].status !== 'healthy' && byId[sid].status !== 'disabled').length;
            const countBadge = subIssues > 0
                ? `<span class="health-sub-count warn">${subIssues} issue${subIssues !== 1 ? 's' : ''}</span>`
                : `<span class="health-sub-count">${subIds.length}</span>`;

            html += `
                <div class="health-item collapsed" data-component="${c.id}">
                    <div class="health-row${issueCls}" onclick="toggleHealthItem(this)">
                        <div class="health-row-main">
                            <span class="health-chevron">\u25BE</span>
                            <span class="status-dot ${_healthDotClass(c.status)}"></span>
                            <span class="health-name">${c.display_name}</span>
                            ${statusBadge(c.status)}
                            ${_healthActions(c, countBadge)}
                        </div>
                        ${chips ? `<div class="health-row-detail">${chips}</div>` : ''}
                    </div>
                    <div class="health-sub">${subHtml}</div>
                </div>
            `;
        }
    });

    // Opted-out section (collapsed by default)
    if (optedOut.length > 0) {
        const optedNames = optedOut.map(c => c.display_name).join(', ');
        html += `
            <div class="health-item collapsed" data-component="_opted_out">
                <div class="health-row" onclick="toggleHealthItem(this)" style="opacity:0.6;">
                    <div class="health-row-main">
                        <span class="health-chevron">\u25BE</span>
                        <span class="status-dot stopped"></span>
                        <span class="health-name">Opted out</span>
                        <span class="health-sub-count">${optedOut.length}</span>
                    </div>
                </div>
                <div class="health-sub">
                    ${optedOut.map(c => `
                        <div class="health-row" style="opacity:0.5;">
                            <div class="health-row-main" style="padding-left:2rem;">
                                <span class="status-dot stopped"></span>
                                <span class="health-name">${c.display_name}</span>
                                <span class="badge badge-muted">opted out</span>
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    return html;
}

function formatUptime(seconds) {
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h + 'h ' + m + 'm';
}

function timeAgo(epoch) {
    if (!epoch) return '\u2014';
    const diff = Math.floor(Date.now() / 1000 - epoch);
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
}

function formatTimestamp(iso) {
    if (!iso) return '\u2014';
    try {
        const d = new Date(iso);
        const now = new Date();
        const time = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        const sameDay = d.toDateString() === now.toDateString();
        if (sameDay) return 'Today ' + time;
        const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
        if (d.toDateString() === yesterday.toDateString()) return 'Yesterday ' + time;
        const month = d.toLocaleString('default', {month: 'short'});
        return month + ' ' + d.getDate() + ' ' + time;
    } catch (e) { return iso; }
}

function timeUntil(epoch) {
    if (!epoch) return '\u2014';
    const diff = Math.floor(epoch - Date.now() / 1000);
    if (diff < 0) return 'overdue';
    if (diff < 60) return 'now';
    if (diff < 3600) return 'in ' + Math.floor(diff / 60) + 'm';
    if (diff < 86400) return 'in ' + Math.floor(diff / 3600) + 'h';
    return 'in ' + Math.floor(diff / 86400) + 'd';
}

async function fetchJSON(url) {
    try {
        const r = await fetch(url);
        return await r.json();
    } catch (e) {
        console.error('Fetch failed:', url, e);
        return null;
    }
}


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

    const jobs = data.jobs || [];
    if (jobs.length === 0) {
        document.getElementById('overview-jobs').innerHTML = '<div class="empty-state">No scheduled jobs</div>';
        return;
    }

    let rows = jobs.map(j => `
        <tr>
            <td>${j.name}</td>
            <td title="${j.schedule}">${j.schedule_desc || j.schedule}</td>
            <td>${j.last_result ? statusBadge(j.last_result, j.last_error) : '\u2014'}</td>
            <td>${timeAgo(j.last_run_at)}</td>
            <td>${timeUntil(j.next_at)}</td>
        </tr>
    `).join('');

    document.getElementById('overview-jobs').innerHTML = `
        <table class="data-table">
            <thead><tr><th>Job</th><th>Schedule</th><th>Last Result</th><th>Last Run</th><th>Next Run</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;

}


// ---- Tasks ----
function renderTaskTable(tasks) {
    const el = document.getElementById('task-list');
    if (tasks.length === 0) {
        el.innerHTML = '<div class="empty-state">No matching tasks</div>';
        return;
    }
    const rows = tasks.map(t => {
        const noteCell = t.note_id
            ? `<a href="obsidian://open?vault=${encodeURIComponent(WB_VAULT_NAME)}&file=${encodeURIComponent(t.note_id)}" title="Open note in Obsidian" style="text-decoration:none;cursor:pointer">&#x1F4D3;</a>`
            : '\u2014';
        const markers = (t.markers || []).map(m =>
            `<span title="${m.label}${m.date ? ' ' + m.date : ''}" style="cursor:help">${m.emoji}</span>`
        ).join(' ') || '\u2014';
        return `<tr>
            <td>${statusBadge(t.state)}</td>
            <td>${t.text}</td>
            <td>${t.urgency !== 'none' ? statusBadge(t.urgency) : '\u2014'}</td>
            <td style="white-space:nowrap">${markers}</td>
            <td style="text-align:center">${noteCell}</td>
            <td><code>${t.id || '\u2014'}</code></td>
        </tr>`;
    }).join('');
    el.innerHTML = `
        <div class="task-list-scroll">
        <table class="data-table">
            <thead><tr><th>State</th><th>Task</th><th>Urgency</th><th>Markers</th><th>Note</th><th>ID</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        </div>
    `;
}

async function loadTasks() {
    const data = await fetchJSON('/api/tasks');
    if (!data) return;

    const counts = data.counts || {};
    const countCards = Object.entries(counts).map(([state, n]) => `
        <div class="card">
            <div class="card-label">${state}</div>
            <div class="card-value">${n}</div>
        </div>
    `).join('');
    document.getElementById('task-counts').innerHTML = countCards || '<div class="empty-state">No tasks</div>';

    const tasks = (data.tasks || []).filter(t => !t.done);
    if (tasks.length === 0) {
        document.getElementById('task-list').innerHTML = '<div class="empty-state">No open tasks</div>';
        return;
    }

    window._allTasks = tasks;
    renderTaskTable(tasks);

    const searchInput = document.getElementById('task-search');
    if (searchInput && !searchInput._bound) {
        searchInput._bound = true;
        searchInput.addEventListener('input', () => {
            const q = searchInput.value.toLowerCase().trim();
            const filtered = q
                ? window._allTasks.filter(t =>
                    (t.text || '').toLowerCase().includes(q) ||
                    (t.id || '').toLowerCase().includes(q) ||
                    (t.state || '').toLowerCase().includes(q) ||
                    (t.urgency || '').toLowerCase().includes(q)
                )
                : window._allTasks;
            renderTaskTable(filtered);
        });
    }
}


// ---- Status (services + event log) ----
async function loadStatus() {
    const data = await fetchJSON('/api/state');
    if (!data) return;

    // --- Obsidian bridge ---
    const b = data.bridge || {};
    const bridgeEl = document.getElementById('status-bridge');
    if (b.status) {
        const dotClass = b.status === 'healthy' ? 'healthy' : (b.status === 'timeout' ? 'unhealthy' : 'crashed');
        const statusLabel = b.status === 'healthy' ? 'connected' : b.status;
        const latencyColor = (b.latency_ms || 0) > 2000 ? 'var(--red)' : (b.latency_ms || 0) > 500 ? 'var(--yellow)' : 'var(--text-primary)';

        // Log-scale sparkline, normalized so tallest bar fills 100%
        const hist = b.history || [];
        const logMax = Math.max(1, ...hist.map(h => Math.log10(Math.max(1, h.ms))));
        const bars = hist.map(h => {
            const logMs = Math.log10(Math.max(1, h.ms));
            const pct = Math.max(8, (logMs / logMax) * 100);
            const cls = !h.ok ? 'bar-fail' : h.ms > 500 ? 'bar-slow' : 'bar-ok';
            const dt = new Date(h.ts * 1000);
            const tip = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) + ' \u2014 ' + h.ms + 'ms';
            return `<div class="bar ${cls}" style="height:${pct}%" title="${tip}"></div>`;
        }).join('');

        bridgeEl.innerHTML = `
            <div class="bridge-card">
                <div class="bridge-header">
                    <h3><span class="status-dot ${dotClass}"></span> Obsidian Bridge \u2014 ${statusLabel}</h3>
                    <div class="bridge-stats">
                        <div>Latency <span class="bridge-stat-value" style="color:${latencyColor}">${b.latency_ms}ms</span></div>
                        <div>Trend <span class="bridge-stat-value">${b.ema_ms || 0}ms</span>${(() => {
                            const tc = (b.ema_ms||0) > 2000 ? 'var(--red)' : (b.ema_ms||0) > 500 ? 'var(--yellow)' : 'var(--text-primary)';
                            const arrow = b.trend === 'up' ? '\u25B2' : b.trend === 'down' ? '\u25BC' : '\u25C6';
                            const tip = b.trend === 'up' ? 'Latency increasing' : b.trend === 'down' ? 'Latency decreasing' : 'Latency stable';
                            return ` <span style="color:${tc}" title="${tip}">${arrow}</span>`;
                        })()}</div>
                        <div>Peak <span class="bridge-stat-value">${b.max_ms || 0}ms</span></div>
                    </div>
                    <div class="bridge-meta">${b.vault || ''} ${b.plugin_version ? 'v' + b.plugin_version : ''}</div>
                </div>
                ${bars ? `<div class="bridge-sparkline">${bars}</div>` : ''}
            </div>
        `;
    } else {
        bridgeEl.innerHTML = '<div class="empty-state">Bridge status unavailable</div>';
    }

    // --- Component health tree ---
    const health = data.health;
    const svcEl = document.getElementById('status-services');
    if (!health || !health.components || health.components.length === 0) {
        // Fallback to flat services table if health not available
        const services = Object.entries(data.services || {});
        if (services.length === 0) {
            svcEl.innerHTML = '<div class="empty-state">No services</div>';
        } else {
            let rows = services.map(([name, s]) => `
                <tr>
                    <td><strong>${name}</strong></td>
                    <td>${statusBadge(s.status)}</td>
                    <td>${s.port}</td>
                    <td>${s.pid || '\u2014'}</td>
                    <td>${s.crash_count || 0}</td>
                    <td>${timeAgo(s.last_check)}</td>
                </tr>
            `).join('');
            svcEl.innerHTML = `
                <table class="data-table">
                    <thead><tr><th>Service</th><th>Status</th><th>Port</th><th>PID</th><th>Crashes</th><th>Last Check</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            `;
        }
    } else {
        // Skip health tree re-render if a diagnostic panel is open —
        // re-rendering would destroy the panel and the user's context.
        if (_diagOpen) {
            // Just update the summary bar and badge statuses in-place
            // (full re-render deferred until diag panel is closed)
        } else {
            // Preserve expand/collapse state across re-renders
            const expanded = new Set();
            svcEl.querySelectorAll('.health-item:not(.collapsed)').forEach(el => {
                const id = el.dataset.component;
                if (id) expanded.add(id);
            });
            svcEl.innerHTML = renderHealthTree(health, data.requirements);
            // Restore: items start collapsed by default, expand the ones that were open
            if (expanded.size > 0) {
                svcEl.querySelectorAll('.health-item.collapsed').forEach(el => {
                    if (expanded.has(el.dataset.component)) {
                        el.classList.remove('collapsed');
                    }
                });
            }
        }
    }

    // --- Event log ---
    const events = (data.events || []).slice().reverse();  // newest first
    if (events.length === 0) {
        document.getElementById('status-log').innerHTML = '<div class="empty-state">No events yet</div>';
        return;
    }

    // Store for copy
    window._logEvents = events;

    const logHtml = events.map((e, i) => {
        const dt = new Date(e.ts * 1000);
        const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const kind = (e.kind || '').replace(/_/g, ' ');
        const level = e.level || 'info';
        const actions = (!_readOnly && (level === 'error' || level === 'warn'))
            ? `<span class="log-actions"><button class="btn-investigate ${level}" onclick="investigateEvent(${i})" title="Spawn agent to investigate">Investigate</button></span>`
            : '';
        return `<div class="log-entry ${level}">
            <span class="log-ts">${time}</span>
            <span class="log-kind">${kind}</span>
            <span class="log-msg"><strong>${e.source}</strong> \u2014 ${e.summary}</span>
            ${actions}
        </div>`;
    }).join('');

    document.getElementById('status-log').innerHTML = `<div class="log-container">${logHtml}</div>`;

    // --- Notification log ---
    try {
        const logData = await fetchJSON('/api/notification-log');
        const logEl = document.getElementById('status-notif-log');
        if (!logEl) return;
        if (!logData || !logData.entries || logData.entries.length === 0) {
            logEl.innerHTML = '<div class="empty-state">No notifications yet</div>';
        } else {
            const logRows = logData.entries.map(e => {
                const dt = new Date(e.ts * 1000);
                const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
                const isReq = e.type === 'request';
                const pill = isReq
                    ? '<span class="type-pill request1" style="font-size:9px;padding:1px 6px">REQUEST</span>'
                    : '<span class="type-pill note1" style="font-size:9px;padding:1px 6px">NOTE</span>';
                const sid = e.short_id ? ' <code>#' + e.short_id + '</code>' : '';
                const surfaces = (e.surfaces || []).join(', ') || '\u2014';
                return '<tr>'
                    + '<td style="white-space:nowrap;color:var(--text-muted)">' + time + '</td>'
                    + '<td>' + pill + '</td>'
                    + '<td>' + (e.title || '') + sid + '</td>'
                    + '<td style="color:var(--text-muted)">' + surfaces + '</td>'
                    + '</tr>';
            }).join('');
            logEl.innerHTML = `
                <table class="data-table">
                    <thead><tr><th>Time</th><th>Type</th><th>Title</th><th>Surfaces</th></tr></thead>
                    <tbody>${logRows}</tbody>
                </table>
            `;
        }
    } catch(e) { /* notification log optional */ }
}


// ---- Chats ----
const chatsState = {
    chats: [],
    selectedId: null,
    messages: [],
    offset: 0,
    limit: 20,
    filteredCount: 0,
    hasMore: false,
    expandedMessages: new Set(),
    commits: [],
    searchHits: [],
    roleFilter: null,
};

/** Strip XML-like tags from message text (e.g. <command-name>...</command-name>) */
function cleanMsgText(text) {
    if (!text) return '';
    return text.replace(/<\/?[a-zA-Z][a-zA-Z0-9_-]*(?:\s[^>]*)?>/g, '');
}

async function loadChats() {
    const days = document.getElementById('chats-days')?.value || 14;
    const data = await fetchJSON('/api/chats?days=' + days);
    if (!data) return;
    chatsState.chats = data.chats || [];
    renderChatList();
    chatsPopulateProjectFilter();
}

function renderChatList() {
    const container = document.getElementById('chats-list');
    let chats = [...chatsState.chats];

    // Filter by selected project
    var projectFilter = document.getElementById('chats-project-filter')?.value;
    if (projectFilter) {
        chats = chats.filter(function(c) { return c.project_name === projectFilter; });
    }

    const sort = document.getElementById('chats-sort')?.value || 'recent';
    if (sort === 'longest') chats.sort((a, b) => (b.message_count - a.message_count));
    else if (sort === 'most-messages') chats.sort((a, b) => (b.tool_count - a.tool_count));

    if (chats.length === 0) {
        container.innerHTML = '<div class="empty-state">No chats found</div>';
        return;
    }

    container.innerHTML = chats.map(c => {
        const title = c.first_message
            ? escapeHtml(cleanMsgText(c.first_message).split('\\n')[0].substring(0, 100))
            : 'Untitled chat';
        return '<div class="chat-card' + (c.session_id === chatsState.selectedId ? ' active' : '') + '"'
            + ' data-sid="' + c.session_id + '">'
            + (c.project_name ? '<div class="chat-card-project">' + escapeHtml(c.project_name) + '</div>' : '')
            + '<div class="chat-card-title">' + title + '</div>'
            + '<div class="chat-card-meta">'
            + '<span>' + formatTimestamp(c.start_time) + '</span>'
            + '<span>' + (c.duration || '--') + '</span>'
            + '<span>' + c.message_count + ' msgs</span>'
            + '</div>'
            + (c.top_tools && c.top_tools.length
                ? '<div class="chat-card-tools">' + c.top_tools.join(', ') + '</div>'
                : '')
            + '</div>';
    }).join('');

    container.querySelectorAll('.chat-card').forEach(function(card) {
        card.addEventListener('click', function() { selectChat(card.dataset.sid); });
    });
}

async function selectChat(sessionId) {
    chatsState.selectedId = sessionId;
    chatsState.offset = 0;
    chatsState._earliestLoaded = 0;
    chatsState.messages = [];
    chatsState.expandedMessages.clear();
    chatsState.commits = [];
    chatsState.searchHits = [];
    chatsState.roleFilter = null;

    document.querySelectorAll('.chat-card').forEach(function(c) {
        c.classList.toggle('active', c.dataset.sid === sessionId);
    });

    document.getElementById('chats-viewer-empty').style.display = 'none';
    document.getElementById('chats-viewer').style.display = 'flex';
    document.getElementById('chats-viewer').style.flexDirection = 'column';
    document.getElementById('chats-viewer').style.flex = '1';
    document.getElementById('chats-in-search').style.display = 'none';
    document.getElementById('chats-commits-bar').style.display = 'none';
    document.getElementById('chats-message-list').innerHTML = '<div class="loading">Loading messages...</div>';

    const [msgData, commitData] = await Promise.all([
        fetchJSON('/api/chats/' + sessionId + '/messages?offset=0&limit=' + chatsState.limit),
        fetchJSON('/api/chats/' + sessionId + '/commits'),
    ]);

    if (msgData) {
        chatsState.messages = msgData.messages || [];
        chatsState.filteredCount = msgData.filtered_count || 0;
        chatsState.hasMore = msgData.has_more || false;
        chatsState.offset = chatsState.messages.length;
        renderChatHeader(msgData.metadata);
    }

    if (commitData && commitData.commits && commitData.commits.length > 0) {
        chatsState.commits = commitData.commits;
        renderCommitsBar(commitData.commits);
    }

    renderMessages();
}

function renderChatHeader(meta) {
    if (!meta) return;
    var header = document.getElementById('chats-viewer-header');
    header.innerHTML = '<div class="chats-hdr-left">'
        + '<code>' + (meta.session_id ? meta.session_id.substring(0, 8) : '--') + '</code>'
        + ' &middot; ' + (meta.message_count || 0) + ' messages'
        + ' &middot; ' + (meta.duration || '--')
        + (meta.start_time ? ' &middot; ' + formatTimestamp(meta.start_time) : '')
        + '</div>'
        + '<div class="chats-hdr-right">'
        + '<button class="chats-hdr-btn" onclick="chatsToggleInSearch()">Search</button>'
        + '<button class="chats-hdr-btn' + (chatsState.roleFilter === 'user' ? ' active' : '') + '" onclick="chatsFilterRole(&#39;user&#39;)">User</button>'
        + '<button class="chats-hdr-btn' + (chatsState.roleFilter === 'assistant' ? ' active' : '') + '" onclick="chatsFilterRole(&#39;assistant&#39;)">Assistant</button>'
        + '<button class="chats-hdr-btn' + (!chatsState.roleFilter ? ' active' : '') + '" onclick="chatsFilterRole(null)">All</button>'
        + '</div>';
}

function renderMessages() {
    var container = document.getElementById('chats-message-list');
    if (chatsState.messages.length === 0) {
        container.innerHTML = '<div class="empty-state">No messages</div>';
        document.getElementById('chats-load-later').style.display = 'none';
        return;
    }

    var html = '';
    chatsState.messages.forEach(function(msg) {
        var isExpanded = chatsState.expandedMessages.has(msg.index);
        var inSpan = chatsState.searchHits.some(function(h) {
            return msg.index >= h.turn_range[0] && msg.index < h.turn_range[1];
        });
        var rawText = cleanMsgText(isExpanded ? (msg.text || msg.text_preview || '') : (msg.text_preview || msg.text || ''));
        var text = rawText.substring(0, isExpanded ? 100000 : 300);
        var truncated = !isExpanded && rawText.length > 300;
        var hasText = text.trim().length > 0;
        var hasTools = msg.tools && msg.tools.length > 0;

        // Skip entirely empty turns (no text, no tools)
        if (!hasText && !hasTools) return;

        // Build tool badges string (reused in bubble or meta)
        var toolBadges = hasTools
            ? msg.tools.map(function(t) { return '<span class="chat-msg-tool-badge">' + t + '</span>'; }).join('')
            : '';

        // For tool-only turns, show tools inside the bubble instead of empty
        var bubbleContent = '';
        if (hasText) {
            bubbleContent = escapeHtml(text)
                + (truncated ? '<span class="chat-msg-truncated"> ...</span>' : '');
        } else {
            bubbleContent = '<div class="chat-msg-tools" style="margin:0;">' + toolBadges + '</div>';
        }

        html += '<div class="chat-msg ' + msg.role + '">'
            + '<div class="chat-msg-bubble'
            + (isExpanded ? ' expanded' : '')
            + (inSpan ? ' in-span' : '')
            + '" data-idx="' + msg.index + '" onclick="chatsMsgClick(' + msg.index + ')">'
            + bubbleContent
            + '</div>'
            + '<div class="chat-msg-meta">'
            + (msg.timestamp ? formatTimestamp(msg.timestamp) : '')
            + (hasText && msg.role === 'assistant' && hasTools
                ? ' <div class="chat-msg-tools">' + toolBadges + '</div>'
                : '')
            + '</div></div>';
    });

    container.innerHTML = html;

    document.getElementById('chats-load-later').style.display =
        chatsState.hasMore ? 'block' : 'none';

    // Show load-earlier if we jumped into the middle of the conversation
    var earliest = chatsState._earliestLoaded || 0;
    document.getElementById('chats-load-earlier').style.display =
        earliest > 0 ? 'block' : 'none';
}

async function chatsMsgClick(index) {
    if (chatsState.expandedMessages.has(index)) {
        chatsState.expandedMessages.delete(index);
        renderMessages();
        return;
    }

    var data = await fetchJSON(
        '/api/chats/' + chatsState.selectedId + '/expand/' + index + '?context_window=0'
    );
    if (!data || !data.messages) return;

    var fullMsg = data.messages.find(function(m) { return m.is_center; });
    if (fullMsg) {
        var existing = chatsState.messages.find(function(m) { return m.index === index; });
        if (existing) {
            existing.text = fullMsg.text;
        }
    }
    chatsState.expandedMessages.add(index);
    renderMessages();
}

async function chatsLoadLater() {
    var data = await fetchJSON(
        '/api/chats/' + chatsState.selectedId + '/messages?offset=' + chatsState.offset
        + '&limit=' + chatsState.limit
        + (chatsState.roleFilter ? '&roles=' + chatsState.roleFilter : '')
    );
    if (!data) return;
    chatsState.messages = chatsState.messages.concat(data.messages || []);
    chatsState.hasMore = data.has_more || false;
    chatsState.offset += (data.messages || []).length;

    // Preserve scroll position — content added below, so same scrollTop works
    var scroller = document.getElementById('chats-messages');
    var prevScroll = scroller.scrollTop;
    renderMessages();
    scroller.scrollTop = prevScroll;
}

async function chatsLoadEarlier() {
    var earliest = chatsState._earliestLoaded || 0;
    if (earliest <= 0) return;

    var newOffset = Math.max(0, earliest - chatsState.limit);
    var newLimit = earliest - newOffset;
    if (newLimit <= 0) return;

    var url = '/api/chats/' + chatsState.selectedId + '/messages?offset=' + newOffset + '&limit=' + newLimit;
    if (chatsState.roleFilter) url += '&roles=' + chatsState.roleFilter;

    var data = await fetchJSON(url);
    if (!data) return;

    var newMsgs = data.messages || [];
    if (newMsgs.length === 0) return;

    // Prepend to existing messages
    chatsState.messages = newMsgs.concat(chatsState.messages);
    chatsState._earliestLoaded = newOffset;

    // Preserve scroll position — content added above, so offset by the height delta
    var scroller = document.getElementById('chats-messages');
    var list = document.getElementById('chats-message-list');
    var prevHeight = list.scrollHeight;
    var prevScroll = scroller.scrollTop;
    renderMessages();
    var newHeight = list.scrollHeight;
    scroller.scrollTop = prevScroll + (newHeight - prevHeight);
}

// ---- Chats: Global search ----

// Track the last global search query so we can carry it into in-chat search
var _lastGlobalQuery = '';
var _commitsPrepared = false;
var _commitsPreparedProject = '';

function chatsPopulateProjectFilter() {
    var select = document.getElementById('chats-project-filter');
    var current = select.value;

    // Collect distinct project names from loaded chats
    var projects = {};
    chatsState.chats.forEach(function(c) {
        if (c.project_name) projects[c.project_name] = (projects[c.project_name] || 0) + 1;
    });

    var sorted = Object.keys(projects).sort();
    var html = '<option value="">All repos</option>';
    sorted.forEach(function(p) {
        html += '<option value="' + escapeHtml(p) + '">' + escapeHtml(p) + ' (' + projects[p] + ')</option>';
    });
    select.innerHTML = html;

    // Restore previous selection if still valid
    if (current && projects[current]) select.value = current;

    chatsUpdateCommitOption();
}

function chatsProjectFilterChanged(project) {
    var select = document.getElementById('chats-project-filter');
    select.classList.toggle('active', !!project);

    chatsUpdateCommitOption();

    // Reset commit cache when project changes
    if (_commitsPreparedProject !== project) {
        _commitsPrepared = false;
        _commitsPreparedProject = '';
    }

    // If Commit method is selected but now hidden, fall back to Hybrid
    var methodSelect = document.getElementById('chats-search-method');
    if (methodSelect.value === 'commit' && !project) {
        methodSelect.value = 'keyword,semantic';
        chatsSearchMethodChanged('keyword,semantic');
    }

    // Re-render chat list with project filter applied
    renderChatList();
}

function chatsUpdateCommitOption() {
    var project = document.getElementById('chats-project-filter').value;
    var methodSelect = document.getElementById('chats-search-method');

    // Add or remove Commit option based on project selection
    var commitOpt = methodSelect.querySelector('option[value="commit"]');
    if (project) {
        if (!commitOpt) {
            commitOpt = document.createElement('option');
            commitOpt.value = 'commit';
            commitOpt.textContent = 'Commit';
            methodSelect.appendChild(commitOpt);
        }
    } else {
        if (commitOpt) commitOpt.remove();
    }
}

function chatsSearchMethodChanged(method) {
    var input = document.getElementById('chats-global-search');
    var project = document.getElementById('chats-project-filter').value;

    if (method === 'commit') {
        input.placeholder = 'Search by commit hash or message...';
        // Pre-embed commits for the selected project
        if (!_commitsPrepared || _commitsPreparedProject !== project) {
            _commitsPrepared = true;
            _commitsPreparedProject = project;
            var prepareUrl = '/api/chats/commits/prepare'
                + (project ? '?project=' + encodeURIComponent(project) : '');
            fetch(prepareUrl, { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (d && d.commit_count) {
                        console.log('Commit embeddings warmed: ' + d.commit_count + ' commits');
                    }
                }).catch(function() {});
        }
    } else {
        input.placeholder = project
            ? 'Search in ' + project + '...'
            : 'Search across all chats...';
    }
}

async function chatsGlobalSearch() {
    var q = document.getElementById('chats-global-search').value.trim();
    if (!q) {
        document.getElementById('chats-search-results').style.display = 'none';
        return;
    }
    _lastGlobalQuery = q;

    var method = document.getElementById('chats-search-method').value;
    if (method === 'commit') return chatsCommitSearch(q);

    var resultsDiv = document.getElementById('chats-search-results');
    resultsDiv.style.display = 'block';
    resultsDiv.innerHTML = '<div class="loading">Searching...</div>';

    var project = document.getElementById('chats-project-filter').value;
    var data = await fetchJSON(
        '/api/chats/search?q=' + encodeURIComponent(q) + '&method=' + method
        + (project ? '&project=' + encodeURIComponent(project) : '')
    );

    if (!data || data.error) {
        resultsDiv.innerHTML = '<div class="empty-state">'
            + (data && data.error ? escapeHtml(data.error) : 'Search failed') + '</div>';
        return;
    }

    // Server returns {sessions: [...], total_chunks: N} — already grouped and scored
    var sessions = data.sessions || [];
    if (sessions.length === 0) {
        resultsDiv.innerHTML = '<div class="empty-state">No results found</div>';
        return;
    }

    // Enrich with data from the chat list (first_message, duration, msg count)
    var chatLookup = {};
    chatsState.chats.forEach(function(c) { chatLookup[c.session_id] = c; });

    var html = '<div style="display:flex;justify-content:space-between;margin-bottom:8px;">'
        + '<span style="font-size:12px;color:var(--text-muted);">'
        + sessions.length + ' chat' + (sessions.length !== 1 ? 's' : '')
        + ' (' + (data.total_chunks || 0) + ' chunks) for "' + escapeHtml(q) + '"</span>'
        + '<button class="chats-hdr-btn" onclick="chatsCloseGlobalSearch()">Close</button>'
        + '</div>';

    sessions.forEach(function(sess, gi) {
        var chatInfo = chatLookup[sess.session_id];
        var title = chatInfo
            ? escapeHtml(cleanMsgText(chatInfo.first_message || '').split('\\n')[0].substring(0, 80))
            : '';
        var duration = chatInfo ? (chatInfo.duration || '') : '';
        var msgCount = chatInfo ? (chatInfo.message_count || '') : '';
        var timeStr = sess.start_time ? formatTimestamp(sess.start_time) : '';

        // Session header — clicking opens the chat at the top
        html += '<div class="chats-search-session-group">'
            + '<div class="chats-search-session-hdr" onclick="chatsOpenFromSearch(&#39;' + sess.session_id + '&#39;)">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;">'
            + '<span>'
            + '<span class="chats-hit-score" style="margin-right:6px;">#' + (gi + 1) + '</span>'
            + '<code style="color:var(--text-primary);font-size:11px;">' + sess.short_id + '</code>'
            + (sess.project_name ? ' <span style="color:var(--accent);font-size:10px;font-weight:600;text-transform:uppercase;margin-left:6px;">' + escapeHtml(sess.project_name) + '</span>' : '')
            + '</span>'
            + '<span style="font-size:11px;color:var(--text-muted);">'
            + sess.chunks.length + ' hit' + (sess.chunks.length !== 1 ? 's' : '')
            + (duration ? ' &middot; ' + duration : '')
            + (msgCount ? ' &middot; ' + msgCount + ' msgs' : '')
            + (timeStr ? ' &middot; ' + timeStr : '')
            + '</span></div>'
            + (title ? '<div style="font-size:12px;color:var(--text-secondary);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + title + '</div>' : '')
            + '</div>';

        // Individual chunk hits — clicking jumps to that exact location
        (sess.chunks || []).forEach(function(chunk) {
            html += '<div class="chats-search-chunk" onclick="chatsJumpToHit(&#39;' + sess.session_id + '&#39;,' + chunk.span_index + ')">'
                + '<div style="font-size:12px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
                + escapeHtml(cleanMsgText(chunk.display_text)) + '</div>'
                + '</div>';
        });

        html += '</div>';
    });

    resultsDiv.innerHTML = html;
}

function chatsCloseGlobalSearch() {
    document.getElementById('chats-search-results').style.display = 'none';
}

async function chatsCommitSearch(q) {
    var resultsDiv = document.getElementById('chats-search-results');
    resultsDiv.style.display = 'block';
    resultsDiv.innerHTML = '<div class="loading">Searching commits...</div>';

    var project = document.getElementById('chats-project-filter').value;
    var url = '/api/chats/search/commits?q=' + encodeURIComponent(q)
        + (project ? '&project=' + encodeURIComponent(project) : '');
    var data = await fetchJSON(url);

    if (!data || data.error) {
        resultsDiv.innerHTML = '<div class="empty-state">'
            + (data && data.error ? escapeHtml(data.error) : 'Commit search failed') + '</div>';
        return;
    }

    var sessions = data.sessions || [];
    if (sessions.length === 0) {
        resultsDiv.innerHTML = '<div class="empty-state">No matching commits found</div>';
        return;
    }

    var chatLookup = {};
    chatsState.chats.forEach(function(c) { chatLookup[c.session_id] = c; });

    var totalCommits = data.total_commits || 0;
    var html = '<div style="display:flex;justify-content:space-between;margin-bottom:8px;">'
        + '<span style="font-size:12px;color:var(--text-muted);">'
        + totalCommits + ' commit' + (totalCommits !== 1 ? 's' : '')
        + ' in ' + sessions.length + ' chat' + (sessions.length !== 1 ? 's' : '')
        + ' for "' + escapeHtml(q) + '"</span>'
        + '<button class="chats-hdr-btn" onclick="chatsCloseGlobalSearch()">Close</button>'
        + '</div>';

    sessions.forEach(function(sess, gi) {
        var chatInfo = chatLookup[sess.session_id];
        var title = chatInfo
            ? escapeHtml(cleanMsgText(chatInfo.first_message || '').split('\\n')[0].substring(0, 80))
            : '';
        var duration = chatInfo ? (chatInfo.duration || '') : '';
        var msgCount = chatInfo ? (chatInfo.message_count || '') : '';

        html += '<div class="chats-search-session-group">'
            + '<div class="chats-search-session-hdr" onclick="chatsOpenFromSearch(&#39;' + sess.session_id + '&#39;)">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;">'
            + '<span>'
            + '<span class="chats-hit-score" style="margin-right:6px;">#' + (gi + 1) + '</span>'
            + '<code style="color:var(--text-primary);font-size:11px;">' + sess.short_id + '</code>'
            + '</span>'
            + '<span style="font-size:11px;color:var(--text-muted);">'
            + sess.commits.length + ' commit' + (sess.commits.length !== 1 ? 's' : '')
            + (duration ? ' &middot; ' + duration : '')
            + (msgCount ? ' &middot; ' + msgCount + ' msgs' : '')
            + '</span></div>'
            + (title ? '<div style="font-size:12px;color:var(--text-secondary);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + title + '</div>' : '')
            + '</div>';

        (sess.commits || []).forEach(function(commit) {
            var hasIdx = commit.message_index != null;
            var clickFn = hasIdx
                ? 'chatsJumpToCommitSearch(&#39;' + sess.session_id + '&#39;,' + commit.message_index + ')'
                : 'chatsOpenFromSearch(&#39;' + sess.session_id + '&#39;)';
            html += '<div class="chats-search-chunk" onclick="' + clickFn + '">'
                + '<div class="chat-commit-marker" style="margin:0;">'
                + '<code>' + (commit.hash || '') + '</code> '
                + '<span class="commit-msg">' + escapeHtml(commit.message || '') + '</span>'
                + '<span class="commit-meta">'
                + '<span>' + (commit.branch || '') + '</span>'
                + (commit.files_changed ? '<span>' + commit.files_changed + ' files</span>' : '')
                + '<span>' + formatTimestamp(commit.timestamp) + '</span>'
                + '</span></div></div>';
        });

        html += '</div>';
    });

    resultsDiv.innerHTML = html;
}

async function chatsJumpToCommitSearch(sessionId, messageIndex) {
    chatsCloseGlobalSearch();

    chatsState.selectedId = sessionId;
    chatsState.expandedMessages.clear();
    chatsState.searchHits = [];
    chatsState.commits = [];
    chatsState.roleFilter = null;

    document.querySelectorAll('.chat-card').forEach(function(c) {
        c.classList.toggle('active', c.dataset.sid === sessionId);
    });

    document.getElementById('chats-viewer-empty').style.display = 'none';
    document.getElementById('chats-viewer').style.display = 'flex';
    document.getElementById('chats-viewer').style.flexDirection = 'column';
    document.getElementById('chats-viewer').style.flex = '1';
    document.getElementById('chats-message-list').innerHTML = '<div class="loading">Jumping to commit...</div>';

    var contextWindow = Math.floor(chatsState.limit / 2);
    var offset = Math.max(0, messageIndex - contextWindow);
    var url = '/api/chats/' + sessionId + '/messages?offset=' + offset + '&limit=' + chatsState.limit;

    var msgData = await fetchJSON(url);
    if (!msgData || !msgData.messages || msgData.messages.length === 0) {
        document.getElementById('chats-message-list').innerHTML =
            '<div class="empty-state">Failed to load messages</div>';
        return;
    }

    chatsState.messages = msgData.messages;
    chatsState.filteredCount = msgData.filtered_count || 0;
    chatsState.hasMore = msgData.has_more || false;

    var msgs = chatsState.messages;
    chatsState.offset = msgs[msgs.length - 1].index + 1;
    chatsState._earliestLoaded = msgs[0].index;

    renderChatHeader(msgData.metadata);

    // Load commits bar in background
    fetchJSON('/api/chats/' + sessionId + '/commits').then(function(commitData) {
        if (commitData && commitData.commits && commitData.commits.length > 0) {
            chatsState.commits = commitData.commits;
            renderCommitsBar(commitData.commits);
        }
    });

    renderMessages();

    setTimeout(function() {
        var target = document.querySelector('.chat-msg-bubble[data-idx="' + messageIndex + '"]');
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.style.transition = 'box-shadow 0.3s';
            target.style.boxShadow = '0 0 0 3px #3fb950';
            setTimeout(function() { target.style.boxShadow = ''; }, 1500);
        }
    }, 150);
}

/** Open a chat from search results — loads from the top and carries the query into in-chat search */
async function chatsOpenFromSearch(sessionId) {
    chatsCloseGlobalSearch();
    await selectChat(sessionId);

    // Carry the search query into the in-chat search bar
    if (_lastGlobalQuery) {
        document.getElementById('chats-in-search-input').value = _lastGlobalQuery;
        document.getElementById('chats-in-search').style.display = 'flex';
    }
}

/** Jump to a specific chunk within a session from search results */
async function chatsJumpToHit(sessionId, spanIndex) {
    chatsCloseGlobalSearch();

    chatsState.selectedId = sessionId;
    chatsState.expandedMessages.clear();
    chatsState.searchHits = [];
    chatsState.commits = [];
    chatsState.roleFilter = null;

    document.querySelectorAll('.chat-card').forEach(function(c) {
        c.classList.toggle('active', c.dataset.sid === sessionId);
    });

    document.getElementById('chats-viewer-empty').style.display = 'none';
    document.getElementById('chats-viewer').style.display = 'flex';
    document.getElementById('chats-viewer').style.flexDirection = 'column';
    document.getElementById('chats-viewer').style.flex = '1';
    document.getElementById('chats-message-list').innerHTML = '<div class="loading">Jumping to result...</div>';

    // Carry the search query into the in-chat search bar
    if (_lastGlobalQuery) {
        document.getElementById('chats-in-search-input').value = _lastGlobalQuery;
        document.getElementById('chats-in-search').style.display = 'flex';
    }

    var data = await fetchJSON('/api/chats/' + sessionId + '/locate/' + spanIndex);
    if (!data || data.error) {
        document.getElementById('chats-message-list').innerHTML =
            '<div class="empty-state">' + (data && data.error ? escapeHtml(data.error) : 'Failed to locate') + '</div>';
        return;
    }

    chatsState.messages = data.messages || [];
    chatsState.filteredCount = data.total_messages || 0;
    if (data.span_turn_range) {
        chatsState.searchHits = [{ turn_range: data.span_turn_range }];
    }

    // Set pagination state based on the window of messages returned
    var msgs = chatsState.messages;
    if (msgs.length > 0) {
        var firstIdx = msgs[0].index;
        var lastIdx = msgs[msgs.length - 1].index;
        chatsState.hasMore = lastIdx < (chatsState.filteredCount - 1);
        chatsState.offset = lastIdx + 1;
        chatsState._earliestLoaded = firstIdx;
    } else {
        chatsState.hasMore = false;
        chatsState.offset = 0;
        chatsState._earliestLoaded = 0;
    }

    renderChatHeader(data.metadata);
    renderMessages();

    // Also fetch commits in the background
    fetchJSON('/api/chats/' + sessionId + '/commits').then(function(commitData) {
        if (commitData && commitData.commits && commitData.commits.length > 0) {
            chatsState.commits = commitData.commits;
            renderCommitsBar(commitData.commits);
        }
    });

    setTimeout(function() {
        var spanEl = document.querySelector('.chat-msg-bubble.in-span');
        if (spanEl) spanEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 150);
}

// ---- Chats: In-session search ----

function chatsToggleInSearch() {
    var el = document.getElementById('chats-in-search');
    if (el.style.display === 'none' || el.style.display === '') {
        el.style.display = 'flex';
        document.getElementById('chats-in-search-input').focus();
    } else {
        el.style.display = 'none';
    }
}

function chatsCloseInSearch() {
    document.getElementById('chats-in-search').style.display = 'none';
    chatsState.searchHits = [];
    document.getElementById('chats-in-search-hits').innerHTML = '';
    renderMessages();
}

async function chatsInSessionSearch() {
    var q = document.getElementById('chats-in-search-input').value.trim();
    if (!q || !chatsState.selectedId) return;

    var hitsDiv = document.getElementById('chats-in-search-hits');
    hitsDiv.innerHTML = '<span style="font-size:12px;color:var(--text-muted);">Searching...</span>';

    var data = await fetchJSON(
        '/api/chats/' + chatsState.selectedId + '/search?q=' + encodeURIComponent(q)
    );

    if (!data || data.error) {
        hitsDiv.innerHTML = '<span style="color:#f85149;font-size:12px;">'
            + (data && data.error ? escapeHtml(data.error) : 'Search failed') + '</span>';
        return;
    }

    var hits = data.hits || [];
    chatsState.searchHits = hits;

    if (hits.length === 0) {
        hitsDiv.innerHTML = '<span style="font-size:12px;color:var(--text-muted);">No results</span>';
        renderMessages();
        return;
    }

    // Build a rich hit list with snippets from each span
    var html = '<div style="width:100%;margin-top:4px;">';
    html += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:6px;">'
        + hits.length + ' match' + (hits.length !== 1 ? 'es' : '') + ' found</div>';

    hits.forEach(function(h, i) {
        // Extract a snippet from the first user message in the span
        var snippet = '';
        if (h.messages && h.messages.length > 0) {
            for (var mi = 0; mi < h.messages.length; mi++) {
                var mtxt = cleanMsgText(h.messages[mi].text || h.messages[mi].text_preview || '');
                if (mtxt.length > 10) {
                    snippet = mtxt.substring(0, 120);
                    break;
                }
            }
        }
        if (!snippet) snippet = 'Messages ' + h.turn_range[0] + '-' + h.turn_range[1];

        html += '<div class="chats-search-hit" style="padding:6px 8px;cursor:pointer;" onclick="chatsJumpToInHit(' + i + ')">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;">'
            + '<span style="font-size:11px;font-weight:600;color:var(--accent);">#' + (i + 1) + '</span>'
            + '<span style="font-size:10px;color:var(--text-muted);">msgs ' + h.turn_range[0] + '\u2013' + h.turn_range[1] + '</span>'
            + '</div>'
            + '<div style="font-size:12px;color:var(--text-secondary);margin-top:2px;'
            + 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
            + escapeHtml(snippet) + '</div></div>';
    });
    html += '</div>';
    hitsDiv.innerHTML = html;

    // Reload messages to include the full conversation so highlights work
    var msgData = await fetchJSON(
        '/api/chats/' + chatsState.selectedId + '/messages?offset=0&limit=200'
    );
    if (msgData) {
        chatsState.messages = msgData.messages || [];
        chatsState.filteredCount = msgData.filtered_count || 0;
        chatsState.hasMore = msgData.has_more || false;
        chatsState.offset = chatsState.messages.length;
    }
    renderMessages();

    // Scroll to first hit
    if (hits.length > 0 && hits[0].turn_range) {
        setTimeout(function() {
            var el = document.querySelector('.chat-msg-bubble[data-idx="' + hits[0].turn_range[0] + '"]');
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 150);
    }
}

function chatsJumpToInHit(hitIndex) {
    var hit = chatsState.searchHits[hitIndex];
    if (!hit || !hit.turn_range) return;
    // Scroll to the first message in this span
    var el = document.querySelector('.chat-msg-bubble[data-idx="' + hit.turn_range[0] + '"]');
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        // Flash animation to draw attention
        el.style.transition = 'box-shadow 0.3s';
        el.style.boxShadow = '0 0 0 3px var(--accent)';
        setTimeout(function() { el.style.boxShadow = ''; }, 1500);
    }
}

// ---- Chats: Role filter ----

async function chatsFilterRole(role) {
    chatsState.roleFilter = role;
    chatsState.offset = 0;
    chatsState.messages = [];
    chatsState.expandedMessages.clear();

    var url = '/api/chats/' + chatsState.selectedId + '/messages?offset=0&limit=' + chatsState.limit;
    if (role) url += '&roles=' + role;

    var data = await fetchJSON(url);
    if (!data) return;
    chatsState.messages = data.messages || [];
    chatsState.filteredCount = data.filtered_count || 0;
    chatsState.hasMore = data.has_more || false;
    chatsState.offset = chatsState.messages.length;
    renderChatHeader(data.metadata);
    renderMessages();
}

// ---- Chats: Commits bar ----

function renderCommitsBar(commits) {
    var bar = document.getElementById('chats-commits-bar');
    bar.style.display = 'block';

    // Group by message to deduplicate retried/amended commits
    var groups = [];
    var seen = {};
    commits.forEach(function(c) {
        var key = (c.message || '').trim();
        if (seen[key]) {
            seen[key].hashes.push(c.hash || '');
            seen[key].count++;
        } else {
            var g = {
                message: key, hashes: [c.hash || ''], branch: c.branch || '',
                files_changed: c.files_changed, count: 1,
                message_index: c.message_index != null ? c.message_index : null,
                timestamp: c.timestamp || '',
            };
            seen[key] = g;
            groups.push(g);
        }
    });

    // Sort chronologically (oldest first)
    groups.sort(function(a, b) { return (a.timestamp || '').localeCompare(b.timestamp || ''); });

    var html = '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">'
        + groups.length + ' unique commit' + (groups.length !== 1 ? 's' : '')
        + (commits.length !== groups.length ? ' (' + commits.length + ' total incl. retries)' : '')
        + ' during this session</div>';

    groups.forEach(function(g) {
        var clickable = g.message_index != null;
        var clickAttr = clickable
            ? ' onclick="chatsJumpToCommit(' + g.message_index + ')" title="Jump to this commit in the conversation"'
            : '';
        html += '<div class="chat-commit-marker' + (clickable ? ' clickable' : '') + '"' + clickAttr + '>'
            + '<code>' + g.hashes[0] + '</code> '
            + '<span class="commit-msg">' + escapeHtml(g.message) + '</span>'
            + '<span class="commit-meta">'
            + (g.count > 1 ? '<span>(' + g.count + 'x)</span>' : '')
            + '<span>' + g.branch + '</span>'
            + (g.files_changed ? '<span>' + g.files_changed + ' files</span>' : '')
            + '<span>' + formatTimestamp(g.timestamp) + '</span>'
            + '</span>'
            + '</div>';
    });
    bar.innerHTML = html;
}

async function chatsJumpToCommit(messageIndex) {
    // Check if the target message is already in the DOM
    var el = document.querySelector('.chat-msg-bubble[data-idx="' + messageIndex + '"]');
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.style.transition = 'box-shadow 0.3s';
        el.style.boxShadow = '0 0 0 3px #3fb950';
        setTimeout(function() { el.style.boxShadow = ''; }, 1500);
        return;
    }

    // Target not loaded — fetch a page centered on the commit's turn
    var contextWindow = Math.floor(chatsState.limit / 2);
    var offset = Math.max(0, messageIndex - contextWindow);
    var url = '/api/chats/' + chatsState.selectedId + '/messages?offset=' + offset + '&limit=' + chatsState.limit;
    if (chatsState.roleFilter) url += '&roles=' + chatsState.roleFilter;

    var data = await fetchJSON(url);
    if (!data || !data.messages || data.messages.length === 0) return;

    chatsState.messages = data.messages;
    chatsState.filteredCount = data.filtered_count || 0;
    chatsState.hasMore = data.has_more || false;

    var msgs = chatsState.messages;
    var firstIdx = msgs[0].index;
    var lastIdx = msgs[msgs.length - 1].index;
    chatsState.offset = lastIdx + 1;
    chatsState._earliestLoaded = firstIdx;

    renderMessages();

    // Scroll after DOM update
    setTimeout(function() {
        var target = document.querySelector('.chat-msg-bubble[data-idx="' + messageIndex + '"]');
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'center' });
            target.style.transition = 'box-shadow 0.3s';
            target.style.boxShadow = '0 0 0 3px #3fb950';
            setTimeout(function() { target.style.boxShadow = ''; }, 1500);
        }
    }, 150);
}

// ---- Chats: Event listeners ----

document.getElementById('chats-sort')?.addEventListener('change', renderChatList);
document.getElementById('chats-days')?.addEventListener('change', function() { loadChats(); });
document.getElementById('chats-global-search')?.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') chatsGlobalSearch();
});
document.getElementById('chats-in-search-input')?.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') chatsInSessionSearch();
});


// ---- Contracts ----
async function loadContracts() {
    const data = await fetchJSON('/api/contracts');
    if (!data) return;

    const contracts = data.contracts || [];
    if (contracts.length === 0) {
        document.getElementById('contracts-table').innerHTML = '<div class="empty-state">No active contracts</div>';
        return;
    }

    let rows = contracts.map(c => `
        <tr>
            <td><strong>${c.title}</strong></td>
            <td>${statusBadge(c.status)}</td>
            <td>${c.type || '—'}</td>
            <td>${c.deadline || '—'}</td>
            <td>${c.priority || '—'}</td>
        </tr>
    `).join('');

    document.getElementById('contracts-table').innerHTML = `
        <table class="data-table">
            <thead><tr><th>Contract</th><th>Status</th><th>Type</th><th>Deadline</th><th>Priority</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}


// ---- Projects ----
let _projectsCache = [];

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
}

async function selectProject(slug) {
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


// ---- Auto-refresh ----
let refreshInterval = null;

function startAutoRefresh(seconds = 30) {
    if (refreshInterval) clearInterval(refreshInterval);
    refreshInterval = setInterval(() => {
        const activeTab = document.querySelector('.tab-btn.active');
        // Don't auto-refresh workflow views — they have live user state
        if (activeTab && !activeTab.dataset.tab.startsWith('wv-')) switchTab(activeTab.dataset.tab);
    }, seconds * 1000);
}

// ---- Init ----
// Set dynamic Obsidian vault links
if (WB_VAULT_NAME) {
    const mtl = document.getElementById('master-task-link');
    if (mtl) mtl.href = `obsidian://open?vault=${encodeURIComponent(WB_VAULT_NAME)}&file=tasks%2Fmaster-task-list.md`;
}
loadOverview();
startAutoRefresh(30);
"""


# ---------------------------------------------------------------------------
# Workflow views: polling + tab management
# ---------------------------------------------------------------------------
