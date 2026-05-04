"""Group-view frontend — multi-column re-organisable layout.

Activates when the active thread has ``parent_relationship === 'group'``.
Renders the parent + its sibling group-parents (same
``originating_scrape_id``) side-by-side as columns; each column lists
its child items with drag handles + inline action previews. Items can:

- Drag-and-drop between columns (reuses the vanilla HTML5 D&D pattern
  from ``script_triage.py:668-677``; no library).
- Multi-select via shift-click or keyboard (``x`` toggles selection
  on the focused card; ``Shift-j/k`` extends a range; ``m`` opens a
  move-to-prompt for the active selection).
- Click to open the standard right-pane editor.

Per-column controls:

- "Submit all" button — bulk-accepts every child in
  ``awaiting_confirmation`` via the ``POST /group_submit`` endpoint.
- Item count badge.

State is module-scoped and survives ``morphdom`` re-renders:

- ``window._groupState.selected: Set<thread_id>`` — currently selected
  items; cleared on any successful move so the next drag starts
  fresh.
- ``window._groupState.dragSource: thread_id | null`` — the item the
  cursor is currently dragging (set on ``dragstart``, cleared on
  ``dragend``).
- ``window._groupState.lastFocused: thread_id | null`` — anchor for
  shift-click range selection.
- ``window._groupState.siblingsByParent: { parentId: [renderedSibling, ...] }``
  — cached siblings response; invalidated by the move op so a fresh
  fetch picks up parent-side mutations.
"""

from __future__ import annotations


