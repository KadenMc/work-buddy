"""Dashboard JS for the Settings tab.

Renders the control graph produced by ``GET /api/control/graph`` as a
hierarchy of domains → subsystems/components → requirements + affected
capabilities. Phase E is read-only; Phase F will wire preference
toggles into a ``POST /api/control/preference`` endpoint.

The renderer reuses ``statusBadge()`` from ``core/page.py`` but
extends the mapping for ``unconfigured`` and preference labels.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Settings tab: unified control graph ----

// Internal state
let WB_CONTROL_GRAPH = null;
let WB_CONTROL_FILTER = '';
// When true, the summary row renders an inline "Disabled components"
// panel listing everything currently opted out. Toggled by clicking
// the disabled chip.
let WB_SHOW_DISABLED_LIST = false;
// Set of node ids visible under the current filter. Computed from the
// graph on every filter change. A node is visible if its own label/id
// matches OR any transitive child/requirement/cap-chip matches.
// Without this, filtering "telegram" would hide the Notifications
// domain even though Telegram lives inside it.
let WB_MATCHED_SET = null;

// ---- Settings sub-tabs: Status (control graph) | Activity (bridge + logs) ----
//
// The Settings tab hosts two sub-views. 'status' is the control graph
// (default). 'activity' is the bridge sparkline + event log +
// notification log. Switching is purely client-side; the active sub-tab
// is persisted to the URL hash via the `st` key (see core/page.py).
let WB_SETTINGS_SUBTAB = 'status';

function switchSettingsSubtab(st) {
    if (st !== 'status' && st !== 'activity' && st !== 'embeddings' && st !== 'inference') st = 'status';
    WB_SETTINGS_SUBTAB = st;
    document.querySelectorAll('.settings-subtab-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.st === st));
    document.querySelectorAll('.settings-subtab-panel').forEach(p =>
        p.classList.toggle('active', p.id === 'ssp-' + st));
    if (typeof _persistHash === 'function') _persistHash();
    // Lazy-load sub-views on first switch — panels ship with placeholder
    // loading divs until their loader populates them.
    if (st === 'activity' && !window._activityLoaded
        && typeof loadActivity === 'function') {
        loadActivity();
    }
    if (st === 'embeddings' && typeof loadEmbeddings === 'function') {
        loadEmbeddings();  // cheap (cached, pre-warmed) — refresh on each open
    }
    if (st === 'inference' && typeof loadInference === 'function') {
        loadInference();  // cheap (cached) — refresh on open; broker.state SSE keeps it live
    }
}

// At-a-glance system cards (uptime / services healthy / jobs / last
// tick) pinned to the top of the Status sub-view. Sourced from the
// cached /api/state snapshot that loadSettings already fetches.
function renderStatusCards() {
    const el = document.getElementById('settings-status-cards');
    if (!el) return;
    const data = window._WB_LAST_STATE;
    if (!data) { el.innerHTML = ''; return; }
    const services = Object.values(data.services || {});
    const healthy = services.filter(s => s.status === 'healthy').length;
    el.innerHTML = `
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

async function loadSettings(force) {
    const tree = document.getElementById('settings-tree');
    const summary = document.getElementById('settings-summary');
    if (!tree) return;
    if (force) tree.innerHTML = '<div class="loading">Rebuilding control graph...</div>';

    try {
        const url = '/api/control/graph' + (force ? '?force=1' : '');
        // Fetch the control graph and /api/state in parallel. The latter
        // feeds the per-component event chips and the at-a-glance status
        // cards (both Status sub-view) — optional decoration. fetchJSON's
        // timeout abandons a slow or hung /api/state so it can never
        // block the graph render (a plain .catch covers only rejection,
        // not a request that hangs without ever resolving).
        const [resp, state] = await Promise.all([
            fetch(url),
            fetchJSON('/api/state', null, 5000),
        ]);
        if (!resp.ok) {
            tree.innerHTML = `<div class="error-state">Failed to load (${resp.status}): ${await resp.text()}</div>`;
            return;
        }
        const data = await resp.json();
        WB_CONTROL_GRAPH = data;
        if (state) {
            window._WB_LAST_STATE = state;
            window._WB_EVENT_COUNTS = state.event_counts_by_source || {};
        }
        _rebuildComponentEventIndex();
        renderStatusCards();
        WB_MATCHED_SET = _computeMatchedSet();
        renderSettingsTree();
        renderSettingsSummary();
        // Restore sub-tab from the URL hash on first load. Consume the
        // key once so a later SSE-driven loadSettings() doesn't override
        // a manual sub-tab switch the user made in the meantime.
        if (window._urlState && window._urlState.st
            && typeof switchSettingsSubtab === 'function') {
            switchSettingsSubtab(window._urlState.st);
            delete window._urlState.st;
        }
    } catch (exc) {
        tree.innerHTML = `<div class="error-state">Error loading control graph: ${escapeHtml(String(exc))}</div>`;
    }
}

// ---- Reprobe-all button ----
//
// Runs every registered tool probe from scratch via
// POST /api/control/reprobe (backed by tools.probe_all(force=True)),
// then renders the fresh graph. Unlike loadSettings(true) which only
// busts the 45-s graph cache, this one actually re-pings every
// service. Worst-case ~10s when Obsidian or Hindsight are slow —
// button shows a spinner and blocks re-entry until done.
async function reprobeAll(btnEl) {
    if (WB_READ_ONLY_MODE) return;
    const orig = btnEl.textContent;
    btnEl.disabled = true;
    btnEl.textContent = 'Probing…';
    const tree = document.getElementById('settings-tree');
    if (tree) tree.innerHTML = '<div class="loading">Reprobing every tool (up to ~10s)…</div>';
    try {
        const resp = await fetch('/api/control/reprobe', {method: 'POST'});
        if (!resp.ok) {
            const errText = await resp.text();
            settingsToast(`Reprobe failed: ${errText}`, 'error');
            return;
        }
        const data = await resp.json();
        WB_CONTROL_GRAPH = data;
        _rebuildComponentEventIndex();
        renderSettingsTree();
        renderSettingsSummary();
        settingsToast('Probes refreshed.', 'success');
    } catch (exc) {
        settingsToast(`Reprobe request failed: ${exc}`, 'error');
    } finally {
        btnEl.disabled = false;
        btnEl.textContent = orig;
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
            WB_MATCHED_SET = _computeMatchedSet();
            renderSettingsTree();
        }, 150);
    });
});

// Build the set of node ids that should remain visible given the
// current filter. A node is visible iff its label or id matches, OR
// any of its transitive children/requirements/dep-targets matches.
// Returns null when the filter is empty (= "show everything").
function _computeMatchedSet() {
    if (!WB_CONTROL_FILTER || !WB_CONTROL_GRAPH) return null;
    const f = WB_CONTROL_FILTER;
    const nodes = WB_CONTROL_GRAPH.nodes;

    const directHits = new Set();
    for (const n of Object.values(nodes)) {
        if ((n.label || '').toLowerCase().includes(f) ||
            (n.id || '').toLowerCase().includes(f)) {
            directHits.add(n.id);
        }
        // affects_capabilities entries are capability names (strings,
        // not node ids). If the filter matches one of them, surface
        // THIS node (usually a component) so the user can find the
        // affected component when searching by capability.
        const affects = n.affects_capabilities || [];
        if (affects.some(capName => capName.toLowerCase().includes(f))) {
            directHits.add(n.id);
            // Also surface the capability node itself so its details
            // chip becomes part of the visible tree if we ever render it.
            const capNode = nodes[`cap:${affects.find(c => c.toLowerCase().includes(f))}`];
            if (capNode) directHits.add(capNode.id);
        }
    }
    if (directHits.size === 0) return new Set();

    // Expand: include every ancestor (via grouping_parents) and every
    // node that references a direct hit through dependency or
    // requirement edges. Iterate until fixed point.
    const visible = new Set(directHits);
    let grew = true;
    while (grew) {
        grew = false;
        for (const n of Object.values(nodes)) {
            if (visible.has(n.id)) continue;
            // A node is visible if any of its children is visible.
            // "Child" = node whose grouping_parents includes this node,
            // OR a requirement listed in this node's requirement_ids,
            // OR a dependency target listed in this node's dependencies.
            const hasVisibleChild =
                Object.values(nodes).some(o =>
                    (o.grouping_parents || []).includes(n.id) &&
                    visible.has(o.id)
                )
                || (n.requirement_ids || []).some(rid => visible.has(rid))
                || (n.dependencies || []).some(e => visible.has(e.target_id));
            if (hasVisibleChild) {
                visible.add(n.id);
                grew = true;
            }
        }
    }
    return visible;
}

function _isVisible(node) {
    if (!WB_MATCHED_SET) return true;  // no filter
    return WB_MATCHED_SET.has(node.id);
}

// ---- Badge helper: extend core/page's statusBadge for control-graph states ----
//
// Non-ok badges are clickable: clicking walks the graph downward from
// the owning node, expands all ancestors, scrolls to the first bad
// descendant, and briefly flashes it. Ok/disabled badges are not
// clickable — nothing useful to drill into.
function controlStateBadge(state, nodeId) {
    const map = {
        ok: 'badge-green',
        degraded: 'badge-yellow',
        blocked: 'badge-red',
        unconfigured: 'badge-yellow',
        disabled: 'badge-muted',
        unknown: 'badge-muted',
    };
    const clickable = nodeId && ['degraded', 'blocked', 'unconfigured', 'unknown'].includes(state);
    if (clickable) {
        return `<span class="badge badge-clickable ${map[state] || 'badge-muted'}" ` +
            `data-drilldown-node="${escapeHtml(nodeId)}" ` +
            `title="Click to find what's wrong under this node" ` +
            `onclick="onBadgeDrilldown(event)">${state}</span>`;
    }
    return `<span class="badge ${map[state] || 'badge-muted'}">${state}</span>`;
}

// ---- Drill-down: from a non-ok node, find the source of its problem ----
// Walks the node's descendants (via grouping_parents and dependencies),
// collects every non-ok node, opens all ancestor <details>, scrolls to
// the first match, and flashes it.
function onBadgeDrilldown(ev) {
    ev.stopPropagation();
    ev.preventDefault();
    const badge = ev.currentTarget;
    const rootId = badge.dataset.drilldownNode;
    if (!rootId || !WB_CONTROL_GRAPH) return;
    const nodes = WB_CONTROL_GRAPH.nodes;

    // Gather every node reachable downward from rootId that is not-ok.
    // Visit via grouping_parents (who claims this as a child) AND via
    // dependencies (what this node needs) — the problem may live in
    // either. Skip `ok`/`disabled` — those aren't the problem.
    const bad = [];
    const visited = new Set();
    const stack = [rootId];
    const badStates = new Set(['degraded', 'blocked', 'unconfigured', 'unknown']);
    while (stack.length) {
        const nid = stack.pop();
        if (visited.has(nid)) continue;
        visited.add(nid);
        const n = nodes[nid];
        if (!n) continue;
        if (nid !== rootId && badStates.has(n.effective_state)) {
            bad.push(n);
        }
        // Descend via grouping — all nodes that list nid as their parent
        for (const other of Object.values(nodes)) {
            if ((other.grouping_parents || []).includes(nid)) {
                stack.push(other.id);
            }
        }
        // Also via requirement_ids + dependency targets — these capture
        // the rest of the "things I need to be ok" surface.
        for (const rid of (n.requirement_ids || [])) {
            if (!visited.has(rid)) stack.push(rid);
        }
        for (const e of (n.dependencies || [])) {
            if (!visited.has(e.target_id)) stack.push(e.target_id);
        }
    }

    if (bad.length === 0) {
        settingsToast(`${rootId} reports ${nodes[rootId].effective_state} but no bad descendant found — check the status reason directly.`, 'info');
        return;
    }

    // Pick the most severe / leafmost one to scroll to. Prefer
    // blocked > unconfigured > degraded > unknown.
    const severity = {blocked: 0, unconfigured: 1, degraded: 2, unknown: 3};
    bad.sort((a, b) => (severity[a.effective_state] ?? 99) - (severity[b.effective_state] ?? 99));
    const target = bad[0];

    // Open all <details> ancestors of the target so it's visible.
    _expandAncestorsOf(target.id);
    _flashNode(target.id, bad.length);
}

function _expandAncestorsOf(nodeId) {
    // Walk up grouping_parents and open every matching <details>.
    if (!WB_CONTROL_GRAPH) return;
    const nodes = WB_CONTROL_GRAPH.nodes;
    const opened = new Set();
    const walk = (nid) => {
        if (opened.has(nid)) return;
        opened.add(nid);
        // Open the <details> for this node if it has one
        const d = document.querySelector(`details[data-wb-detail-key="${CSS.escape(nid)}"]`);
        if (d) d.open = true;
        const n = nodes[nid];
        if (!n) return;
        for (const p of (n.grouping_parents || [])) walk(p);
    };
    walk(nodeId);

    // Also open any <details> whose req list includes this node, or
    // whose cap chip list includes it. (Those <details> are keyed by
    // sorted-id-list, so the membership check is substring-ish.)
    document.querySelectorAll('details[data-wb-detail-key^="req:"]').forEach(d => {
        const key = d.dataset.wbDetailKey || '';
        // key format: "req:sort1,sort2,..."
        const ids = key.slice(4).split(',');
        const stripped = nodeId.startsWith('req:') ? nodeId.slice(4) : nodeId;
        if (ids.includes(stripped) || ids.includes(nodeId)) d.open = true;
    });
}

function _flashNode(nodeId, siblingCount) {
    // Scroll to the first DOM element tagged with this node id, and
    // briefly add a flash class. For requirement nodes we look inside
    // the req list items; for domains/subsystems/components we look at
    // the panel root.
    let el = document.querySelector(`[data-wb-node-id="${CSS.escape(nodeId)}"]`);
    if (!el) {
        // Fallback: find a <details> with this key
        el = document.querySelector(`details[data-wb-detail-key="${CSS.escape(nodeId)}"]`);
    }
    if (!el) return;
    el.scrollIntoView({behavior: 'smooth', block: 'center'});
    el.classList.add('wb-flash');
    setTimeout(() => el.classList.remove('wb-flash'), 1800);
    if (siblingCount > 1) {
        settingsToast(`Showing worst of ${siblingCount} issues — badges above lead to others.`, 'info');
    }
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
            settingsToast(`Preference update failed: ${err}`, 'error');
            return;
        }
        const data = await resp.json();
        WB_CONTROL_GRAPH = { nodes: data.nodes, cache: data.cache };
        WB_MATCHED_SET = _computeMatchedSet();
        renderSettingsTree();
        renderSettingsSummary();
        settingsToast(`Preference saved for ${componentId}`, 'success');
    } catch (exc) {
        settingsToast(`Preference update failed: ${exc}`, 'error');
    } finally {
        // If render didn't run (error path), re-enable the buttons
        siblings.forEach(b => { b.disabled = false; });
        btnEl.classList.remove('pending');
    }
}

// Simple status toast for the Settings tab (fix results, reprobe,
// preference saves). Deliberately NOT named `showToast`: core/
// notifications.py owns `window.showToast(title, body, ..., view, ...)`
// for notification cards, and all frontend scripts concatenate into one
// scope — naming this `showToast` lets the notifications version shadow
// it, and calling it with `(msg, kind)` then throws on `view.short_id`.
function settingsToast(msg, kind) {
    const container = document.getElementById('toast-container');
    if (!container) { console.log('[toast]', kind, msg); return; }
    const el = document.createElement('div');
    el.className = 'toast toast-' + (kind || 'info');
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => el.remove(), 4000);
}

// ---- Summary row ----
// Goals in priority order:
//   1. Tell the user "is anything wrong?" at a glance.
//   2. If yes, name the worst things and give one-click navigation.
//   3. Otherwise, stay out of the way — no wall of counts for healthy state.
function renderSettingsSummary() {
    const el = document.getElementById('settings-summary');
    if (!el || !WB_CONTROL_GRAPH) return;
    const nodes = Object.values(WB_CONTROL_GRAPH.nodes);

    // Identify user-facing worst offenders: domain/subsystem/component
    // nodes in a non-ok, non-disabled state, ranked by severity. We
    // skip requirement + capability nodes here — they're noise at this
    // level; users see them by expanding the parent.
    const severity = {blocked: 0, unconfigured: 1, degraded: 2, unknown: 3};
    const topLevel = nodes.filter(n =>
        ['domain', 'subsystem', 'component'].includes(n.kind)
        && ['blocked', 'unconfigured', 'degraded', 'unknown'].includes(n.effective_state)
    );
    topLevel.sort((a, b) => {
        const sa = severity[a.effective_state] ?? 99;
        const sb = severity[b.effective_state] ?? 99;
        if (sa !== sb) return sa - sb;
        // Tiebreak: prefer more specific nodes (component > subsystem > domain)
        // so users jump to actionable leaves rather than rolled-up domains.
        const kindRank = {component: 0, subsystem: 1, domain: 2};
        return (kindRank[a.kind] ?? 9) - (kindRank[b.kind] ?? 9);
    });
    const topIssues = topLevel.slice(0, 3);

    const cacheAge = WB_CONTROL_GRAPH.cache && WB_CONTROL_GRAPH.cache.age_seconds;
    const cacheStr = cacheAge != null ? `${cacheAge.toFixed(0)}s old` : 'fresh';

    // Always-present: totals + cache freshness. Small, unobtrusive.
    const totals = {ok: 0, degraded: 0, blocked: 0, unconfigured: 0, disabled: 0, unknown: 0};
    for (const n of nodes) {
        if (n.kind === 'capability') continue;  // exclude capability noise
        totals[n.effective_state] = (totals[n.effective_state] || 0) + 1;
    }
    // Chip behaviors:
    //   - Non-ok states (blocked/unconfigured/degraded/unknown): click
    //     jumps to the first node in that state and flashes it.
    //   - disabled: click toggles an inline list of what's disabled
    //     (users want to see what they've opted out of without digging).
    //   - ok: informational only (rarely actionable to see "which 42
    //     things are healthy"); kept static.
    const jumpStates = new Set(['blocked', 'unconfigured', 'degraded', 'unknown']);
    const summaryChips = ['blocked', 'unconfigured', 'degraded', 'unknown', 'ok', 'disabled']
        .filter(s => totals[s])
        .map(s => {
            const cls = _badgeClass(s);
            if (jumpStates.has(s)) {
                return (
                    `<span class="badge badge-clickable ${cls}" ` +
                    `onclick="onStateChipClick('${s}')" ` +
                    `title="Click to jump to the first node in this state">${s}</span> ` +
                    `<span class="settings-summary-count">${totals[s]}</span>`
                );
            }
            if (s === 'disabled') {
                const pressed = WB_SHOW_DISABLED_LIST ? ' pressed' : '';
                return (
                    `<span class="badge badge-clickable ${cls}${pressed}" ` +
                    `onclick="onDisabledChipClick()" ` +
                    `title="${WB_SHOW_DISABLED_LIST ? 'Click to hide the disabled-items list' : 'Click to list what is currently disabled'}">${s}</span> ` +
                    `<span class="settings-summary-count">${totals[s]}</span>`
                );
            }
            return (
                `<span class="badge ${cls}">${s}</span> ` +
                `<span class="settings-summary-count">${totals[s]}</span>`
            );
        })
        .join('');

    let html = `
        <div class="settings-summary-row">
            <div class="settings-summary-totals">${summaryChips}</div>
            <div class="settings-summary-cache" title="Graph TTL is 45s; click Reprobe all to re-run every probe and rebuild from scratch.">${cacheStr}</div>
        </div>
    `;

    // Inline "what's disabled" panel — renders between the chip row
    // and the top-issues/all-ok line when the user toggles it on.
    if (WB_SHOW_DISABLED_LIST && totals.disabled > 0) {
        const kindRank = {component: 0, subsystem: 1, domain: 2, requirement: 3};
        const disabledNodes = nodes
            .filter(n => n.effective_state === 'disabled' && n.kind !== 'capability')
            .sort((a, b) => (kindRank[a.kind] ?? 99) - (kindRank[b.kind] ?? 99));
        const rows = disabledNodes.map(n => {
            // What made it disabled? Walk: if this is a component with
            // preference=unwanted, say so; if it's required (core),
            // that's impossible (core can't be disabled); otherwise
            // it's a cascade from a disabled hard-dep.
            let why = '';
            if (n.preference === 'unwanted') {
                why = 'opted out';
            } else if ((n.blocking_issues || []).length) {
                why = `dep disabled: ${n.blocking_issues.join(', ')}`;
            } else {
                why = n.status_reason || '';
            }
            return `
                <div class="settings-summary-issue" onclick="onBadgeDrilldown({currentTarget:{dataset:{drilldownNode:'${escapeHtml(n.id)}'}},stopPropagation:()=>{},preventDefault:()=>{}})" title="Click to jump to this node">
                    <span class="badge badge-muted">${escapeHtml(n.kind)}</span>
                    <span class="settings-summary-issue-label">${escapeHtml(n.label)}</span>
                    ${why ? `<span class="settings-summary-issue-reason">${escapeHtml(why)}</span>` : ''}
                </div>
            `;
        }).join('');
        html += `
            <div class="settings-summary-issues-header">Disabled (${disabledNodes.length})</div>
            <div class="settings-summary-issues">${rows}</div>
        `;
    }

    if (topIssues.length > 0) {
        const issueRows = topIssues.map(n => `
            <div class="settings-summary-issue" onclick="onBadgeDrilldown({currentTarget: {dataset: {drilldownNode: '${escapeHtml(n.id)}'}}, stopPropagation:()=>{}, preventDefault:()=>{}})" title="Click to jump to this issue">
                ${controlStateBadge(n.effective_state, n.id)}
                <span class="settings-summary-issue-label">${escapeHtml(n.label)}</span>
                ${n.status_reason ? `<span class="settings-summary-issue-reason">${escapeHtml(n.status_reason)}</span>` : ''}
            </div>
        `).join('');
        const moreCount = topLevel.length - topIssues.length;
        html += `
            <div class="settings-summary-issues-header">
                Top ${topIssues.length} issue${topIssues.length === 1 ? '' : 's'}${moreCount > 0 ? ` (+${moreCount} more below)` : ''}
            </div>
            <div class="settings-summary-issues">${issueRows}</div>
        `;
    } else {
        html += `<div class="settings-summary-all-ok">All domains, subsystems, and components are healthy.</div>`;
    }

    el.innerHTML = html;
}

// ---- Per-requirement action buttons (Fix + Help) ----
//
// Two universal actions on every requirement that's not currently ok:
//
//   * Fix — present iff the requirement declared a fix_kind != "none".
//     Programmatic: confirm popover with fix_preview, then one-click apply.
//     Input-required: inline form with fields from fix_params, submit applies.
//     Agent-handoff: confirm popover, then spawns a Claude Code session.
//
//   * ? — always present (when not ok). Spawns a help-agent session with
//     a structured brief. Replaces the legacy Status-tab `🪄 /wb-setup
//     diagnose` hint.
// Shared data-* attributes for any button that opens the fix flow
// (onFixClick reads them). data-fix-params is single-quoted because the
// JSON value is full of double quotes; escapeHtml only covers < > & so
// apostrophes inside fix_params text would close the attribute early —
// hence the explicit ' -> &#39; pass.
function _fixDataAttrs(r) {
    return `data-req-id="${escapeHtml(r.id.replace(/^req:/, ''))}" ` +
        `data-fix-kind="${escapeHtml(r.fix_kind)}" ` +
        `data-fix-preview="${escapeHtml(r.fix_preview || '')}" ` +
        `data-fix-params='${escapeHtml(JSON.stringify(r.fix_params || {})).replace(/'/g, '&#39;')}'`;
}

function _renderRequirementActions(r) {
    if (r.effective_state === 'disabled') {
        return '';
    }
    // A satisfied (ok) requirement has no fix to apply — but an
    // input_required one is still an editable setting (timezone, backup
    // repo, …). Offer a subtle pencil to change its value; other ok
    // requirements get no action.
    if (r.effective_state === 'ok') {
        if (r.fix_kind !== 'input_required') return '';
        const roDisabled = WB_READ_ONLY_MODE
            ? 'disabled title="Dashboard is in read-only mode"' : '';
        return `<span class="settings-req-actions">` +
            `<button class="settings-edit-btn" type="button" ` +
            `onclick="onFixClick(this)" ${_fixDataAttrs(r)} ${roDisabled} ` +
            `title="Change this setting">✎</button></span>`;
    }
    // Two user-facing verbs:
    //   * "Configure" covers BOTH programmatic and input_required.
    //     Internally, clicking it opens an inline panel: if the fix
    //     needs input, the panel is a form; if not, the panel is a
    //     preview-with-Apply-button so the user confirms the auto-
    //     change before it happens. Same intent ("configure this
    //     requirement to pass"), just branches on whether we need
    //     values from the user.
    //   * "Walk me through" means we're handing off to a Claude Code
    //     session. Kept distinct because the side-effect (a new
    //     terminal opens) is categorically different.
    let fixLabel = 'Fix';
    let fixClass = 'settings-fix-btn';
    let fixTitle = 'Apply the registered fix for this requirement';
    if (r.fix_kind === 'programmatic' || r.fix_kind === 'input_required') {
        fixLabel = 'Configure';
        fixTitle = (r.fix_kind === 'input_required'
            ? 'Opens a form to collect the needed values, then applies. '
            : 'Opens a preview of what will change, then applies on your confirm. ')
            + (r.fix_preview || '');
    } else if (r.fix_kind === 'agent_handoff') {
        fixLabel = 'Walk me through';
        fixClass += ' settings-fix-btn-agent';
        fixTitle = 'Opens a Claude Code session to walk you through this. ' + (r.fix_preview || '');
    }
    const fixBtn = r.fix_kind && r.fix_kind !== 'none'
        ? `<button class="${fixClass}" type="button"
                   onclick="onFixClick(this)"
                   ${_fixDataAttrs(r)}
                   ${WB_READ_ONLY_MODE ? 'disabled title="Dashboard is in read-only mode"' : ''}
                   title="${escapeHtml(fixTitle.trim())}">${escapeHtml(fixLabel)}</button>`
        : '';
    // "?" = spawns an agent briefed to explain/diagnose. Hidden on
    // agent_handoff requirements because "Walk me through" already
    // spawns a session for the same kind of requirement — a second
    // button that also opens a terminal would be redundant.
    //
    // Kept on programmatic / input_required requirements where it's
    // a genuinely distinct action: "?" explains, Configure applies.
    const showHelp = r.fix_kind !== 'agent_handoff';
    const helpBtn = showHelp
        ? `<button class="settings-help-btn settings-help-btn-alert" type="button"
                    onclick="onHelpClick(this)"
                    data-node-id="${escapeHtml(r.id)}"
                    ${WB_READ_ONLY_MODE ? 'disabled' : ''}
                    title="Spawn a Claude Code session focused on this requirement. Use when you want to understand or investigate rather than auto-apply a fix.">?</button>`
        : '';
    return `<span class="settings-req-actions">${fixBtn}${helpBtn}</span>`;
}

// ---- Fix click ----
//
// Three code paths based on fix_kind:
//
//   * input_required → inline form panel, submit applies.
//   * programmatic   → inline CONFIRM panel (shows preview + what
//                      will change), Apply button commits. No
//                      browser window.confirm() — that's ugly and
//                      under-describes the change. The inline panel
//                      is styled the same as the form panel so
//                      both variants of Configure feel consistent.
//   * agent_handoff  → still uses a lightweight confirm popover
//                      (one question: "open a new terminal?") —
//                      no data to preview, and the side effect is
//                      self-evident once the terminal appears.
async function onFixClick(btnEl) {
    if (WB_READ_ONLY_MODE) return;
    const reqId = btnEl.dataset.reqId;
    const fixKind = btnEl.dataset.fixKind;
    const preview = btnEl.dataset.fixPreview;
    let params = {};
    try { params = JSON.parse(btnEl.dataset.fixParams || '{}'); } catch (e) { params = {}; }

    if (fixKind === 'input_required') {
        // input_required must never fall through to the agent_handoff
        // confirm dialog. With form fields, render the form; without
        // (misconfigured fix_params, or a parse failure above), fall
        // back to the programmatic confirm panel.
        if (Object.keys(params).length > 0) {
            _renderInputForm(btnEl, reqId, params);
        } else {
            _renderConfirmPanel(btnEl, reqId, preview);
        }
        return;
    }

    if (fixKind === 'programmatic') {
        _renderConfirmPanel(btnEl, reqId, preview);
        return;
    }

    // agent_handoff: lightweight confirm + spawn
    const ok = confirm('This will open a new Claude Code terminal session to walk you through the fix. Proceed?');
    if (!ok) return;
    await _postFix(reqId, {}, btnEl);
}

// Inline confirmation panel for programmatic fixes. Mirrors the
// structure of the input form so users see a consistent "Configure"
// panel regardless of whether input is needed.
function _renderConfirmPanel(btnEl, reqId, preview) {
    const li = btnEl.closest('.settings-req-item');
    if (!li) return;
    const actions = li.querySelector('.settings-req-actions');
    if (actions) actions.style.display = 'none';

    const panel = document.createElement('div');
    panel.className = 'settings-fix-form settings-fix-confirm';
    panel.innerHTML = `
        <div class="settings-fix-confirm-header">What will happen:</div>
        <div class="settings-fix-confirm-body">${escapeHtml(preview || 'Apply the registered fix for this requirement.')}</div>
        <div class="settings-fix-form-actions">
            <button type="button" class="settings-fix-btn settings-fix-apply-btn">Apply</button>
            <button type="button" class="settings-fix-cancel-btn">Cancel</button>
        </div>
    `;
    panel.querySelector('.settings-fix-apply-btn').addEventListener('click', async (e) => {
        await _postFix(reqId, {}, e.currentTarget);
    });
    panel.querySelector('.settings-fix-cancel-btn').addEventListener('click', () => {
        panel.remove();
        if (actions) actions.style.display = '';
    });
    li.appendChild(panel);
}

// Optimistically fold a fix endpoint's single-requirement `recheck`
// result into the in-memory graph so the fixed row updates instantly,
// without waiting on a full graph rebuild. Mirrors the server's
// requirement-state derivation (see control/graph.py); the follow-up
// loadSettings(false) is the authority that reconciles cascade
// roll-ups onto parent components and domains.
function _applyRecheck(reqId, recheck) {
    if (!recheck || !WB_CONTROL_GRAPH || !WB_CONTROL_GRAPH.nodes) return;
    const node = WB_CONTROL_GRAPH.nodes['req:' + reqId];
    if (!node) return;
    node.effective_state = recheck.ok
        ? 'ok'
        : (recheck.severity === 'required' ? 'unconfigured' : 'degraded');
    node.status_reason = recheck.detail || '';
    renderSettingsTree();
    renderSettingsSummary();
}

async function _postFix(reqId, params, btnEl) {
    btnEl.disabled = true;
    const orig = btnEl.textContent;
    btnEl.textContent = '…';
    try {
        const resp = await fetch('/api/control/fix/' + encodeURI(reqId), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({params}),
        });
        const data = await resp.json();
        if (data.spawned) {
            settingsToast(`Help session launched (pid ${data.spawned.pid}).`, 'success');
        } else if (data.ok) {
            const eff = data.side_effects && data.side_effects.length
                ? ` — ${data.side_effects.join('; ')}`
                : '';
            settingsToast(`Fixed: ${data.detail}${eff}`, 'success');
        } else {
            settingsToast(`Fix did not apply: ${data.detail}`, 'error');
        }
        // Update the fixed requirement in place from the endpoint's
        // single-requirement `recheck`, then refresh the rest of the
        // graph in the background (unforced — no panel blanking) so
        // cascade roll-ups onto parent components/domains reconcile.
        // Avoids blanking the whole panel behind a multi-second graph
        // rebuild just to reflect one row's change.
        if (data.recheck) {
            _applyRecheck(reqId, data.recheck);
        }
        loadSettings(false);
        // Briefly flash the requirement so the user sees THIS row is
        // the one that changed.
        if (data.ok) {
            requestAnimationFrame(() => _flashNode('req:' + reqId));
        }
    } catch (exc) {
        settingsToast(`Fix request failed: ${exc}`, 'error');
        btnEl.disabled = false;
        btnEl.textContent = orig;
    }
}

function _renderInputForm(btnEl, reqId, fixParams) {
    const li = btnEl.closest('.settings-req-item');
    if (!li) return;
    // Hide the actions while form is open
    const actions = li.querySelector('.settings-req-actions');
    if (actions) actions.style.display = 'none';

    const fields = Object.entries(fixParams).map(([name, spec]) => {
        const inputType = spec.secret ? 'password' : (spec.type === 'path' ? 'text' : 'text');
        const required = spec.required ? 'required' : '';
        const placeholder = spec.hint ? `placeholder="${escapeHtml(spec.hint)}"` : '';
        // Pre-fill with the requirement's current configured value when
        // the server supplied one, else the declared default. This is
        // what lets the form double as an "edit this setting" panel.
        const prefill = spec.current_value != null ? spec.current_value
                      : (spec.default != null ? spec.default : null);
        const valueAttr = prefill != null ? `value="${escapeHtml(String(prefill))}"` : '';
        return `
            <label class="settings-fix-field">
                <span class="settings-fix-field-label">${escapeHtml(spec.label || name)}${spec.required ? ' *' : ''}</span>
                <input type="${inputType}" name="${escapeHtml(name)}" ${required} ${placeholder} ${valueAttr}
                       autocomplete="off" spellcheck="false" />
            </label>
        `;
    }).join('');

    const form = document.createElement('form');
    form.className = 'settings-fix-form';
    form.innerHTML = `
        <div class="settings-fix-form-fields">${fields}</div>
        <div class="settings-fix-form-actions">
            <button type="submit" class="settings-fix-btn">Apply</button>
            <button type="button" class="settings-fix-cancel-btn">Cancel</button>
        </div>
    `;
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        const params = {};
        for (const [k, v] of fd.entries()) params[k] = v;
        // Restore actions visibility before posting (so re-render doesn't
        // leave the form orphaned)
        await _postFix(reqId, params, form.querySelector('button[type="submit"]'));
    });
    form.querySelector('.settings-fix-cancel-btn').addEventListener('click', () => {
        form.remove();
        if (actions) actions.style.display = '';
    });
    li.appendChild(form);
}

// ---- Per-component reprobe click ----
//
// Mirrors the Status-tab per-component reprobe: hits
// POST /api/reprobe/<component_id> which re-runs a single probe and
// rewrites tool_status.json for that one entry. Fast (~1s typical, no
// full graph rebuild). We then bust the graph cache via force=1 so
// the UI re-renders with the fresh state.
async function onComponentReprobeClick(btnEl) {
    if (WB_READ_ONLY_MODE) return;
    const componentId = btnEl.dataset.componentId;
    if (!componentId) return;
    const orig = btnEl.textContent;
    btnEl.disabled = true;
    btnEl.textContent = '…';
    try {
        const resp = await fetch('/api/reprobe/' + encodeURIComponent(componentId), {method: 'POST'});
        if (!resp.ok) {
            const errText = await resp.text();
            settingsToast(`Reprobe ${componentId} failed: ${errText}`, 'error');
            return;
        }
        await loadSettings(true);  // force-refresh the graph (single probe is on disk now)
        requestAnimationFrame(() => _flashNode('component:' + componentId));
    } catch (exc) {
        settingsToast(`Reprobe request failed: ${exc}`, 'error');
    } finally {
        // loadSettings re-renders so btnEl no longer exists if successful,
        // but restore state on error paths.
        btnEl.disabled = false;
        btnEl.textContent = orig;
    }
}

// ---- Help click ----
async function onHelpClick(btnEl) {
    if (WB_READ_ONLY_MODE) return;
    const nodeId = btnEl.dataset.nodeId;
    if (!confirm('Open a Claude Code session focused on diagnosing this? A new terminal window will appear.')) return;
    btnEl.disabled = true;
    const orig = btnEl.textContent;
    btnEl.textContent = '…';
    try {
        const resp = await fetch('/api/control/help/' + encodeURI(nodeId), {method: 'POST'});
        const data = await resp.json();
        if (data.ok) {
            settingsToast(`Help session launched (pid ${data.pid || '?'}).`, 'success');
        } else {
            settingsToast(`Help launch failed: ${data.detail}`, 'error');
        }
    } catch (exc) {
        settingsToast(`Help request failed: ${exc}`, 'error');
    } finally {
        btnEl.disabled = false;
        btnEl.textContent = orig;
    }
}

// Toggle the inline "what's disabled" panel in the summary row.
// Unlike other state chips which jump to a single node, "disabled" is
// plural by nature — users want to see the whole list.
function onDisabledChipClick() {
    WB_SHOW_DISABLED_LIST = !WB_SHOW_DISABLED_LIST;
    renderSettingsSummary();
}

// Click a bulk state chip in the summary row: find the first node in
// that state (considering ALL kinds, not just domain/subsystem/component),
// open its ancestors, and flash it. This surfaces orphan requirement
// failures that don't bubble up to a component.
function onStateChipClick(state) {
    if (!WB_CONTROL_GRAPH) return;
    const nodes = WB_CONTROL_GRAPH.nodes;
    // Prefer the most actionable node: requirement > component > subsystem > domain.
    // Capabilities excluded — they're rarely where the fix lives.
    const kindRank = {requirement: 0, component: 1, subsystem: 2, domain: 3};
    const candidates = Object.values(nodes)
        .filter(n => n.effective_state === state && n.kind !== 'capability')
        .sort((a, b) => (kindRank[a.kind] ?? 99) - (kindRank[b.kind] ?? 99));
    if (candidates.length === 0) {
        settingsToast(`No ${state} nodes visible.`, 'info');
        return;
    }
    const target = candidates[0];
    _expandAncestorsOf(target.id);
    _flashNode(target.id, candidates.length);
}

function _badgeClass(state) {
    return {
        ok: 'badge-green',
        degraded: 'badge-yellow',
        blocked: 'badge-red',
        unconfigured: 'badge-yellow',
        disabled: 'badge-muted',
        unknown: 'badge-muted',
    }[state] || 'badge-muted';
}

// ---- Tree rendering ----

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
    // Auto-open when filter is active AND any contained requirement is
    // a filter match — otherwise the user types "vault" and sees
    // "Requirements (8/9 ok)" collapsed with no way to know vault-root
    // is right there inside it.
    const autoOpen = WB_CONTROL_FILTER && reqIds.some(rid => {
        const r = nodes[rid];
        if (!r) return false;
        const f = WB_CONTROL_FILTER;
        return (r.label || '').toLowerCase().includes(f) ||
               (r.id || '').toLowerCase().includes(f);
    });
    return `
        <details class="settings-req-details" data-wb-detail-key="${escapeHtml(detailKey)}"${autoOpen ? ' open' : ''}>
            <summary>Requirements (${passing}/${total} ok)</summary>
            <ul class="settings-req-list">
                ${reqIds.map(rid => {
                    const r = nodes[rid];
                    if (!r) return `<li class="settings-req-item muted">${escapeHtml(rid)} — not registered</li>`;
                    return `<li class="settings-req-item" data-wb-node-id="${escapeHtml(r.id)}">
                        ${controlStateBadge(r.effective_state, r.id)}
                        <span class="settings-req-label">${escapeHtml(r.label)}</span>
                        ${r.status_reason ? `<span class="settings-req-reason">${escapeHtml(r.status_reason)}</span>` : ''}
                        ${_renderRequirementActions(r)}
                    </li>`;
                }).join('')}
            </ul>
        </details>
    `;
}

function _renderCapabilityList(capNames) {
    if (!capNames || capNames.length === 0) return '';
    const detailKey = 'cap:' + capNames.slice().sort().join(',');
    // Auto-open when filter matches one of the listed capability names.
    const autoOpen = WB_CONTROL_FILTER && capNames.some(
        c => c.toLowerCase().includes(WB_CONTROL_FILTER)
    );
    return `
        <details class="settings-cap-details" data-wb-detail-key="${escapeHtml(detailKey)}"${autoOpen ? ' open' : ''}>
            <summary>Affects ${capNames.length} capabilities</summary>
            <div class="settings-cap-chips">
                ${capNames.map(n => {
                    const hl = WB_CONTROL_FILTER && n.toLowerCase().includes(WB_CONTROL_FILTER)
                        ? ' settings-cap-chip-match'
                        : '';
                    return `<span class="settings-cap-chip${hl}">${escapeHtml(n)}</span>`;
                }).join('')}
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
            const hardness = e.hardness || 'hard';
            // Soft-dep tooltip prefers the specific fallback_note so
            // the user sees exactly what's affected when this dep is
            // down — not a vague "may be reduced".
            let softTip;
            if (hardness === 'soft') {
                softTip = e.fallback_note
                    ? `When ${escapeHtml(label)} is unavailable: ${escapeHtml(e.fallback_note)}`
                    : `Soft dependency — absence degrades some features, does not block this component`;
            }
            const hardnessBadge = hardness === 'soft'
                ? `<span class="settings-dep-hardness" title="${softTip}">soft</span>`
                : '';
            const chipTip = hardness === 'soft' && e.fallback_note
                ? softTip
                : `${escapeHtml(e.target_id)} (${hardness})`;
            return `<span class="settings-dep-chip" title="${chipTip}">${controlStateBadge(state, e.target_id)} ${escapeHtml(label)}${hardnessBadge}</span>`;
        }).join('')}
    </div>`;
}

function _renderAlsoIn(node, currentParent) {
    const others = (node.grouping_parents || []).filter(p => p !== currentParent);
    if (others.length === 0) return '';
    return `<span class="settings-also-in" title="Also appears under: ${escapeHtml(others.join(', '))}">also in ${others.length}</span>`;
}

function _renderComponentNode(nodes, node, underParent) {
    if (!_isVisible(node)) return '';
    // ? button always available on components — diagnose/help via a
    // spawned Claude Code session briefed with help_briefs.build_help_brief.
    // Styled emphatically (red-ish) when the component isn't ok so it
    // visibly says "you may want to click me" rather than fading in.
    const helpAlert = node.effective_state !== 'ok' && node.effective_state !== 'disabled'
        ? ' settings-help-btn-alert' : '';
    const helpBtn = `<button class="settings-help-btn${helpAlert}" type="button"
                              onclick="onHelpClick(this)"
                              data-node-id="${escapeHtml(node.id)}"
                              ${WB_READ_ONLY_MODE ? 'disabled' : ''}
                              title="Spawn a Claude Code session focused on this component. Bundles the full diagnostic output so you can investigate without re-explaining context.">?</button>`;
    // ↻ Reprobe button — refreshes THIS component's probe only, same
    // path as the legacy Status-tab reprobe. Fast (no full graph
    // rebuild) so users can chase 'unknown'/'degraded' signals
    // per-component without committing to the ~10s full reprobe.
    const reprobeBtn = `<button class="settings-reprobe-btn" type="button"
                                 onclick="onComponentReprobeClick(this)"
                                 data-component-id="${escapeHtml(node.component_id || '')}"
                                 ${WB_READ_ONLY_MODE ? 'disabled' : ''}
                                 title="Re-run just this component's probe (fast). Use when you want a definitive state for this one thing without waiting for a full reprobe.">&#x21BB;</button>`;
    // Recent-events chip — present only when this component has a sidecar
    // service AND that service has logged warn/error events. Clicking it
    // expands an inline panel (state tracked in window._WB_EVT_PANELS so
    // it survives morphdom re-renders).
    const evtReg = (window._WB_COMPONENT_EVTS || {})[node.id];
    const evtCount = evtReg ? evtReg.events.length : 0;
    const evtOpen = !!(window._WB_EVT_PANELS && window._WB_EVT_PANELS[node.id]);
    const evtChip = evtCount > 0
        ? `<button class="settings-evt-chip${evtOpen ? ' open' : ''}" type="button"
                   onclick="toggleComponentEvents('${escapeHtml(node.id)}', this)"
                   title="${evtCount} recent warn/error event${evtCount !== 1 ? 's' : ''} on this component — click to ${evtOpen ? 'hide' : 'show'}">&#9888; ${evtCount}</button>`
        : '';
    const evtPanel = evtCount > 0
        ? _renderComponentEventPanel(node.id, evtReg.events, evtOpen)
        : '';
    return `
        <div class="settings-node settings-component" data-state="${node.effective_state}" data-kind="component" data-wb-node-id="${escapeHtml(node.id)}">
            <div class="settings-node-header">
                <span class="settings-node-kind">COMPONENT</span>
                <span class="settings-node-label">${escapeHtml(node.label)}</span>
                ${controlStateBadge(node.effective_state, node.id)}
                ${preferenceBadge(node.preference)}
                ${_renderAlsoIn(node, underParent)}
                ${evtChip}
                ${reprobeBtn}
                ${helpBtn}
            </div>
            ${node.status_reason ? `<div class="settings-node-reason">${escapeHtml(node.status_reason)}</div>` : ''}
            ${preferenceToggleControls(node.component_id, node.preference)}
            ${_renderDependencyChips(nodes, node.dependencies)}
            ${_renderRequirementList(nodes, node.requirement_ids)}
            ${_renderCapabilityList(node.affects_capabilities)}
            ${evtPanel}
        </div>
    `;
}

function _renderSubsystemNode(nodes, node) {
    if (!_isVisible(node)) return '';
    return `
        <div class="settings-node settings-subsystem" data-state="${node.effective_state}" data-kind="subsystem" data-wb-node-id="${escapeHtml(node.id)}">
            <div class="settings-node-header">
                <span class="settings-node-kind">SUBSYSTEM</span>
                <span class="settings-node-label">${escapeHtml(node.label)}</span>
                ${controlStateBadge(node.effective_state, node.id)}
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

    if (!_isVisible(domain)) return '';

    // Default open for problematic domains (anything that isn't
    // ok/disabled), OR when filtering (so the user can immediately see
    // the matches in every domain that contains them).
    const shouldBeOpen = WB_CONTROL_FILTER
        ? true
        : !['ok', 'disabled'].includes(domain.effective_state);

    return `
        <details class="settings-domain" ${shouldBeOpen ? 'open' : ''} data-state="${domain.effective_state}" data-wb-detail-key="${escapeHtml(domain.id)}">
            <summary class="settings-domain-header">
                <span class="settings-domain-label">${escapeHtml(domain.label)}</span>
                ${controlStateBadge(domain.effective_state, domain.id)}
                <span class="settings-domain-count">${children.length} child${children.length === 1 ? '' : 'ren'}</span>
            </summary>
            <div class="settings-domain-desc">${escapeHtml(domain.description || '')}</div>
            <div class="settings-domain-body">
                ${childrenHtml || '<div class="empty-state">No visible children.</div>'}
            </div>
        </details>
    `;
}

// renderSettingsTree morphdom-merges fresh HTML into the live tree.
// morphdom natively diffs the ``open`` attribute on <details>, so the
// user's drill-down state survives preference toggles + reprobes
// without manual snapshot/restore.
//
// When WB_CONTROL_FILTER is active the new render has already computed
// which <details> should be open to surface filter matches; we
// communicate intent via the ``open=""`` attribute on the rendered
// HTML and morphdom respects it.
function renderSettingsTree() {
    const container = document.getElementById('settings-tree');
    if (!container) return;
    if (!WB_CONTROL_GRAPH) {
        container.innerHTML = '<div class="loading">Loading...</div>';
        return;
    }

    const nodes = WB_CONTROL_GRAPH.nodes;

    // Render user-facing domains first in a specific order, then everything else
    const domainOrder = [
        'domain:journal',
        'domain:notifications',
        'domain:knowledge',
        'domain:browser',
        'domain:calendar',
        'domain:runtime',
        'domain:backups',
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

    if (typeof window._wbMorphReplace === 'function') {
        window._wbMorphReplace(container, domainHtml + extras);
    } else {
        container.innerHTML = domainHtml + extras;
    }
}

// Surface handle for the Settings tab. SSE handlers in
// core/event_bus.py call refresh() on component.health_changed and
// component.preference_changed — refetches /api/control/graph and
// morphdom-merges into the tree. <details> open state is preserved
// natively by morphdom; no panel-wide rewrite ever occurs.
window.settingsSurface = {
    refresh: function() {
        let p;
        if (typeof loadSettings === 'function') p = loadSettings();
        // Keep the Activity sub-view fresh too, but only once it has been
        // opened at least once (loadActivity sets window._activityLoaded).
        if (window._activityLoaded && typeof loadActivity === 'function') {
            loadActivity();
        }
        return p;
    },
    isMounted: function() {
        return !!document.getElementById('settings-tree');
    },
};

// ==================================================================
// Activity sub-view — bridge sparkline + event log + notification log.
//
// The component health tree is intentionally NOT rendered here: the
// control graph in the Status sub-view is its strict superset.
//
// Read-only gating uses WB_READ_ONLY_MODE (set eagerly by
// initReadOnlyFlag), not the global _readOnly — the latter is only
// set truthfully once the Overview loader has run.
// ==================================================================

// Registry-driven mounter. The Activity sub-view's widgets (bridge
// sparkline, event log, notification log) are DashboardCards: the
// server (work_buddy/dashboard/cards.py) decides which mount given the
// control graph, and core/card_registry.py's wbMountCards renders the
// registered renderers into #activity-cards. The bridge card is gated
// on the ``obsidian`` component, so opting Obsidian out drops it here
// AND stops the backend bridge probe (see api.get_system_state).
//
// settingsSurface.refresh() re-runs this on component.preference_changed,
// so a preference toggle re-evaluates the gates with no page reload.
async function loadActivity() {
    window._activityLoaded = true;
    const data = await fetchJSON('/api/state', null, 5000);
    if (!data) return;
    window._WB_LAST_STATE = data;
    const container = document.getElementById('activity-cards');
    if (container && typeof window.wbMountCards === 'function') {
        await window.wbMountCards('activity', container, data);
    }
}

// Copy the event log to the clipboard as plain text.
function copyActivityLog() {
    const events = window._logActivityEvents || [];
    if (!events.length) return;
    const text = events.map(e => {
        const dt = new Date(e.ts * 1000);
        const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const kind = (e.kind || '').replace(/_/g, ' ').padEnd(16);
        const level = (e.level || 'info').toUpperCase().padEnd(5);
        return `${time}  ${level}  ${kind}  ${e.source}: ${e.summary}`;
    }).join('\\n');
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.querySelector('#ssp-activity .log-toolbar-btn');
        if (btn) { btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy Log', 1500); }
    });
}

// Shared spawn path for the Investigate buttons. POSTs the event to
// /api/investigate; when componentId is set the backend produces a
// richer brief (control-graph context for the linked component).
async function _spawnInvestigate(evt, btn, componentId) {
    if (WB_READ_ONLY_MODE || !evt || !btn) return;
    btn.textContent = 'Launching...';
    btn.disabled = true;
    const body = {event: evt};
    if (componentId) body.component_id = componentId;
    try {
        const r = await fetch('/api/investigate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
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

// Investigate from the Activity event log (no component context).
async function investigateActivityEvent(idx, btnEl) {
    const evt = (window._logActivityEvents || [])[idx];
    const btn = btnEl || (typeof event !== 'undefined' && event ? event.target : null);
    await _spawnInvestigate(evt, btn, null);
}

// Investigate from a per-component event chip panel (Status sub-view).
// Carries the component id so the spawned agent gets the richer brief.
async function investigateComponentEvent(nodeId, idx, btnEl) {
    const reg = (window._WB_COMPONENT_EVTS || {})[nodeId];
    if (!reg) return;
    await _spawnInvestigate(reg.events[idx], btnEl, reg.componentId);
}

// ---- Per-component event chips (Status sub-view) ----
//
// Joins sidecar warn/error events to control-graph components via
// `node.sidecar_service` === `event.source`. Rebuilt whenever the graph
// or /api/state refreshes. window._WB_COMPONENT_EVTS maps a component
// node id -> { componentId, sidecarService, events[] } (newest first).

function _rebuildComponentEventIndex() {
    const idx = {};
    const state = window._WB_LAST_STATE;
    const graph = WB_CONTROL_GRAPH;
    if (state && graph && graph.nodes) {
        const events = state.events || [];
        for (const nid in graph.nodes) {
            const n = graph.nodes[nid];
            if (n.kind !== 'component' || !n.sidecar_service) continue;
            const evts = events
                .filter(e => e && e.source === n.sidecar_service
                             && (e.level === 'warn' || e.level === 'error'))
                .slice()
                .reverse();  // newest first
            if (evts.length) {
                idx[n.id] = {
                    componentId: n.component_id,
                    sidecarService: n.sidecar_service,
                    events: evts,
                };
            }
        }
    }
    window._WB_COMPONENT_EVTS = idx;
}

// HTML for the collapsible event panel rendered inside a component node.
// `open` reflects window._WB_EVT_PANELS so the expanded/collapsed state
// survives morphdom re-renders.
function _renderComponentEventPanel(nodeId, evts, open) {
    const rows = evts.map((e, i) => {
        const dt = new Date(e.ts * 1000);
        const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const kind = (e.kind || '').replace(/_/g, ' ');
        const level = e.level || 'info';
        const invBtn = WB_READ_ONLY_MODE ? '' :
            `<button class="btn-investigate ${level}" type="button"
                     onclick="investigateComponentEvent('${escapeHtml(nodeId)}', ${i}, this)"
                     title="Spawn an agent to investigate this event">Investigate</button>`;
        return `<div class="evt-panel-row ${level}">
            <span class="evt-panel-ts">${time}</span>
            <span class="evt-panel-kind">${escapeHtml(kind)}</span>
            <span class="evt-panel-msg">${escapeHtml(e.summary || '')}</span>
            ${invBtn}
        </div>`;
    }).join('');
    return `<div class="settings-evt-panel${open ? ' open' : ''}" data-evt-component="${escapeHtml(nodeId)}">${rows}</div>`;
}

// Expand/collapse a component's event panel. Pure class toggle — the
// panel is part of the rendered tree, so morphdom keeps it in place.
function toggleComponentEvents(nodeId, btnEl) {
    if (!window._WB_EVT_PANELS) window._WB_EVT_PANELS = {};
    const open = !window._WB_EVT_PANELS[nodeId];
    window._WB_EVT_PANELS[nodeId] = open;
    const host = btnEl ? btnEl.closest('.settings-component') : null;
    const panel = host ? host.querySelector('.settings-evt-panel') : null;
    if (panel) panel.classList.toggle('open', open);
    if (btnEl) btnEl.classList.toggle('open', open);
}
"""
