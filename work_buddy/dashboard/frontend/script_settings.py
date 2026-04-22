"""Dashboard JS for the Settings tab.

Renders the control graph produced by ``GET /api/control/graph`` as a
hierarchy of domains → subsystems/components → requirements + affected
capabilities. Phase E is read-only; Phase F will wire preference
toggles into a ``POST /api/control/preference`` endpoint.

The renderer reuses ``statusBadge()`` from ``script_main.py`` but
extends the mapping for ``unconfigured`` and preference labels.
"""

from __future__ import annotations


def _settings_script() -> str:
    return r"""
// ---- Settings tab: unified control graph ----

// Internal state
let WB_CONTROL_GRAPH = null;
let WB_CONTROL_FILTER = '';

async function loadSettings(force) {
    const tree = document.getElementById('settings-tree');
    const summary = document.getElementById('settings-summary');
    if (!tree) return;
    if (force) tree.innerHTML = '<div class="loading">Rebuilding control graph...</div>';

    try {
        const url = '/api/control/graph' + (force ? '?force=1' : '');
        const resp = await fetch(url);
        if (!resp.ok) {
            tree.innerHTML = `<div class="error-state">Failed to load (${resp.status}): ${await resp.text()}</div>`;
            return;
        }
        const data = await resp.json();
        WB_CONTROL_GRAPH = data;
        renderSettingsTree();
        renderSettingsSummary();
    } catch (exc) {
        tree.innerHTML = `<div class="error-state">Error loading control graph: ${escapeHtml(String(exc))}</div>`;
    }
}

// Filter input — debounced
let _settingsFilterTimer = null;
document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('settings-filter');
    if (!input) return;
    input.addEventListener('input', (e) => {
        clearTimeout(_settingsFilterTimer);
        _settingsFilterTimer = setTimeout(() => {
            WB_CONTROL_FILTER = (e.target.value || '').toLowerCase().trim();
            renderSettingsTree();
        }, 150);
    });
});

// ---- Badge helper: extend script_main's statusBadge for control-graph states ----
function controlStateBadge(state) {
    const map = {
        ok: 'badge-green',
        degraded: 'badge-yellow',
        blocked: 'badge-red',
        unconfigured: 'badge-yellow',
        disabled: 'badge-muted',
        unknown: 'badge-muted',
    };
    return `<span class="badge ${map[state] || 'badge-muted'}">${state}</span>`;
}

function preferenceBadge(pref) {
    if (!pref) return '';
    const map = {
        wanted: 'badge-blue',
        unwanted: 'badge-muted',
        undecided: 'badge-purple',
        required: 'badge-green',
    };
    const titleMap = {
        wanted: 'You want this component.',
        unwanted: 'You opted out of this component.',
        undecided: 'You have not decided about this component yet.',
        required: 'Core component — nothing in work-buddy works without this; cannot be opted out.',
    };
    return `<span class="badge ${map[pref] || 'badge-muted'}" title="${escapeHtml(titleMap[pref] || 'Feature preference')}">${pref}</span>`;
}

// ---- Preference toggle ----
// Three-state cycle: undecided → wanted → unwanted → undecided.
// Writes go through POST /api/control/preference (consent-gated server-side,
// auto-granted because the click IS the consent).
//
// Core ("required") components do NOT get a toggle — the model enforces
// they can't be opted out, so offering the choice would lie.
function preferenceToggleControls(componentId, currentPref) {
    if (!componentId) return '';
    if (currentPref === 'required') {
        return `
            <div class="settings-pref-required" title="Core component — cannot be opted out">
                Required (no opt-out)
            </div>
        `;
    }
    const disabledRo = WB_READ_ONLY_MODE ? 'disabled title="Dashboard is in read-only mode"' : '';
    const btn = (label, value, isActive) => `
        <button type="button"
                class="settings-pref-btn ${isActive ? 'active' : ''}"
                data-component="${escapeHtml(componentId)}"
                data-value="${value}"
                onclick="onPreferenceClick(this)" ${disabledRo}>
            ${escapeHtml(label)}
        </button>
    `;
    return `
        <div class="settings-pref-controls" role="group" aria-label="Set preference for ${escapeHtml(componentId)}">
            ${btn('Want', 'true', currentPref === 'wanted')}
            ${btn('No thanks', 'false', currentPref === 'unwanted')}
            ${btn('Undecided', 'null', currentPref === 'undecided' || !currentPref)}
        </div>
    `;
}

// Populated once at load from /api/state — the Settings tab honors this.
let WB_READ_ONLY_MODE = false;
(async function initReadOnlyFlag() {
    try {
        const resp = await fetch('/api/state');
        const data = await resp.json();
        WB_READ_ONLY_MODE = !!data.read_only;
    } catch (e) {}
})();

async function onPreferenceClick(btnEl) {
    if (WB_READ_ONLY_MODE) return;
    const componentId = btnEl.dataset.component;
    const rawValue = btnEl.dataset.value;
    let wanted;
    if (rawValue === 'true') wanted = true;
    else if (rawValue === 'false') wanted = false;
    else wanted = null;

    // Optimistic UI: mark buttons as pending
    const siblings = btnEl.parentElement.querySelectorAll('.settings-pref-btn');
    siblings.forEach(b => { b.classList.remove('active'); b.disabled = true; });
    btnEl.classList.add('pending');

    try {
        const resp = await fetch('/api/control/preference', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                updates: { [componentId]: { wanted: wanted } },
            }),
        });
        if (!resp.ok) {
            const err = await resp.text();
            showToast(`Preference update failed: ${err}`, 'error');
            return;
        }
        const data = await resp.json();
        WB_CONTROL_GRAPH = { nodes: data.nodes, cache: data.cache };
        renderSettingsTree();
        renderSettingsSummary();
        showToast(`Preference saved for ${componentId}`, 'success');
    } catch (exc) {
        showToast(`Preference update failed: ${exc}`, 'error');
    } finally {
        // If render didn't run (error path), re-enable the buttons
        siblings.forEach(b => { b.disabled = false; });
        btnEl.classList.remove('pending');
    }
}

// Minimal toast fallback in case the shared one isn't loaded yet
function showToast(msg, kind) {
    const container = document.getElementById('toast-container');
    if (!container) { console.log('[toast]', kind, msg); return; }
    const el = document.createElement('div');
    el.className = 'toast toast-' + (kind || 'info');
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => el.remove(), 4000);
}

// ---- Summary row ----
function renderSettingsSummary() {
    const el = document.getElementById('settings-summary');
    if (!el || !WB_CONTROL_GRAPH) return;
    const nodes = Object.values(WB_CONTROL_GRAPH.nodes);
    const counts = {};
    const kindCounts = {};
    for (const n of nodes) {
        counts[n.effective_state] = (counts[n.effective_state] || 0) + 1;
        kindCounts[n.kind] = (kindCounts[n.kind] || 0) + 1;
    }
    const cacheAge = WB_CONTROL_GRAPH.cache && WB_CONTROL_GRAPH.cache.age_seconds;
    const kindsTxt = Object.entries(kindCounts)
        .map(([k, v]) => `${v} ${k}`)
        .join(' · ');
    const statesTxt = ['ok','degraded','blocked','unconfigured','disabled','unknown']
        .filter(s => counts[s])
        .map(s => `${controlStateBadge(s)} ${counts[s]}`)
        .join(' ');
    el.innerHTML = `
        <div class="settings-summary-row">
            <div class="settings-summary-kinds">${kindsTxt}</div>
            <div class="settings-summary-states">${statesTxt}</div>
            <div class="settings-summary-cache">Cache age: ${cacheAge != null ? cacheAge.toFixed(0) + 's' : 'fresh'}</div>
        </div>
    `;
}

// ---- Tree rendering ----
function _matchesFilter(node) {
    if (!WB_CONTROL_FILTER) return true;
    const f = WB_CONTROL_FILTER;
    if ((node.label || '').toLowerCase().includes(f)) return true;
    if ((node.id || '').toLowerCase().includes(f)) return true;
    return false;
}

function _childrenOf(nodes, parentId) {
    return Object.values(nodes).filter(n => (n.grouping_parents || []).includes(parentId));
}

function _renderRequirementList(nodes, reqIds) {
    if (!reqIds || reqIds.length === 0) return '';
    const passing = reqIds.filter(rid => nodes[rid] && nodes[rid].effective_state === 'ok').length;
    const total = reqIds.length;
    // Key by the sorted requirement id list so restoration survives
    // re-renders even if a parent node's position shifts.
    const detailKey = 'req:' + reqIds.slice().sort().join(',');
    return `
        <details class="settings-req-details" data-wb-detail-key="${escapeHtml(detailKey)}">
            <summary>Requirements (${passing}/${total} ok)</summary>
            <ul class="settings-req-list">
                ${reqIds.map(rid => {
                    const r = nodes[rid];
                    if (!r) return `<li class="settings-req-item muted">${escapeHtml(rid)} — not registered</li>`;
                    return `<li class="settings-req-item">
                        ${controlStateBadge(r.effective_state)}
                        <span class="settings-req-label">${escapeHtml(r.label)}</span>
                        ${r.status_reason ? `<span class="settings-req-reason">${escapeHtml(r.status_reason)}</span>` : ''}
                    </li>`;
                }).join('')}
            </ul>
        </details>
    `;
}

function _renderCapabilityList(capNames) {
    if (!capNames || capNames.length === 0) return '';
    const detailKey = 'cap:' + capNames.slice().sort().join(',');
    return `
        <details class="settings-cap-details" data-wb-detail-key="${escapeHtml(detailKey)}">
            <summary>Affects ${capNames.length} capabilities</summary>
            <div class="settings-cap-chips">
                ${capNames.map(n => `<span class="settings-cap-chip">${escapeHtml(n)}</span>`).join('')}
            </div>
        </details>
    `;
}

function _renderDependencyChips(nodes, deps) {
    if (!deps || deps.length === 0) return '';
    return `<div class="settings-dep-chips">
        <span class="settings-dep-label">depends on:</span>
        ${deps.map(e => {
            const target = nodes[e.target_id];
            const state = target ? target.effective_state : 'unknown';
            const label = target ? target.label : e.target_id;
            return `<span class="settings-dep-chip" title="${escapeHtml(e.target_id)}">${controlStateBadge(state)} ${escapeHtml(label)}</span>`;
        }).join('')}
    </div>`;
}

function _renderAlsoIn(node, currentParent) {
    const others = (node.grouping_parents || []).filter(p => p !== currentParent);
    if (others.length === 0) return '';
    return `<span class="settings-also-in" title="Also appears under: ${escapeHtml(others.join(', '))}">also in ${others.length}</span>`;
}

function _renderComponentNode(nodes, node, underParent) {
    const reqCount = (node.requirement_ids || []).length;
    const capCount = (node.affects_capabilities || []).length;
    const matched = _matchesFilter(node);
    if (!matched && !WB_CONTROL_FILTER) {
        // Always show when filter is empty
    } else if (!matched) {
        // Filter on — still render if any descendant matches (simple approach: skip self, UI will collapse)
        // For Phase E keep it simple: match on self only.
        return '';
    }
    return `
        <div class="settings-node settings-component" data-state="${node.effective_state}" data-kind="component">
            <div class="settings-node-header">
                <span class="settings-node-kind">COMPONENT</span>
                <span class="settings-node-label">${escapeHtml(node.label)}</span>
                ${controlStateBadge(node.effective_state)}
                ${preferenceBadge(node.preference)}
                ${_renderAlsoIn(node, underParent)}
            </div>
            ${node.status_reason ? `<div class="settings-node-reason">${escapeHtml(node.status_reason)}</div>` : ''}
            ${preferenceToggleControls(node.component_id, node.preference)}
            ${_renderDependencyChips(nodes, node.dependencies)}
            ${_renderRequirementList(nodes, node.requirement_ids)}
            ${_renderCapabilityList(node.affects_capabilities)}
        </div>
    `;
}

function _renderSubsystemNode(nodes, node) {
    const matched = _matchesFilter(node);
    if (WB_CONTROL_FILTER && !matched) return '';
    return `
        <div class="settings-node settings-subsystem" data-state="${node.effective_state}" data-kind="subsystem">
            <div class="settings-node-header">
                <span class="settings-node-kind">SUBSYSTEM</span>
                <span class="settings-node-label">${escapeHtml(node.label)}</span>
                ${controlStateBadge(node.effective_state)}
            </div>
            <div class="settings-node-desc">${escapeHtml(node.description || '')}</div>
            ${_renderDependencyChips(nodes, node.dependencies)}
            ${_renderRequirementList(nodes, node.requirement_ids)}
        </div>
    `;
}

function _renderDomainNode(nodes, domainId) {
    const domain = nodes[domainId];
    if (!domain) return '';

    // Direct children: subsystems + components whose grouping_parents include this domain
    const children = _childrenOf(nodes, domainId)
        .filter(c => c.kind === 'subsystem' || c.kind === 'component');

    // Sort: subsystems before components, each alphabetical
    children.sort((a, b) => {
        if (a.kind !== b.kind) return a.kind === 'subsystem' ? -1 : 1;
        return (a.label || '').localeCompare(b.label || '');
    });

    const childrenHtml = children.map(c => {
        if (c.kind === 'subsystem') return _renderSubsystemNode(nodes, c);
        if (c.kind === 'component') return _renderComponentNode(nodes, c, domainId);
        return '';
    }).filter(Boolean).join('');

    const matched = _matchesFilter(domain) || childrenHtml.length > 0;
    if (WB_CONTROL_FILTER && !matched) return '';

    // Default open for problematic domains (anything that isn't ok/disabled)
    const shouldBeOpen = !['ok', 'disabled'].includes(domain.effective_state);

    return `
        <details class="settings-domain" ${shouldBeOpen ? 'open' : ''} data-state="${domain.effective_state}" data-wb-detail-key="${escapeHtml(domain.id)}">
            <summary class="settings-domain-header">
                <span class="settings-domain-label">${escapeHtml(domain.label)}</span>
                ${controlStateBadge(domain.effective_state)}
                <span class="settings-domain-count">${children.length} child${children.length === 1 ? '' : 'ren'}</span>
            </summary>
            <div class="settings-domain-desc">${escapeHtml(domain.description || '')}</div>
            <div class="settings-domain-body">
                ${childrenHtml || '<div class="empty-state">No visible children.</div>'}
            </div>
        </details>
    `;
}

// Snapshot the open-state of every <details> in the settings tree before
// wholesale-replacing innerHTML, and restore after. This preserves the
// user's drill-down context when anything triggers a re-render (preference
// toggle, manual Rebuild). Uses data-wb-detail-key attributes rather than
// DOM position so restoration is robust to node-order changes.
function _snapshotDetailsState(container) {
    const state = {};
    container.querySelectorAll('details[data-wb-detail-key]').forEach(el => {
        state[el.dataset.wbDetailKey] = el.open;
    });
    return state;
}

function _restoreDetailsState(container, state) {
    if (!state) return;
    container.querySelectorAll('details[data-wb-detail-key]').forEach(el => {
        const key = el.dataset.wbDetailKey;
        if (key in state) {
            el.open = state[key];
        }
    });
}

function renderSettingsTree() {
    const container = document.getElementById('settings-tree');
    if (!container) return;
    if (!WB_CONTROL_GRAPH) {
        container.innerHTML = '<div class="loading">Loading...</div>';
        return;
    }

    // Snapshot BEFORE we rebuild — we restore after.
    const priorState = _snapshotDetailsState(container);

    const nodes = WB_CONTROL_GRAPH.nodes;

    // Render user-facing domains first in a specific order, then everything else
    const domainOrder = [
        'domain:journal',
        'domain:notifications',
        'domain:knowledge',
        'domain:browser',
        'domain:calendar',
        'domain:runtime',
        'domain:system',
    ];
    const rendered = new Set();
    const domainHtml = domainOrder
        .filter(id => nodes[id])
        .map(id => { rendered.add(id); return _renderDomainNode(nodes, id); })
        .join('');

    // Any domain not in the canonical order — render at the end
    const extras = Object.values(nodes)
        .filter(n => n.kind === 'domain' && !rendered.has(n.id))
        .map(d => _renderDomainNode(nodes, d.id))
        .join('');

    container.innerHTML = domainHtml + extras;

    // Restore — any <details> with a key that was open before stays open.
    _restoreDetailsState(container, priorState);
}
"""