def _group_view_script() -> str:
    return r"""
(function () {
    if (!window._groupState) {
        window._groupState = {
            selected: new Set(),
            dragSource: null,
            lastFocused: null,
            siblingsByParent: {},
            siblingErrors: {},
        };
    }

    function _esc(s) {
        const d = document.createElement("div");
        d.textContent = s == null ? "" : String(s);
        return d.innerHTML;
    }

    // Public API: invoked by script_threads_v5.renderThreadDetail
    // when the active thread has parent_relationship === 'group'.
    window.renderGroupView = function (thread) {
        if (!thread) {
            return '<div class="threads-v5-group-empty">No thread loaded.</div>';
        }
        const parentId = thread.thread_id;
        const cached = window._groupState.siblingsByParent[parentId];
        if (cached) {
            return _renderColumns(thread, cached);
        }
        const failed = window._groupState.siblingErrors[parentId];
        if (failed) {
            return _renderFetchError(parentId, failed);
        }
        // Lazy-fetch siblings for this group
        fetch('/api/threads/' + encodeURIComponent(parentId) + '/group_siblings')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                window._groupState.siblingsByParent[parentId] =
                    data.siblings || [];
                if (typeof window._renderActiveThread === "function") {
                    window._renderActiveThread();
                }
            })
            .catch(err => {
                window._groupState.siblingErrors[parentId] = String(err);
                if (typeof window._renderActiveThread === "function") {
                    window._renderActiveThread();
                }
            });
        return '<div class="threads-v5-group-loading">Loading group view...</div>';
    };

    function _renderFetchError(parentId, msg) {
        return '<div class="threads-v5-group-empty threads-v5-group-fetch-error">'
            + '<h3>Couldn’t load sibling groups</h3>'
            + '<p>' + _esc(msg) + '</p>'
            + '<button class="threads-v5-retry-btn" onclick="(function(){'
            +   'delete window._groupState.siblingErrors[\'' + _esc(parentId) + '\'];'
            +   'window._renderActiveThread && window._renderActiveThread();'
            + '})()">Retry</button>'
            + '</div>';
    }

    function _renderColumns(active, siblings) {
        const activeId = active.thread_id;
        // Header — scrape-wide info + selection toolbar.
        const selCount = window._groupState.selected.size;
        let html = '<div class="threads-v5-group-view">'
            + _renderSuggestionsPanel(activeId)
            +   '<div class="threads-v5-group-header">'
            +     '<div class="threads-v5-group-title">'
            +       _esc(active.title || "Group view")
            +     '</div>'
            +     '<div class="threads-v5-group-meta">'
            +       siblings.length + ' group'
            +       (siblings.length === 1 ? '' : 's') + ' in this scrape'
            +       (selCount > 0
                        ? ' &middot; <strong>' + selCount + ' selected</strong>'
                        : '')
            +     '</div>'
            +   '</div>'
            +   '<div class="threads-v5-group-columns">';
        for (const sib of siblings) {
            html += _renderColumn(sib, sib.thread_id === activeId);
        }
        // Drop-here-to-spawn-a-new-group zone (stretch goal). Visible
        // only when there's at least one sibling to use as a
        // reference for scope inheritance.
        if (siblings.length > 0) {
            html += _renderNewGroupZone(activeId);
        }
        html += '</div>'
            + '<p class="threads-v5-group-help">'
            +   '<kbd>x</kbd> select &middot; <kbd>Shift</kbd>+click range '
            +   '&middot; <kbd>m</kbd> move-to &middot; drag any item to '
            +   'another group to move it (whole selection moves together)'
            + '</p>'
            + '</div>';
        return html;
    }

    function _renderColumn(sib, isActive) {
        const items = sib.children_render || [];
        const sId = sib.thread_id;
        const stateCounts = sib.sub_thread_state_counts || {};
        const awaitingCount = stateCounts.awaiting_confirmation || 0;
        let html = '<div class="threads-v5-group-column'
            + (isActive ? ' threads-v5-group-column-active' : '') + '" '
            + 'data-parent-id="' + _esc(sId) + '" '
            + 'ondragover="event.preventDefault();this.classList.add(\'drag-over\');" '
            + 'ondragleave="this.classList.remove(\'drag-over\');" '
            + 'ondrop="threadsGroupDropOnColumn(event, \'' + _esc(sId) + '\')">'
            + '<div class="threads-v5-group-column-header">'
            +   '<div class="threads-v5-group-column-title">'
            +     _esc(sib.title || sId)
            +   '</div>'
            +   '<div class="threads-v5-group-column-meta">'
            +     items.length + ' item' + (items.length === 1 ? '' : 's')
            +     (awaitingCount > 0
                    ? ' &middot; ' + awaitingCount + ' awaiting'
                    : '')
            +   '</div>';
        if (awaitingCount > 0) {
            html += '<button class="threads-v5-group-submit-all" '
                +   'title="Accept every awaiting_confirmation item in this group" '
                +   'onclick="event.stopPropagation();threadsGroupSubmitAll(\''
                +     _esc(sId) + '\')">'
                +   'Submit all (' + awaitingCount + ')'
                + '</button>';
        }
        html += '</div>'
            + '<ul class="threads-v5-group-items">';
        if (items.length === 0) {
            html += '<li class="threads-v5-group-empty-col">'
                +   '(empty — drop items here)'
                + '</li>';
        } else {
            for (const it of items) {
                html += _renderItemCard(it, sId);
            }
        }
        html += '</ul></div>';
        return html;
    }

    function _renderItemCard(item, parentId) {
        const tid = item.thread_id;
        const selected = window._groupState.selected.has(tid);
        const stateLabel = item.fsm_state || "";
        const intent = (item.intent && item.intent.text) || item.title || tid;
        const actions = item.actions || [];
        let html = '<li class="threads-v5-group-item'
            + (selected ? ' selected' : '') + '" '
            + 'data-thread-id="' + _esc(tid) + '" '
            + 'data-parent-id="' + _esc(parentId) + '" '
            + 'draggable="true" '
            + 'ondragstart="threadsGroupDragStart(event, \'' + _esc(tid) + '\')" '
            + 'ondragend="threadsGroupDragEnd(event, \'' + _esc(tid) + '\')" '
            + 'onclick="threadsGroupItemClick(event, \'' + _esc(tid) + '\')">'
            + '<div class="threads-v5-group-item-handle" title="Drag to move to another group">&#8801;</div>'
            + '<div class="threads-v5-group-item-body">'
            +   '<div class="threads-v5-group-item-title">'
            +     _esc(item.title || tid)
            +   '</div>'
            +   '<div class="threads-v5-group-item-state">'
            +     _esc(stateLabel) + '</div>';
        if (intent && intent !== item.title) {
            html += '<div class="threads-v5-group-item-intent">'
                +   _esc(intent.length > 100 ? intent.slice(0, 97) + '...' : intent)
                + '</div>';
        }
        if (actions.length > 0) {
            html += '<div class="threads-v5-group-item-actions">';
            for (const a of actions) {
                html += '<span class="threads-v5-group-item-action">'
                    +   '&rarr; ' + _esc(a.name || a.id || "(unnamed)")
                    + '</span>';
            }
            html += '</div>';
        }
        html += '</div></li>';
        return html;
    }

    // ---- Suggested cross-group merges --------------------------------
    //
    // Lazy-fetched from /group_suggestions per active parent_id; cached
    // in window._groupState.suggestionsByParent. The panel only
    // appears when there's at least one suggestion. Each suggestion
    // has Accept / Dismiss buttons:
    //   Accept → fires move op for the FIRST id in the pair into the
    //            SECOND id's parent (i.e. follows the system's
    //            recommendation).
    //   Dismiss → adds the pair to a session-only "ignored" set so
    //            the same pair doesn't re-surface this session.

    function _renderSuggestionsPanel(activeId) {
        const cached = (window._groupState.suggestionsByParent || {})[activeId];
        if (!cached) {
            // Trigger lazy fetch on first render. Don't show a
            // loading shell — suggestions are passive; if they
            // arrive a beat later, the panel just appears.
            _fetchSuggestions(activeId);
            return '';
        }
        const ignored = window._groupState.suggestionsIgnored || new Set();
        const live = (cached.suggestions || []).filter(s => {
            const key = _suggestionKey(s);
            return !ignored.has(key);
        });
        if (live.length === 0) return '';
        let html = '<div class="threads-v5-group-suggestions">'
            + '<div class="threads-v5-group-suggestions-header">'
            +   'Suggested moves '
            +   '<span class="threads-v5-group-suggestions-count">'
            +     '(' + live.length + ')'
            +   '</span>'
            + '</div>'
            + '<ul class="threads-v5-group-suggestions-list">';
        for (const s of live) {
            const key = _suggestionKey(s);
            const score = s.fused_score
                ? Math.round(s.fused_score * 100) + '%'
                : '';
            html += '<li class="threads-v5-group-suggestion">'
                +   '<div class="threads-v5-group-suggestion-text">'
                +     _esc(s.labels[0]) + ' &harr; ' + _esc(s.labels[1])
                +     (score ? ' <span class="threads-v5-group-suggestion-score">'
                                 + score + '</span>' : '')
                +   '</div>'
                +   '<div class="threads-v5-group-suggestion-actions">'
                +     '<button class="threads-v5-group-suggestion-accept" '
                +       'title="Move first item into the second item&#39;s group" '
                +       'onclick="threadsGroupAcceptSuggestion(\''
                +         _esc(s.ids[0]) + '\', \'' + _esc(s.ids[1]) + '\', \''
                +         _esc(key) + '\')">'
                +       'Accept &rarr;'
                +     '</button>'
                +     '<button class="threads-v5-group-suggestion-dismiss" '
                +       'title="Hide this suggestion" '
                +       'onclick="threadsGroupDismissSuggestion(\''
                +         _esc(key) + '\')">'
                +       '&times;'
                +     '</button>'
                +   '</div>'
                + '</li>';
        }
        html += '</ul></div>';
        return html;
    }

    function _suggestionKey(s) {
        // Order-independent key so accept/dismiss hits the right pair.
        const ids = (s.ids || []).slice().sort();
        return ids.join('|');
    }

    function _fetchSuggestions(activeId) {
        if (!window._groupState.suggestionsByParent) {
            window._groupState.suggestionsByParent = {};
        }
        // Mark as in-flight so we don't re-fire on every render.
        window._groupState.suggestionsByParent[activeId] = {
            suggestions: [],
            inflight: true,
        };
        fetch('/api/threads/' + encodeURIComponent(activeId) + '/group_suggestions')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                window._groupState.suggestionsByParent[activeId] = {
                    suggestions: data.suggestions || [],
                };
                if (typeof window._renderActiveThread === "function"
                    && (data.suggestions || []).length > 0) {
                    window._renderActiveThread();
                }
            })
            .catch(err => {
                // Silent failure — suggestions are passive.
                window._groupState.suggestionsByParent[activeId] = {
                    suggestions: [],
                    error: String(err),
                };
            });
    }

    window.threadsGroupAcceptSuggestion = function (sourceId, targetId, key) {
        // Mark dismissed first so the panel hides immediately even
        // if the move call is slow.
        if (!window._groupState.suggestionsIgnored) {
            window._groupState.suggestionsIgnored = new Set();
        }
        window._groupState.suggestionsIgnored.add(key);
        // Find the target's parent_id from the rendered DOM (already
        // there) so the move goes to the right destination column.
        const targetEl = document.querySelector(
            '.threads-v5-group-item[data-thread-id="' + targetId + '"]'
        );
        if (!targetEl) {
            // Suggestion stale (e.g., target moved or terminal).
            window._renderActiveThread && window._renderActiveThread();
            return;
        }
        const destParent = targetEl.dataset.parentId;
        // Move just this one item; clear any wider selection so we
        // don't accidentally drag others along.
        window._groupState.selected.clear();
        window._groupState.selected.add(sourceId);
        _moveBatch([sourceId], destParent);
    };

    window.threadsGroupDismissSuggestion = function (key) {
        if (!window._groupState.suggestionsIgnored) {
            window._groupState.suggestionsIgnored = new Set();
        }
        window._groupState.suggestionsIgnored.add(key);
        if (typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
    };

    function _renderNewGroupZone(referenceParentId) {
        return '<div class="threads-v5-group-newzone" '
            + 'ondragover="event.preventDefault();this.classList.add(\'drag-over\');" '
            + 'ondragleave="this.classList.remove(\'drag-over\');" '
            + 'ondrop="threadsGroupDropOnNewZone(event, \''
            +   _esc(referenceParentId) + '\')">'
            + '<div class="threads-v5-group-newzone-icon">+</div>'
            + 'Drop here to create a new group'
            + '</div>';
    }

    // ---- Drag-and-drop handlers ----------------------------------------

    window.threadsGroupDragStart = function (ev, threadId) {
        // If the dragged item isn't already in the selection, select it
        // alone — this matches the user expectation that drag operates
        // on the visually-grabbed item plus any explicit multi-selection.
        const sel = window._groupState.selected;
        if (!sel.has(threadId)) {
            sel.clear();
            sel.add(threadId);
            // Refresh selection visuals without a full re-render.
            document.querySelectorAll('.threads-v5-group-item.selected')
                .forEach(el => el.classList.remove('selected'));
            const me = document.querySelector(
                '.threads-v5-group-item[data-thread-id="' + threadId + '"]'
            );
            if (me) me.classList.add('selected');
        }
        window._groupState.dragSource = threadId;
        ev.dataTransfer.effectAllowed = "move";
        // Fingerprint so the drop target can sanity-check.
        try { ev.dataTransfer.setData("text/plain", threadId); } catch(e) {}
        // Mark all selected items as dragging-multi so the user sees
        // them lift together visually.
        document.querySelectorAll('.threads-v5-group-item.selected')
            .forEach(el => el.classList.add('dragging-multi'));
    };

    window.threadsGroupDragEnd = function (ev, threadId) {
        window._groupState.dragSource = null;
        document.querySelectorAll('.dragging-multi, .drag-over')
            .forEach(el => {
                el.classList.remove('dragging-multi');
                el.classList.remove('drag-over');
            });
    };

    window.threadsGroupDropOnNewZone = function (ev, referenceParentId) {
        ev.preventDefault();
        const zone = ev.currentTarget || ev.target.closest(
            '.threads-v5-group-newzone'
        );
        if (zone) zone.classList.remove('drag-over');
        const sel = Array.from(window._groupState.selected);
        if (sel.length === 0) return;
        // Optional label prompt — fall back to "New group" if cancelled.
        let label = window.prompt(
            'Name for the new group? (Leave blank for "New group")', ''
        );
        if (label === null) return;  // explicit cancel
        label = (label || '').trim() || 'New group';
        // Spawn the sibling, then move the dragged selection into it.
        fetch('/api/threads/' + encodeURIComponent(referenceParentId)
              + '/spawn_sibling_group', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label: label }),
        })
        .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
        .then(({ ok, body }) => {
            if (!ok) {
                if (typeof window.showToast === "function") {
                    window.showToast(
                        'New group failed',
                        body.error || 'Could not spawn sibling group',
                        'threads-view',
                        'group-spawn-err-' + Date.now(),
                        { expandable: false, view_type: 'generic' },
                        false, false,
                    );
                }
                return;
            }
            const newParentId = body.parent_id;
            _moveBatch(sel, newParentId);
        })
        .catch(e => {
            if (typeof window.showToast === "function") {
                window.showToast(
                    'New group failed', String(e),
                    'threads-view',
                    'group-spawn-err-' + Date.now(),
                    { expandable: false, view_type: 'generic' },
                    false, false,
                );
            }
        });
    };

    window.threadsGroupDropOnColumn = function (ev, destParentId) {
        ev.preventDefault();
        const col = ev.currentTarget || ev.target.closest(
            '.threads-v5-group-column'
        );
        if (col) col.classList.remove('drag-over');
        const sel = Array.from(window._groupState.selected);
        if (sel.length === 0) return;
        // Filter: don't move items already in the destination.
        const targets = sel.filter(tid => {
            const el = document.querySelector(
                '.threads-v5-group-item[data-thread-id="' + tid + '"]'
            );
            return el && el.dataset.parentId !== destParentId;
        });
        if (targets.length === 0) {
            window._groupState.selected.clear();
            window._renderActiveThread && window._renderActiveThread();
            return;
        }
        _moveBatch(targets, destParentId);
    };

    function _moveBatch(threadIds, destParentId) {
        const total = threadIds.length;
        let ok = 0, failed = 0;
        const failures = [];
        const promises = threadIds.map(tid =>
            fetch('/api/threads/' + encodeURIComponent(tid) + '/move_parent', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ new_parent_id: destParentId }),
            })
            .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
            .then(({ ok: ok2, body }) => {
                if (ok2) ok++;
                else { failed++; failures.push(body.reason || body.error); }
            })
            .catch(e => { failed++; failures.push(String(e)); })
        );
        Promise.all(promises).then(() => {
            // Invalidate caches so siblings re-fetch with fresh state.
            _invalidateGroupCaches();
            window._groupState.selected.clear();
            // Toast — reuse the shared notifications helper if present.
            const msg = (
                ok === total
                    ? 'Moved ' + ok + ' item' + (ok === 1 ? '' : 's')
                    : 'Moved ' + ok + ' / ' + total
                       + (failed
                           ? ' (' + failed + ' failed: '
                              + (failures[0] || 'unknown')
                              + (failures.length > 1
                                  ? ', ...' : '') + ')'
                           : '')
            );
            if (typeof window.showToast === "function") {
                try {
                    window.showToast(
                        ok === total ? "Items moved" : "Move partial",
                        msg, 'threads-view',
                        'group-move-' + Date.now(),
                        { expandable: false, view_type: 'generic' },
                        false, false,
                    );
                } catch(e) { /* toast is best-effort */ }
            }
            // Force a full re-render so column counts update.
            window._renderActiveThread && window._renderActiveThread();
        });
    }

    function _invalidateGroupCaches() {
        // Both the per-thread detail cache and the sibling cache need
        // refreshing — moves change parent_id (detail) and child counts
        // (siblings) on multiple threads at once.
        window._groupState.siblingsByParent = {};
        window._groupState.siblingErrors = {};
        if (typeof window.invalidateThreadCache === "function") {
            try { window.invalidateThreadCache(); } catch(e) {}
        }
        if (typeof window.invalidateTopLevelCache === "function") {
            try { window.invalidateTopLevelCache(); } catch(e) {}
        }
    }

    // ---- Selection handlers --------------------------------------------

    window.threadsGroupItemClick = function (ev, threadId) {
        // Plain click WITHOUT shift/ctrl/cmd → open the thread in the
        // standard right-pane editor (existing behaviour).
        // Shift/Ctrl/Cmd click → toggle selection.
        if (ev.shiftKey) {
            ev.preventDefault();
            _extendSelection(threadId);
            _refreshSelectionClasses();
            return;
        }
        if (ev.ctrlKey || ev.metaKey) {
            ev.preventDefault();
            window._groupState.selected.has(threadId)
                ? window._groupState.selected.delete(threadId)
                : window._groupState.selected.add(threadId);
            window._groupState.lastFocused = threadId;
            _refreshSelectionClasses();
            return;
        }
        // Plain click — clear selection, open the item.
        window._groupState.selected.clear();
        window._groupState.lastFocused = threadId;
        if (typeof window.threadsPushPath === "function") {
            window.threadsPushPath(threadId);
        }
    };

    function _extendSelection(toThreadId) {
        // Extend the selection to a range from lastFocused -> toThreadId.
        // Walks all items in DOM order so the "between" range is the
        // visual one the user expects.
        const all = Array.from(document.querySelectorAll(
            '.threads-v5-group-item'
        ));
        const ids = all.map(el => el.dataset.threadId);
        const anchor = window._groupState.lastFocused;
        const a = anchor ? ids.indexOf(anchor) : -1;
        const b = ids.indexOf(toThreadId);
        if (b < 0) return;
        if (a < 0) {
            window._groupState.selected.add(toThreadId);
            window._groupState.lastFocused = toThreadId;
            return;
        }
        const [lo, hi] = a < b ? [a, b] : [b, a];
        for (let i = lo; i <= hi; i++) {
            window._groupState.selected.add(ids[i]);
        }
    }

    function _refreshSelectionClasses() {
        const sel = window._groupState.selected;
        document.querySelectorAll('.threads-v5-group-item').forEach(el => {
            const tid = el.dataset.threadId;
            el.classList.toggle('selected', sel.has(tid));
        });
        // Update header selection-count without a full re-render.
        const meta = document.querySelector('.threads-v5-group-meta');
        if (meta) {
            const base = meta.textContent.split('·')[0].trim();
            meta.innerHTML = sel.size > 0
                ? base + ' &middot; <strong>' + sel.size + ' selected</strong>'
                : base;
        }
    }

    // ---- Bulk submit ---------------------------------------------------

    window.threadsGroupSubmitAll = function (parentId) {
        if (!window.confirm(
            'Submit all awaiting items in this group? Each item will run '
            + 'through Accept individually.'
        )) return;
        fetch('/api/threads/' + encodeURIComponent(parentId)
              + '/group_submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        })
        .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
        .then(({ ok, body }) => {
            if (!ok) {
                if (typeof window.showToast === "function") {
                    window.showToast(
                        'Submit failed',
                        body.error || 'Group submit failed',
                        'threads-view',
                        'group-submit-err-' + Date.now(),
                        { expandable: false, view_type: 'generic' },
                        false, false,
                    );
                }
                return;
            }
            const submitted = body.submitted || 0;
            const failed = body.failed || 0;
            const skipped = body.skipped || 0;
            const msg = 'Submitted ' + submitted
                + (failed ? ', ' + failed + ' failed' : '')
                + (skipped ? ', ' + skipped + ' skipped' : '');
            if (typeof window.showToast === "function") {
                window.showToast(
                    'Group submitted',
                    msg, 'threads-view',
                    'group-submit-' + Date.now(),
                    { expandable: false, view_type: 'generic' },
                    false, false,
                );
            }
            _invalidateGroupCaches();
            window._renderActiveThread && window._renderActiveThread();
        })
        .catch(e => {
            if (typeof window.showToast === "function") {
                window.showToast(
                    'Submit failed', String(e),
                    'threads-view',
                    'group-submit-err-' + Date.now(),
                    { expandable: false, view_type: 'generic' },
                    false, false,
                );
            }
        });
    };

    // ---- Keyboard handler — extends script_threads_v5's j/k --------------
    //
    // Installed once at module load. We listen on keydown and dispatch
    // group-specific keys (x / m / Shift-j/k) when the active thread
    // is a group-parent. Keys we don't handle pass through to the
    // existing j/k handler.

    if (!window._threadsGroupKbdInstalled) {
        window._threadsGroupKbdInstalled = true;
        document.addEventListener("keydown", function (ev) {
            // Only act when a group-parent is the active detail.
            const state = window._threadsState;
            if (!state || !state.path || state.path.length === 0) return;
            const activeId = state.path[state.path.length - 1];
            const cached = (window._threadDetailCache || {})[activeId];
            if (!cached || cached.parent_relationship !== "group") return;
            // Don't fire when the user is typing.
            const tgt = ev.target;
            if (tgt && /^(INPUT|TEXTAREA|SELECT)$/.test(tgt.tagName)) return;
            const k = ev.key;
            if (k === "x") {
                ev.preventDefault();
                _toggleSelectionAtFocus();
                _refreshSelectionClasses();
            } else if (k === "m") {
                ev.preventDefault();
                _promptMove();
            } else if (k === "J" || k === "K") {
                // Shift-j / Shift-k: extend selection by one step.
                ev.preventDefault();
                _stepFocus(k === "J" ? -1 : 1, true);
            } else if (k === "Escape") {
                if (window._groupState.selected.size > 0) {
                    ev.preventDefault();
                    window._groupState.selected.clear();
                    _refreshSelectionClasses();
                }
            }
        });
    }

    function _toggleSelectionAtFocus() {
        const focusedEl = document.querySelector(
            '.threads-v5-group-item.threads-v5-kbd-focus'
        ) || document.querySelector(
            '.threads-v5-group-item[data-thread-id="'
            + (window._groupState.lastFocused || '') + '"]'
        );
        if (!focusedEl) return;
        const tid = focusedEl.dataset.threadId;
        if (window._groupState.selected.has(tid)) {
            window._groupState.selected.delete(tid);
        } else {
            window._groupState.selected.add(tid);
            window._groupState.lastFocused = tid;
        }
    }

    function _stepFocus(delta, extendSelection) {
        const all = Array.from(document.querySelectorAll(
            '.threads-v5-group-item'
        ));
        if (all.length === 0) return;
        const focused = window._groupState.lastFocused;
        let idx = focused
            ? all.findIndex(el => el.dataset.threadId === focused)
            : -1;
        if (idx < 0) idx = 0;
        let next = idx + delta;
        if (next < 0) next = 0;
        if (next >= all.length) next = all.length - 1;
        const nextTid = all[next].dataset.threadId;
        if (extendSelection) {
            window._groupState.selected.add(nextTid);
        }
        window._groupState.lastFocused = nextTid;
        all.forEach(el => el.classList.remove('threads-v5-kbd-focus'));
        all[next].classList.add('threads-v5-kbd-focus');
        all[next].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        _refreshSelectionClasses();
    }

    function _promptMove() {
        const sel = Array.from(window._groupState.selected);
        if (sel.length === 0) {
            if (typeof window.showToast === "function") {
                window.showToast(
                    'Nothing selected',
                    'Press x to select items first.',
                    'threads-view',
                    'group-move-empty-' + Date.now(),
                    { expandable: false, view_type: 'generic' },
                    false, false,
                );
            }
            return;
        }
        // Pull sibling labels from the cache so the user picks by label.
        const state = window._threadsState;
        const activeId = state.path[state.path.length - 1];
        const sibs = (window._groupState.siblingsByParent[activeId] || []);
        const candidates = sibs.filter(s => s.thread_id !== activeId);
        if (candidates.length === 0) {
            if (typeof window.showToast === "function") {
                window.showToast(
                    'No other groups',
                    'This scrape only has one group.',
                    'threads-view',
                    'group-move-only-' + Date.now(),
                    { expandable: false, view_type: 'generic' },
                    false, false,
                );
            }
            return;
        }
        const lines = candidates.map((s, i) =>
            (i + 1) + ') ' + (s.title || s.thread_id)
        ).join('\n');
        const pick = window.prompt(
            'Move ' + sel.length + ' item' + (sel.length === 1 ? '' : 's')
                + ' to which group?\n\n' + lines + '\n\nEnter a number:'
        );
        const n = parseInt(pick, 10);
        if (!n || n < 1 || n > candidates.length) return;
        _moveBatch(sel, candidates[n - 1].thread_id);
    }
})();
"""


