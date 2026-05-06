"""Dashboard Tasks tab JS.

Owns the Tasks tab loader, master-task-list rendering, namespace tree
filtering, and the state-chip filter cluster (#todo states).

Publishes ``window.tasksSurface`` so the SSE event bus can refresh on
``task.created`` / ``task.state_changed`` / ``task.description_changed``
without polling.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Tasks ----
function renderTaskTable(tasks) {
    const el = document.getElementById('task-list');
    if (tasks.length === 0) {
        if (typeof window._wbMorphReplace === 'function') {
            window._wbMorphReplace(el, '<div class="empty-state">No matching tasks</div>');
        } else {
            el.innerHTML = '<div class="empty-state">No matching tasks</div>';
        }
        return;
    }
    const rows = tasks.map(t => {
        const noteCell = t.note_id
            ? `<a href="obsidian://open?vault=${encodeURIComponent(WB_VAULT_NAME)}&file=${encodeURIComponent(t.note_id)}" title="Open note in Obsidian" style="text-decoration:none;cursor:pointer">&#x1F4D3;</a>`
            : '\u2014';
        const markers = (t.markers || []).map(m =>
            `<span title="${m.label}${m.date ? ' ' + m.date : ''}" style="cursor:help">${m.emoji}</span>`
        ).join(' ') || '\u2014';
        // Slice 4: tier + actor + pipeline-blocker badges, opt-in
        // via the .has-* CSS classes (only render when the field is
        // populated, so legacy rows look unchanged).
        let autoCell = '\u2014';
        const autBits = [];
        if (typeof t.operating_tier === 'number') {
            autBits.push(
                '<span class="aut-tier-badge tier-' + t.operating_tier
                + '" title="Operating tier (achievable=' + (t.achievable_tier ?? '?') + ')">'
                + 'tier ' + t.operating_tier + '</span>'
            );
        }
        if (t.pipeline_blocker) {
            const blkLabels = {
                'consent_required': 'consent',
                'risk_threshold_exceeded': 'risk',
                'inference_uncertain': 'unsure',
                'agent_context_unmet': 'agent ctx',
                'user_context_unmet': 'user ctx',
                'clarification_required': 'clarify',
            };
            const blkLabel = blkLabels[t.pipeline_blocker] || t.pipeline_blocker;
            autBits.push(
                '<span class="wv-blocker-badge tone-blocked" title="' + escapeHtml(t.pipeline_blocker) + '">'
                + '<span class="wv-blocker-icon">\u26d4</span>'
                + '<span class="wv-blocker-label">' + escapeHtml(blkLabel) + '</span>'
                + '</span>'
            );
        }
        if (t.last_actor) {
            autBits.push(
                '<span class="aut-actor-badge actor-' + escapeHtml(t.last_actor) + '"'
                + ' title="Last actor">' + escapeHtml(t.last_actor) + '</span>'
            );
        }
        if (autBits.length) autoCell = autBits.join(' ');
        // Per-row identity via data-task-id so morphdom can keep
        // unchanged rows in place across refreshes (preserves any
        // inline edit state, hover, scroll position).
        return `<tr data-task-id="${t.id || ''}">
            <td>${statusBadge(t.state)}</td>
            <td>${t.text}</td>
            <td>${t.urgency !== 'none' ? statusBadge(t.urgency) : '\u2014'}</td>
            <td style="white-space:nowrap">${markers}</td>
            <td style="white-space:nowrap">${autoCell}</td>
            <td style="text-align:center">${noteCell}</td>
            <td><code>${t.id || '\u2014'}</code></td>
        </tr>`;
    }).join('');
    const html = `
        <div class="task-list-scroll">
        <table class="data-table">
            <thead><tr><th>State</th><th>Task</th><th>Urgency</th><th>Markers</th><th title="tier \u00b7 blocker \u00b7 actor">Auto</th><th>Note</th><th>ID</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        </div>
    `;
    if (typeof window._wbMorphReplace === 'function') {
        window._wbMorphReplace(el, html);
    } else {
        el.innerHTML = html;
    }
}

// Namespace-tree state for the Tasks tab. null = "All tasks" lens.
window._selectedNamespace = null;

// Task state filter — a Set of state names the user wants visible.
// Default: every non-done state. Users toggle individual chips to
// add/remove states; "done" can be toggled on alongside any others
// so the view can show arbitrary combinations.
const TASK_STATES_ORDER = ["mit", "focused", "inbox", "snoozed", "done"];
window._taskStateFilter = new Set(["mit", "focused", "inbox", "snoozed"]);

function _applyTaskStateFilter(tasks, activeStates) {
    if (!Array.isArray(tasks)) return [];
    if (!activeStates || activeStates.size === 0) return [];
    return tasks.filter(t => activeStates.has(t.state || (t.done ? "done" : "inbox")));
}

function _renderTaskStateChips() {
    const host = document.getElementById('task-state-chips');
    if (!host) return;
    host.innerHTML = '';
    const active = window._taskStateFilter;
    for (const state of TASK_STATES_ORDER) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'task-state-chip' + (active.has(state) ? ' selected' : '');
        chip.dataset.state = state;
        chip.textContent = state;
        chip.title = active.has(state) ? 'Hide ' + state : 'Show ' + state;
        chip.addEventListener('click', () => {
            if (active.has(state)) active.delete(state);
            else active.add(state);
            _renderTaskStateChips();
            _refreshTaskView();
        });
        host.appendChild(chip);
    }
}

async function loadTasks() {
    _renderTaskStateChips();
    await _refreshTaskView();

    const searchInput = document.getElementById('task-search');
    if (searchInput && !searchInput._bound) {
        searchInput._bound = true;
        searchInput.addEventListener('input', () => {
            const q = searchInput.value.toLowerCase().trim();
            const base = window._allTasks || [];
            const filtered = q
                ? base.filter(t =>
                    (t.text || '').toLowerCase().includes(q) ||
                    (t.id || '').toLowerCase().includes(q) ||
                    (t.state || '').toLowerCase().includes(q) ||
                    (t.urgency || '').toLowerCase().includes(q)
                )
                : base;
            renderTaskTable(filtered);
        });
    }
}

// Surface handle for the Tasks tab. SSE handlers in core/event_bus.py
// call refresh() on task.created / task.state_changed /
// task.description_changed — re-runs _refreshTaskView which fetches
// /api/tasks and morphdom-merges the table. The user's typing in
// task-search and any other inputs survive natively.
window.tasksSurface = {
    refresh: function() {
        if (typeof _refreshTaskView === 'function') return _refreshTaskView();
    },
    isMounted: function() {
        return !!document.getElementById('task-list');
    },
};

// Task filter composition:
//
//   /api/tasks (single fetch) → all tasks (with state + namespace tags)
//        ↓
//   apply state chips  → stateFilteredTasks
//        ↓
//   build tree from stateFilteredTasks ─→ sidebar
//        ↓
//   apply namespace selection → visibleTasks
//        ↓
//   list panel + counts cards
//
// The tree is rebuilt on every state-chip / namespace change so counts,
// aggregation, and empty-node hiding all reflect the current lens.

function _matchesNamespace(task, ns) {
    // Task matches a namespace if any of its tags equals `ns` or is a
    // descendant (prefix + '/'). Tree navigation always treats selection
    // as prefix-inclusive (descendants=true) because that's what the
    // tree UI implies.
    const tags = task.tags || [];
    const prefix = ns + '/';
    for (const t of tags) {
        if (t === ns || t.startsWith(prefix)) return true;
    }
    return false;
}

function _buildNamespaceTree(tasks) {
    // Build a tree from the given tasks. A node is created for every
    // segment on every task's namespace tag; each ancestor's `taskIds`
    // set accumulates the union of its subtree. Nodes only exist if
    // they have at least one task — empty namespaces fall out naturally.
    const root = {
        children: new Map(),
        taskIds: new Set(),
        recentIds: new Set(),
        tag: null,
    };
    for (const t of tasks) {
        // Every task counts toward the root total (shown on "All tasks").
        root.taskIds.add(t.id);
        if (t.is_recent) root.recentIds.add(t.id);
        const tags = t.tags || [];
        if (!tags.length) continue;
        for (const tag of tags) {
            const parts = tag.split('/').filter(Boolean);
            if (!parts.length) continue;
            let node = root;
            for (let i = 0; i < parts.length; i++) {
                const seg = parts[i];
                if (!node.children.has(seg)) {
                    node.children.set(seg, {
                        children: new Map(),
                        taskIds: new Set(),
                        recentIds: new Set(),
                        tag: parts.slice(0, i + 1).join('/'),
                    });
                }
                node = node.children.get(seg);
                node.taskIds.add(t.id);
                if (t.is_recent) node.recentIds.add(t.id);
            }
        }
    }
    return root;
}

function _renderNamespaceTree(root) {
    const container = document.getElementById('task-namespace-tree');
    if (!container) return;
    container.innerHTML = '';

    // "All tasks" pseudo-node — always present, acts as the clear.
    const allNode = document.createElement('div');
    allNode.className = 'wv-ns-node wv-ns-all' + (window._selectedNamespace === null ? ' selected' : '');
    const allLabel = document.createElement('span');
    allLabel.className = 'wv-ns-label';
    allLabel.textContent = 'All tasks';
    allNode.appendChild(allLabel);
    const allBadge = document.createElement('span');
    allBadge.className = 'wv-ns-count';
    allBadge.textContent = String(root.taskIds.size);
    allNode.appendChild(allBadge);
    allNode.addEventListener('click', () => selectNamespace(null));
    container.appendChild(allNode);

    if (root.children.size === 0) {
        const hint = document.createElement('div');
        hint.className = 'wv-ns-empty';
        hint.textContent = 'No namespaces match current filter.';
        container.appendChild(hint);
        return;
    }

    function renderNode(parent, node, depth) {
        // Sort siblings by rollup score (descending) with alphabetical
        // tie-break so ordering is stable and predictable.
        const sorted = Array.from(node.children.entries()).sort((a, b) => {
            const ra = a[1].taskIds.size + 2 * a[1].recentIds.size;
            const rb = b[1].taskIds.size + 2 * b[1].recentIds.size;
            if (ra !== rb) return rb - ra;
            return a[0].localeCompare(b[0]);
        });
        for (const [seg, child] of sorted) {
            const wrap = document.createElement('div');
            wrap.className = 'wv-ns-node-wrap';
            const row = document.createElement('div');
            row.className = 'wv-ns-node' + (window._selectedNamespace === child.tag ? ' selected' : '');
            row.style.paddingLeft = (6 + depth * 12) + 'px';
            const hasKids = child.children.size > 0;
            const twisty = document.createElement('span');
            twisty.className = 'wv-ns-twisty';
            twisty.textContent = hasKids ? '\u25B8' : ' ';
            row.appendChild(twisty);
            const label = document.createElement('span');
            label.className = 'wv-ns-label';
            label.textContent = seg;
            row.appendChild(label);
            // Always show a badge — every node has at least one task
            // (empty nodes are pruned at build time).
            const badge = document.createElement('span');
            badge.className = 'wv-ns-count';
            badge.textContent = String(child.taskIds.size);
            row.appendChild(badge);
            row.addEventListener('click', (e) => {
                if (e.target === twisty && hasKids) {
                    wrap.classList.toggle('collapsed');
                    twisty.textContent = wrap.classList.contains('collapsed') ? '\u25B8' : '\u25BE';
                    return;
                }
                selectNamespace(child.tag);
            });
            wrap.appendChild(row);
            if (hasKids) {
                const kids = document.createElement('div');
                kids.className = 'wv-ns-children';
                renderNode(kids, child, depth + 1);
                wrap.appendChild(kids);
                // Expanded by default if a descendant is selected.
                const sel = window._selectedNamespace;
                const keepOpen = sel && (sel === child.tag || sel.startsWith(child.tag + '/'));
                if (keepOpen) twisty.textContent = '\u25BE';
                else { wrap.classList.add('collapsed'); twisty.textContent = '\u25B8'; }
            }
            parent.appendChild(wrap);
        }
    }

    renderNode(container, root, 0);
}

async function _refreshTaskView() {
    const data = await fetchJSON('/api/tasks');
    if (!data) return;

    const allTasks = data.tasks || [];
    const activeStates = window._taskStateFilter || new Set();
    const ns = window._selectedNamespace;

    // Pipeline: state filter → tree + counts; then namespace filter → list.
    const stateFiltered = _applyTaskStateFilter(allTasks, activeStates);

    // Tree reflects the state filter — empty nodes are naturally pruned
    // because _buildNamespaceTree only creates nodes for tags actually
    // present on surviving tasks.
    const treeRoot = _buildNamespaceTree(stateFiltered);
    _renderNamespaceTree(treeRoot);

    // Counts cards: baseline snapshot of state distribution (unfiltered
    // by state, so the user can see what their chip toggles dropped).
    const counts = {};
    for (const t of allTasks) {
        const s = t.state || (t.done ? 'done' : 'inbox');
        counts[s] = (counts[s] || 0) + 1;
    }
    const countCards = Object.entries(counts).map(([state, n]) => `
        <div class="card">
            <div class="card-label">${state}</div>
            <div class="card-value">${n}</div>
        </div>
    `).join('');
    const countsEl = document.getElementById('task-counts');
    const countsHtml = countCards || '<div class="empty-state">No tasks</div>';
    if (typeof window._wbMorphReplace === 'function') {
        window._wbMorphReplace(countsEl, countsHtml);
    } else {
        countsEl.innerHTML = countsHtml;
    }

    // Breadcrumb.
    const crumb = document.getElementById('task-namespace-breadcrumb');
    if (crumb) {
        if (ns) {
            crumb.innerHTML = '\u2192 <span class="task-namespace-crumb-tag">#' + ns + '</span> '
                + '<a href="#" class="task-namespace-clear" title="Show all tasks">\u2715 clear</a>';
            const clearEl = crumb.querySelector('.task-namespace-clear');
            if (clearEl) clearEl.addEventListener('click', (e) => {
                e.preventDefault();
                selectNamespace(null);
            });
        } else {
            crumb.innerHTML = '';
        }
    }

    // Apply namespace selection to the list panel. Descendants included.
    const visible = ns
        ? stateFiltered.filter(t => _matchesNamespace(t, ns))
        : stateFiltered;

    if (visible.length === 0) {
        let label;
        if (activeStates.size === 0) {
            label = 'No states selected';
        } else {
            const parts = Array.from(activeStates);
            label = 'No tasks in {' + parts.join(', ') + '}';
        }
        document.getElementById('task-list').innerHTML =
            '<div class="empty-state">' + label + (ns ? ' under #' + ns : '') + '</div>';
        window._allTasks = [];
        return;
    }

    window._allTasks = visible;
    renderTaskTable(visible);
}

function selectNamespace(ns) {
    window._selectedNamespace = ns;
    _refreshTaskView();
    _persistHash();
}
"""
