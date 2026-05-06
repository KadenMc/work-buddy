"""Dashboard shared helper JS — utilities used by every tab.

Tab-agnostic primitives: HTTP fetch, status badges, health-tree
rendering, time formatters. Concatenated BEFORE every tab module so
the helpers exist in scope when tab loaders run.

Originally lived inside ``script_main.py``'s ``// ---- Helpers ----``
section (~458 lines).
"""

from __future__ import annotations


def script() -> str:
    return r"""
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
    // Wizard hint + launch buttons — shown when there's a failure
    if (diag.status === 'failed' && diag.component_id) {
        const cid = escapeHtml(diag.component_id);
        html += '<div class="health-diag-wizard">'
            + '\ud83e\ude84 Run <code>/wb-setup diagnose ' + cid + '</code> in Claude Code'
            + '<span class="wizard-launch-btns">'
            + ' <button class="wizard-launch-btn" onclick="launchSetupAgent(\'' + cid + '\', \'desktop\', this)"'
            + ' title="Open terminal session on this machine">\ud83d\udda5 Desktop</button>'
            + ' <button class="wizard-launch-btn mobile" onclick="launchSetupAgent(\'' + cid + '\', \'mobile\', this)"'
            + ' title="Launch remote-control session for mobile access">\ud83d\udcf1 Mobile</button>'
            + '</span>'
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

async function fetchJSON(url, options) {
    // Accept either ``fetchJSON(url)`` (GET) or ``fetchJSON(url, {method, body, headers})``.
    // Previously silently dropped the options argument, which meant POST
    // callers sent plain GETs and Flask replied 405 — see the Review tab
    // approve path that was a no-op until this fix landed (2026-04-20).
    try {
        const r = await fetch(url, options);
        return await r.json();
    } catch (e) {
        console.error('Fetch failed:', url, e);
        return null;
    }
}
"""