def _group_view_styles() -> str:
    return r"""
/* Stage 5: group-view multi-column layout. Activates when the active
 * thread has parent_relationship === 'group'. Columns are flex
 * children that wrap on narrow viewports.
 */
.threads-v5-group-view {
    padding: 16px 20px;
    color: var(--text, #ddd);
}
.threads-v5-group-empty,
.threads-v5-group-loading {
    padding: 2em;
    text-align: center;
    color: var(--text-muted, #888);
}
.threads-v5-group-fetch-error {
    color: var(--text, #ddd);
}

.threads-v5-group-header {
    margin-bottom: 14px;
    display: flex;
    flex-direction: column;
    gap: 4px;
}
.threads-v5-group-title {
    font-size: 18px;
    font-weight: 600;
}
.threads-v5-group-meta {
    color: var(--text-muted, #888);
    font-size: 12px;
}

.threads-v5-group-columns {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    align-items: flex-start;
}

.threads-v5-group-column {
    flex: 1 1 280px;
    min-width: 240px;
    max-width: 360px;
    background: var(--bg-secondary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    padding: 10px;
    transition: border-color 80ms, background-color 80ms;
}
.threads-v5-group-column.drag-over {
    border-color: var(--accent, #4a7fc1);
    background: var(--bg-tertiary, #232323);
}
.threads-v5-group-column-active {
    box-shadow: 0 0 0 1px var(--accent, #4a7fc1);
}

.threads-v5-group-column-header {
    margin-bottom: 8px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border, #333);
}
.threads-v5-group-column-title {
    font-size: 14px;
    font-weight: 600;
}
.threads-v5-group-column-meta {
    font-size: 11px;
    color: var(--text-muted, #888);
    margin-top: 2px;
}
.threads-v5-group-submit-all {
    margin-top: 6px;
    background: var(--accent, #4a7fc1);
    color: white;
    border: 1px solid var(--accent, #4a7fc1);
    border-radius: 4px;
    padding: 3px 10px;
    font-size: 11px;
    cursor: pointer;
}
.threads-v5-group-submit-all:hover {
    filter: brightness(1.1);
}

.threads-v5-group-items {
    list-style: none;
    margin: 0;
    padding: 0;
    min-height: 60px;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.threads-v5-group-empty-col {
    color: var(--text-muted, #666);
    font-size: 12px;
    font-style: italic;
    padding: 14px 6px;
    text-align: center;
    border: 1px dashed var(--border, #333);
    border-radius: 4px;
}

.threads-v5-group-item {
    display: flex;
    gap: 6px;
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 5px;
    padding: 7px 9px;
    cursor: grab;
    user-select: none;
    transition: background-color 80ms, border-color 80ms;
}
.threads-v5-group-item:hover {
    background: var(--bg-tertiary, #1a1a1a);
}
.threads-v5-group-item.selected {
    border-color: var(--accent, #4a7fc1);
    background: rgba(74, 127, 193, 0.08);
}
.threads-v5-group-item.dragging-multi {
    opacity: 0.5;
}
.threads-v5-group-item.threads-v5-kbd-focus {
    outline: 2px solid var(--accent, #4a7fc1);
    outline-offset: -2px;
}

.threads-v5-group-item-handle {
    color: var(--text-muted, #666);
    font-size: 14px;
    flex: 0 0 auto;
    line-height: 1.2;
    cursor: grab;
}
.threads-v5-group-item-body {
    flex: 1 1 auto;
    min-width: 0;
}
.threads-v5-group-item-title {
    font-size: 13px;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.threads-v5-group-item-state {
    font-size: 10px;
    color: var(--text-muted, #888);
    text-transform: capitalize;
}
.threads-v5-group-item-intent {
    font-size: 11px;
    color: var(--text-muted, #aaa);
    margin-top: 3px;
}
.threads-v5-group-item-actions {
    margin-top: 4px;
    font-size: 11px;
    color: var(--accent, #4a7fc1);
}
.threads-v5-group-item-action {
    display: inline-block;
    margin-right: 8px;
}

.threads-v5-group-newzone {
    flex: 0 0 240px;
    min-height: 140px;
    border: 2px dashed var(--border, #333);
    border-radius: 8px;
    color: var(--text-muted, #888);
    font-size: 12px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 16px;
    transition: border-color 80ms, color 80ms;
}
.threads-v5-group-newzone.drag-over {
    border-color: var(--accent, #4a7fc1);
    color: var(--accent, #4a7fc1);
    background: rgba(74, 127, 193, 0.05);
}
.threads-v5-group-newzone-icon {
    font-size: 28px;
    line-height: 1;
}

/* Suggested cross-group merges — passive side panel above the
 * columns. Cards have Accept (follow the suggestion → move) and
 * Dismiss (hide for the rest of the session). Suggestions come
 * from the embedding-fused similarity layer in
 * journal_backlog.similarity, which we built in PR #75. */
.threads-v5-group-suggestions {
    margin-bottom: 14px;
    background: var(--bg-secondary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-left: 3px solid #c0a040;  /* warm yellow → "tip" */
    border-radius: 6px;
    padding: 10px 12px;
}
.threads-v5-group-suggestions-header {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-muted, #aaa);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.threads-v5-group-suggestions-count {
    color: var(--text-muted, #888);
    font-weight: 400;
}
.threads-v5-group-suggestions-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
}
.threads-v5-group-suggestion {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 4px 6px;
    border-radius: 4px;
    font-size: 12px;
}
.threads-v5-group-suggestion:hover {
    background: var(--bg-tertiary, #232323);
}
.threads-v5-group-suggestion-text {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.threads-v5-group-suggestion-score {
    color: var(--text-muted, #888);
    font-size: 11px;
    margin-left: 6px;
}
.threads-v5-group-suggestion-actions {
    flex: 0 0 auto;
    display: flex;
    gap: 4px;
}
.threads-v5-group-suggestion-accept {
    background: transparent;
    color: var(--accent, #4a7fc1);
    border: 1px solid var(--accent, #4a7fc1);
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 11px;
    cursor: pointer;
}
.threads-v5-group-suggestion-accept:hover {
    background: var(--accent, #4a7fc1);
    color: white;
}
.threads-v5-group-suggestion-dismiss {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid transparent;
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 14px;
    cursor: pointer;
    line-height: 1;
}
.threads-v5-group-suggestion-dismiss:hover {
    color: var(--text, #ddd);
    border-color: var(--border, #333);
}

.threads-v5-group-help {
    margin-top: 14px;
    color: var(--text-muted, #666);
    font-size: 11px;
}
.threads-v5-group-help kbd {
    background: var(--bg-tertiary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 1px 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 10px;
}
"""
