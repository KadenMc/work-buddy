"""Group-view frontend — multi-column drag/drop layout slotted
into the standard thread detail UI's "Sub-threads" section, for
group-relationship **umbrella** threads only.

Architecture
------------

The renderer here exposes ``window.renderGroupSubThreads(umbrella)``,
which the standard ``renderConfirmationCard``'s sub-threads section
calls when ``parent_relationship === 'group'`` (i.e., the user is
looking at an umbrella thread). The rendered grid shows:

- **One column per child sub-thread** (each child = "a group" — e.g.
  "Code", "Research", "Ungrouped"). The child is itself a normal
  Thread with FSM/intent/actions; clicking its column header drills
  into that child via ``threadsPushPath`` (standard navigation).
- **Each column body shows the child's** ``context_items`` **as
  draggable cards**. Items are pipeline-specific:

  - Chrome: tabs (id, label=title, payload.url)
  - Journal: segmented lines (id, label=first line, payload.raw_text)

Drag an item card from column A to column B → ``move_item`` (POST
``/api/threads/<src>/move_item``) rewrites the ``context_items``
tuples on both children. Optimistic update applies first; SSE
``thread.state_changed`` triggers a debounced ``GET /groups``
reconciliation.

Module-scoped state (survives morphdom re-renders)
--------------------------------------------------

- ``window._groupState.selected: Set<item_id>`` — currently selected
  ContextItem ids; cleared on any successful move.
- ``window._groupState.dragSource: item_id | null``
- ``window._groupState.lastFocused: item_id | null``
- ``window._groupState.groupsByUmbrella: { umbrella_id: groups[] }``
  — the cache. ``groups[i]`` is a child render dict with its
  ``context_items`` array.
- ``window._groupStateBusWired`` — once-guard for the SSE handler.

Toasts use ``_groupToast`` (self-contained, no workflow-views
roundtrip) so dismiss never 404s.
"""

from __future__ import annotations


def script() -> str:
    return r"""
(function () {
    if (!window._groupState) {
        window._groupState = {
            selected: new Set(),
            dragSource: null,
            lastFocused: null,
            groupsByUmbrella: {},
            errorByUmbrella: {},
            // Per-source action library (universal + per-source
            // descriptors). Keyed by umbrella id; same shape as the
            // /groups endpoint's ``action_options`` field. Populated
            // alongside groupsByUmbrella on each lazy-fetch / refresh.
            actionOptionsByUmbrella: {},
            // Which column's chip dropdown is currently open. Set on
            // dropdown open; cleared on outside click / selection /
            // re-render.
            openActionChipFor: null,
        };
    }

    function _esc(s) {
        const d = document.createElement("div");
        d.textContent = s == null ? "" : String(s);
        return d.innerHTML;
    }

    // Self-contained transient toast — does NOT register a
    // workflow-view, so there's no /api/workflow-views/.../dismiss
    // 404 when it goes away. Three-second auto-dismiss; multiple
    // stack vertically.
    function _groupToast(title, body) {
        let host = document.getElementById('threads-group-toast-host');
        if (!host) {
            host = document.createElement('div');
            host.id = 'threads-group-toast-host';
            document.body.appendChild(host);
        }
        const t = document.createElement('div');
        t.className = 'threads-group-toast';
        t.innerHTML = '<div class="threads-group-toast-title">'
            +   _esc(title)
            + '</div>'
            + (body
                ? '<div class="threads-group-toast-body">'
                    + _esc(body) + '</div>'
                : '');
        host.appendChild(t);
        requestAnimationFrame(() => t.classList.add('show'));
        setTimeout(() => {
            t.classList.remove('show');
            setTimeout(() => { try { t.remove(); } catch(e) {} }, 200);
        }, 3000);
    }

    // ---- Cache primitives -------------------------------------------

    function _setGroupsForUmbrella(umbrellaId, groups, actionOptions) {
        window._groupState.groupsByUmbrella[umbrellaId] = groups;
        if (actionOptions !== undefined) {
            window._groupState.actionOptionsByUmbrella[umbrellaId] =
                Array.isArray(actionOptions) ? actionOptions : [];
        }
        delete window._groupState.errorByUmbrella[umbrellaId];
    }

    function _replaceGroupsInPlace(umbrellaId, fresh, actionOptions) {
        // Mutate the existing array in place when possible so any
        // references held by event-bus handlers stay valid.
        const cache = window._groupState.groupsByUmbrella;
        const existing = cache[umbrellaId];
        if (existing && Array.isArray(existing)) {
            existing.length = 0;
            for (const g of fresh) existing.push(g);
            if (actionOptions !== undefined) {
                window._groupState.actionOptionsByUmbrella[umbrellaId] =
                    Array.isArray(actionOptions) ? actionOptions : [];
            }
        } else {
            _setGroupsForUmbrella(umbrellaId, fresh, actionOptions);
        }
    }

    function _refreshGroups(umbrellaId) {
        return fetch('/api/threads/' + encodeURIComponent(umbrellaId)
                     + '/groups')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                _replaceGroupsInPlace(
                    umbrellaId,
                    data.groups || [],
                    data.action_options,
                );
                if (typeof window._renderActiveThread === "function") {
                    window._renderActiveThread();
                }
            })
            .catch(err => {
                console.warn('[group-view] refresh failed:', err);
            });
    }

    // ---- Optimistic move (item-level) -------------------------------
    //
    // Pulls a ContextItem from src.context_items, appends to
    // dest.context_items. The cached groups arrays are mutated in
    // place. Returns true if applied; false if the move couldn't be
    // resolved locally (e.g., item not found in cache).
    function _applyOptimisticMove(itemIds, srcThreadIds, destThreadId) {
        const groupsCache = window._groupState.groupsByUmbrella;
        // Find the umbrella that contains all of src + dest (we expect
        // exactly one).
        let groups = null;
        for (const key in groupsCache) {
            const arr = groupsCache[key];
            if (!arr) continue;
            const ids = new Set(arr.map(g => g.thread_id));
            if (ids.has(destThreadId)
                && srcThreadIds.every(s => ids.has(s))) {
                groups = arr;
                break;
            }
        }
        if (!groups) return false;
        const dest = groups.find(g => g.thread_id === destThreadId);
        if (!dest) return false;
        const movedItems = [];
        for (let i = 0; i < itemIds.length; i++) {
            const itemId = itemIds[i];
            const srcId = srcThreadIds[i];
            const src = groups.find(g => g.thread_id === srcId);
            if (!src) continue;
            const items = src.context_items || [];
            const ix = items.findIndex(it => it.id === itemId);
            if (ix < 0) continue;
            const [pulled] = items.splice(ix, 1);
            movedItems.push(pulled);
        }
        if (movedItems.length === 0) return false;
        if (!dest.context_items) dest.context_items = [];
        for (const it of movedItems) dest.context_items.push(it);
        return true;
    }

    // ---- SSE wiring (one-time) --------------------------------------
    //
    // Debounce the refresh — a cascade of FSM events from one move op
    // (e.g., child cleanup → parent advance) coalesces into one
    // /groups GET.
    let _refreshTimer = null;
    function _scheduleRefreshForActive() {
        if (_refreshTimer) clearTimeout(_refreshTimer);
        _refreshTimer = setTimeout(() => {
            _refreshTimer = null;
            const state = window._threadsState;
            if (!state || !state.path || state.path.length === 0) return;
            const activeId = state.path[state.path.length - 1];
            // Refresh only if the active view is currently an
            // umbrella with a populated cache.
            const cache = window._groupState.groupsByUmbrella;
            if (!cache[activeId]) return;
            _refreshGroups(activeId);
        }, 250);
    }

    if (!window._groupStateBusWired) {
        window._groupStateBusWired = true;
        function _wire() {
            if (!window.eventBus
                || typeof window.eventBus.on !== "function") {
                requestAnimationFrame(_wire);
                return;
            }
            window.eventBus.on("thread.state_changed", (payload) => {
                try {
                    const tid = payload && payload.thread_id;
                    if (!tid) return;
                    // Relevant if this thread is an umbrella we have
                    // cached, OR a child of one.
                    const cache = window._groupState.groupsByUmbrella;
                    let touched = false;
                    if (cache[tid]) {
                        touched = true;
                    } else {
                        for (const umbId in cache) {
                            const arr = cache[umbId] || [];
                            if (arr.some(g => g.thread_id === tid)) {
                                touched = true; break;
                            }
                        }
                    }
                    if (touched) _scheduleRefreshForActive();
                } catch (e) {
                    console.warn('[group-view] bus handler:', e);
                }
            });
        }
        _wire();
    }

    // ---- Public API: render the columns under the umbrella's
    //      Sub-threads section ----------------------------------------

    window.renderGroupSubThreads = function (umbrella) {
        if (!umbrella) {
            return '<div class="threads-group-empty">'
                +   'No umbrella thread loaded.'
                + '</div>';
        }
        const umbrellaId = umbrella.thread_id;
        const cached = window._groupState.groupsByUmbrella[umbrellaId];
        if (cached) {
            return _renderColumns(umbrella, cached);
        }
        const failed = window._groupState.errorByUmbrella[umbrellaId];
        if (failed) {
            return _renderFetchError(umbrellaId, failed);
        }
        // Lazy-fetch.
        fetch('/api/threads/' + encodeURIComponent(umbrellaId) + '/groups')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                _setGroupsForUmbrella(
                    umbrellaId,
                    data.groups || [],
                    data.action_options,
                );
                if (typeof window._renderActiveThread === "function") {
                    window._renderActiveThread();
                }
            })
            .catch(err => {
                window._groupState.errorByUmbrella[umbrellaId] =
                    String(err);
                if (typeof window._renderActiveThread === "function") {
                    window._renderActiveThread();
                }
            });
        return '<div class="threads-group-loading">'
            +   'Loading group columns...'
            + '</div>';
    };

    function _renderFetchError(umbrellaId, msg) {
        return '<div class="threads-group-empty '
            +   'threads-group-fetch-error">'
            + '<h3>Couldn’t load groups</h3>'
            + '<p>' + _esc(msg) + '</p>'
            + '<button class="threads-retry-btn" '
            +   wbActAttrs('threadsGroupRetryFetch', {umbrellaId: umbrellaId})
            +   '>Retry</button>'
            + '</div>';
    }

    window.wbAction('threadsGroupRetryFetch', function (el) {
        var umbrellaId = el.dataset.umbrellaId;
        delete window._groupState.errorByUmbrella[umbrellaId];
        window._renderActiveThread && window._renderActiveThread();
    });

    function _renderColumns(umbrella, groups) {
        const umbrellaId = umbrella.thread_id;
        const selCount = window._groupState.selected.size;
        // Hide terminal children from the column grid entirely —
        // dismissing a group via the column header X (or via the
        // action chip's "Dismiss") should make the column disappear,
        // not linger as a "Dismissed" pill. The audit trail
        // (KIND_GROUP_DELETED on the umbrella + the child's terminal
        // state in the standard threads list) is preserved
        // server-side; this is a UI-only filter.
        const isTerminal = g => {
            const s = (g.fsm_state || '').toLowerCase();
            return s === 'done' || s === 'dismissed' || s === 'handed_off';
        };
        const visibleGroups = groups.filter(g => !isTerminal(g));
        // Approve-all button: counts non-terminal children only,
        // since terminal ones are already done.
        const liveChildren = visibleGroups;
        const actionOptions = window._groupState
            .actionOptionsByUmbrella[umbrellaId] || [];
        let html = '<div class="threads-group-toolbar">'
            + (liveChildren.length > 0
                ? '<button class="threads-group-approve-all" '
                    + 'title="Run Accept on every non-terminal group" '
                    + wbActAttrs('threadsGroupApproveAll', {umbrellaId: umbrellaId})
                    + '>'
                    + 'Approve all (' + liveChildren.length + ')'
                + '</button>'
                : '')
            + '<div class="threads-group-selection-bar'
            +   (selCount > 0 ? ' show' : '') + '">'
            +   '<span class="count">'
            +     selCount + ' selected'
            +   '</span>'
            +   '<span class="hint">'
            +     'drag to another column to move &middot; '
            +     '<kbd>m</kbd> move-to &middot; <kbd>Esc</kbd> clear'
            +   '</span>'
            + '</div>'
            + '</div>'
            + '<div class="threads-group-columns">';
        if (visibleGroups.length === 0) {
            html += '<div class="threads-group-empty-col" '
                +   'style="flex:1 1 auto;">'
                +   (groups.length === 0
                        ? 'No groups yet. Drop here to create the first one.'
                        : 'All groups dismissed. Drop here to start fresh.')
                + '</div>';
        } else {
            for (const g of visibleGroups) {
                html += _renderColumn(g, actionOptions);
            }
        }
        html += _renderNewGroupZone(umbrellaId);
        html += '</div>';
        return html;
    }

    // ---- Action chip --------------------------------------------------
    //
    // The chip lives in the column header (between the meta line and
    // the items list). It shows the current proposed_action's label,
    // and on click toggles a dropdown of every per_group action in the
    // umbrella's action_options. Selecting one POSTs to
    // /api/threads/<id>/set_action_proposal and refreshes the cache.
    //
    // We read the current proposal off the standard render dict's
    // ``actions[0]`` (which is the latest action_inferred event's
    // payload — the runner's synthetic proposal lands there too).

    function _currentActionForChild(child) {
        const actions = child.actions || [];
        if (actions.length === 0) return null;
        const a = actions[0];
        // The render layer flattens payload.name to actions[0].name in
        // some shapes; be defensive about both.
        const name = a.name || (a.payload && a.payload.name) || "";
        if (!name) return null;
        return {
            capability_name: name,
            rationale: a.rationale || (a.data && a.data.rationale) || null,
        };
    }

    function _findActionDescriptor(capabilityName, actionOptions) {
        if (!capabilityName) return null;
        for (const d of actionOptions) {
            if (d.capability_name === capabilityName) return d;
        }
        return null;
    }

    function _renderActionChip(child, actionOptions) {
        const sId = child.thread_id;
        const perGroup = (actionOptions || []).filter(
            d => d.cardinality === "per_group",
        );
        if (perGroup.length === 0) return "";
        const current = _currentActionForChild(child);
        const currentDescriptor = current
            ? _findActionDescriptor(current.capability_name, perGroup)
            : null;
        const chipLabel = currentDescriptor
            ? currentDescriptor.label
            : 'Pick action';
        const isOpen = window._groupState.openActionChipFor === sId;

        let html = '<div class="threads-group-action-chip-wrap">'
            + '<button class="threads-group-action-chip'
            +   (currentDescriptor ? ' has-proposal' : '')
            +   (isOpen ? ' open' : '') + '" '
            +   'title="' + _esc(currentDescriptor
                ? (current.rationale || currentDescriptor.description)
                : 'Pick a per-group action') + '" '
            +   wbActAttrs('threadsGroupToggleActionChip', {threadId: sId})
            +   '>'
            +   '<span class="threads-group-action-chip-arrow">&rarr;</span> '
            +   _esc(chipLabel)
            +   ' <span class="threads-group-action-chip-caret">&#9662;</span>'
            + '</button>';

        if (isOpen) {
            html += '<div class="threads-group-action-chip-menu" '
                +   'data-on-click="wbNoop">';
            for (const d of perGroup) {
                const isCurrent = currentDescriptor
                    && currentDescriptor.capability_name === d.capability_name;
                html += '<button class="threads-group-action-chip-option'
                    +     (isCurrent ? ' current' : '') + '" '
                    +     'title="' + _esc(d.description || '') + '" '
                    +     wbActAttrs('threadsGroupSetActionProposal', {
                                threadId: sId,
                                capabilityName: d.capability_name,
                            })
                    +     '>'
                    +     '<span class="label">' + _esc(d.label) + '</span>'
                    +     (d.description
                            ? '<span class="desc">'
                                + _esc(d.description.length > 80
                                    ? d.description.slice(0, 77) + '...'
                                    : d.description)
                                + '</span>'
                            : '')
                    + '</button>';
            }
            // Add a "Clear proposal" affordance when a proposal exists.
            if (currentDescriptor) {
                html += '<button class="threads-group-action-chip-option '
                    +     'clear" '
                    +     wbActAttrs('threadsGroupClearActionProposal', {threadId: sId})
                    +     '>'
                    +     '<span class="label">Clear proposal</span>'
                    +     '<span class="desc">'
                    +       'No batch action; user reviews individually.'
                    +     '</span>'
                    + '</button>';
            }
            html += '</div>';
        }
        html += '</div>';
        return html;
    }

    window.threadsGroupToggleActionChip = function (threadId) {
        if (window._groupState.openActionChipFor === threadId) {
            window._groupState.openActionChipFor = null;
        } else {
            window._groupState.openActionChipFor = threadId;
        }
        if (typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
    };

    window.wbAction('threadsGroupToggleActionChip', function (el, e) {
        e.stopPropagation();
        window.threadsGroupToggleActionChip(el.dataset.threadId);
    });

    window.threadsGroupSetActionProposal = function (threadId, capabilityName) {
        // capabilityName === null → clear
        const body = capabilityName === null
            ? { capability_name: null }
            : { capability_name: capabilityName, confidence: 1.0 };
        // Close the dropdown immediately so the next render reflects.
        window._groupState.openActionChipFor = null;
        fetch('/api/threads/' + encodeURIComponent(threadId)
              + '/set_action_proposal', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        })
        .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
        .then(({ ok, body }) => {
            if (!ok) {
                _groupToast(
                    'Action update failed',
                    body.error || 'Could not update action proposal',
                );
                return;
            }
            // Invalidate the per-thread render cache for the target
            // child — without this, the right-pane editor + the
            // detail view stay painted with the OLD action_inferred
            // because they read from _threadDetailCache, which is
            // independent of the umbrella's groupsByUmbrella cache
            // that _refreshGroups updates.
            try {
                if (typeof window.invalidateThreadCache === 'function') {
                    window.invalidateThreadCache(threadId);
                }
            } catch (e) { /* best-effort */ }
            // Refresh the active umbrella's groups cache so the
            // column-header chip re-paints with the new proposal.
            // When the user is at the umbrella view, the active path's
            // last entry IS the umbrella. When the user has drilled
            // into a sub-thread, the umbrella is the prior entry —
            // ``state.path = [umbrella_id, sub_thread_id]`` — and the
            // last entry is the threadId we just updated, NOT the
            // umbrella. Refreshing groups on a non-umbrella 404s and
            // leaves the chip stale.
            const state = window._threadsState;
            const path = (state && state.path) || [];
            let umbrellaId = null;
            if (path.length >= 2 && path[path.length - 1] === threadId) {
                umbrellaId = path[path.length - 2];
            } else if (path.length >= 1) {
                umbrellaId = path[path.length - 1];
            }
            if (umbrellaId) {
                _refreshGroups(umbrellaId);
            }
            // Trigger a re-render of the active thread view so the
            // right-pane editor picks up the new action_inferred.
            // _renderActiveThread() walks the same code path as the
            // initial render — it'll see the invalidated cache and
            // re-fetch /api/threads/<id>.
            if (typeof window._renderActiveThread === 'function') {
                window._renderActiveThread();
            }
        })
        .catch(e => _groupToast('Action update failed', String(e)));
    };

    window.wbAction('threadsGroupSetActionProposal', function (el, e) {
        e.stopPropagation();
        window.threadsGroupSetActionProposal(
            el.dataset.threadId, el.dataset.capabilityName,
        );
    });

    window.wbAction('threadsGroupClearActionProposal', function (el, e) {
        e.stopPropagation();
        window.threadsGroupSetActionProposal(el.dataset.threadId, null);
    });

    // Close the chip dropdown when the user clicks elsewhere on the
    // page. Wired once at module load.
    if (!window._threadsGroupChipDismissInstalled) {
        window._threadsGroupChipDismissInstalled = true;
        document.addEventListener('click', function () {
            if (window._groupState.openActionChipFor) {
                window._groupState.openActionChipFor = null;
                if (typeof window._renderActiveThread === "function") {
                    window._renderActiveThread();
                }
            }
        });
    }

    function _renderColumn(child, actionOptions) {
        const items = child.context_items || [];
        const sId = child.thread_id;
        const stateLabel = child.fsm_state || "";
        const intentText = (child.intent && child.intent.text) || "";
        const showIntent = intentText && intentText !== child.title;
        const itemCount = items.length;
        actionOptions = actionOptions || [];
        let html = '<div class="threads-group-column" '
            + 'data-parent-id="' + _esc(sId) + '" '
            + 'data-on-dragover="threadsDragOver" '
            + 'data-on-dragleave="threadsDragLeave" '
            + 'data-on-drop="threadsDropOnColumn">'
            + '<div class="threads-group-column-header '
            +   'threads-group-column-header-clickable" '
            +   'role="link" tabindex="0" '
            +   'title="Open ' + _esc(child.title || sId) + '" '
            +   wbActAttrs('threadsGroupHeaderClick', {threadId: sId})
            +   '>'
            +   '<button class="threads-group-column-delete-x" '
            +     'title="Delete group sub-thread" '
            +     wbActAttrs('threadsGroupDeleteSubthread', {
                        threadId: sId,
                        hadItems: itemCount > 0 ? 'true' : 'false',
                    })
            +     '>'
            +     '&times;'
            +   '</button>'
            +   '<div class="threads-group-column-title-row">'
            +     '<span class="threads-group-column-title">'
            +       _esc(child.title || sId) + '</span>'
            +     (stateLabel
                    ? '<span class="threads-group-column-state">'
                        + _esc(stateLabel) + '</span>'
                    : '')
            +     '<span class="threads-group-column-enter" '
            +       'aria-hidden="true">&rarr;</span>'
            +   '</div>';
        if (showIntent) {
            html += '<div class="threads-group-column-intent" '
                +   'title="' + _esc(intentText) + '">'
                +   _esc(intentText.length > 110
                            ? intentText.slice(0, 107) + '...' : intentText)
                + '</div>';
        }
        html += '<div class="threads-group-column-meta">'
            +     itemCount + ' item' + (itemCount === 1 ? '' : 's')
            +   '</div>'
            + _renderActionChip(child, actionOptions)
            + '</div>'
            + '<ul class="threads-group-items">';
        if (itemCount === 0) {
            html += '<li class="threads-group-empty-col">'
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
        const iId = item.id;
        const selected = window._groupState.selected.has(iId);
        const label = item.label || iId;
        const url = (item.payload && item.payload.url) || '';
        const source = item.source || '';
        const type = item.type || '';
        let html = '<li class="threads-group-item'
            + (selected ? ' selected' : '') + '" '
            + 'data-item-id="' + _esc(iId) + '" '
            + 'data-parent-id="' + _esc(parentId) + '" '
            + 'draggable="true" '
            + 'data-on-dragstart="threadsItemDragStart" '
            + 'data-on-dragend="threadsItemDragEnd" '
            + 'data-on-click="threadsGroupItemClick">'
            + '<div class="threads-group-item-handle" '
            +   'title="Drag to move to another group">&#8801;</div>'
            + '<div class="threads-group-item-body">'
            +   '<div class="threads-group-item-title" '
            +     'title="' + _esc(label) + '">'
            +     _esc(label.length > 90
                        ? label.slice(0, 87) + '...' : label)
            +   '</div>';
        if (url) {
            html += '<div class="threads-group-item-url" '
                +   'title="' + _esc(url) + '">'
                +   _esc(url.length > 80 ? url.slice(0, 77) + '...' : url)
                + '</div>';
        }
        if (source || type) {
            html += '<div class="threads-group-item-meta">'
                +   _esc(source)
                +   (source && type ? ' &middot; ' : '')
                +   _esc(type)
                + '</div>';
        }
        html += '</div></li>';
        return html;
    }

    function _renderNewGroupZone(umbrellaId) {
        return '<div class="threads-group-newzone" '
            + 'data-umbrella-id="' + _esc(umbrellaId) + '" '
            + 'data-on-dragover="threadsDragOver" '
            + 'data-on-dragleave="threadsDragLeave" '
            + 'data-on-drop="threadsDropOnNewZone">'
            + '<div class="threads-group-newzone-icon">+</div>'
            + 'Drop here to create a new group'
            + '</div>';
    }

    // ---- Drag-and-drop handlers ------------------------------------

    window.threadsGroupDragStart = function (ev, itemId) {
        const sel = window._groupState.selected;
        if (!sel.has(itemId)) {
            sel.clear();
            sel.add(itemId);
            document.querySelectorAll('.threads-group-item.selected')
                .forEach(el => el.classList.remove('selected'));
            const me = document.querySelector(
                '.threads-group-item[data-item-id="' + itemId + '"]'
            );
            if (me) me.classList.add('selected');
        }
        window._groupState.dragSource = itemId;
        ev.dataTransfer.effectAllowed = "move";
        try { ev.dataTransfer.setData("text/plain", itemId); } catch(e) {}
        document.querySelectorAll('.threads-group-item.selected')
            .forEach(el => el.classList.add('dragging-multi'));
    };

    window.threadsGroupDragEnd = function (ev, itemId) {
        window._groupState.dragSource = null;
        document.querySelectorAll('.dragging-multi, .drag-over')
            .forEach(el => {
                el.classList.remove('dragging-multi');
                el.classList.remove('drag-over');
            });
    };

    window.threadsGroupDropOnNewZone = function (ev, umbrellaId) {
        ev.preventDefault();
        const zone = ev.currentTarget || ev.target.closest(
            '.threads-group-newzone'
        );
        if (zone) zone.classList.remove('drag-over');
        const sel = Array.from(window._groupState.selected);
        if (sel.length === 0) {
            // Allow user to spawn an empty group with no items
            // selected — useful for "I want to start fresh and put
            // things here later."
            const labelOnly = window.prompt(
                'Name for the new group? (Leave blank for "New group")',
                '',
            );
            if (labelOnly === null) return;
            _spawnEmptyGroup(umbrellaId, (labelOnly || '').trim());
            return;
        }
        const label = window.prompt(
            'Name for the new group? (Leave blank for "New group")', '',
        );
        if (label === null) return;
        const cleaned = (label || '').trim() || 'New group';
        fetch('/api/threads/' + encodeURIComponent(umbrellaId)
              + '/spawn_empty_group', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label: cleaned }),
        })
        .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
        .then(({ ok, body }) => {
            if (!ok) {
                _groupToast('New group failed',
                    body.error || 'Could not create group');
                return;
            }
            const newGroupId = body.new_thread_id;
            // We need to fetch fresh groups so the new column appears
            // in cache, then move the items. Refresh first.
            return _refreshGroups(umbrellaId).then(() => {
                _moveItems(sel, newGroupId);
            });
        })
        .catch(e => {
            _groupToast('New group failed', String(e));
        });
    };

    function _spawnEmptyGroup(umbrellaId, label) {
        const cleaned = label || 'New group';
        fetch('/api/threads/' + encodeURIComponent(umbrellaId)
              + '/spawn_empty_group', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label: cleaned }),
        })
        .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
        .then(({ ok, body }) => {
            if (!ok) {
                _groupToast('New group failed',
                    body.error || 'Could not create group');
                return;
            }
            _refreshGroups(umbrellaId);
        })
        .catch(e => _groupToast('New group failed', String(e)));
    }

    window.threadsGroupDropOnColumn = function (ev, destParentId) {
        ev.preventDefault();
        const col = ev.currentTarget || ev.target.closest(
            '.threads-group-column'
        );
        if (col) col.classList.remove('drag-over');
        const sel = Array.from(window._groupState.selected);
        if (sel.length === 0) return;
        // Filter: don't move items already in this destination.
        const filtered = sel.filter(itemId => {
            const el = document.querySelector(
                '.threads-group-item[data-item-id="' + itemId + '"]'
            );
            return el && el.dataset.parentId !== destParentId;
        });
        if (filtered.length === 0) {
            window._groupState.selected.clear();
            window._renderActiveThread && window._renderActiveThread();
            return;
        }
        _moveItems(filtered, destParentId);
    };

    function _moveItems(itemIds, destParentId) {
        // Map each item id back to its current source column.
        const srcByItem = {};
        for (const iId of itemIds) {
            const el = document.querySelector(
                '.threads-group-item[data-item-id="' + iId + '"]'
            );
            if (el) srcByItem[iId] = el.dataset.parentId;
        }
        const total = itemIds.length;
        const srcThreadIds = itemIds.map(i => srcByItem[i]);
        const optimistic = _applyOptimisticMove(
            itemIds, srcThreadIds, destParentId,
        );
        window._groupState.selected.clear();
        if (optimistic && typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
        let ok = 0, failed = 0;
        const failures = [];
        const promises = itemIds.map(iId => {
            const srcId = srcByItem[iId];
            if (!srcId) {
                failed++;
                failures.push('source unknown for ' + iId);
                return Promise.resolve();
            }
            return fetch('/api/threads/' + encodeURIComponent(srcId)
                         + '/move_item', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    item_id: iId,
                    dest_thread_id: destParentId,
                }),
            })
            .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
            .then(({ ok: ok2, body }) => {
                if (ok2) ok++;
                else { failed++; failures.push(body.error || 'unknown'); }
            })
            .catch(e => { failed++; failures.push(String(e)); });
        });
        Promise.all(promises).then(() => {
            // Invalidate the per-thread detail cache for both source
            // and destination threads — otherwise drilling into either
            // sub-thread shows the pre-move ``context_items`` from the
            // stale cache. The umbrella's ``groupsByUmbrella`` cache
            // is updated by the optimistic step + SSE refresh, but
            // ``_threadDetailCache`` is independent (tabs/threads/main
            // owns it) and needs explicit invalidation.
            try {
                if (typeof window.invalidateThreadCache === "function") {
                    const touched = new Set();
                    touched.add(destParentId);
                    for (const iId of itemIds) {
                        const src = srcByItem[iId];
                        if (src) touched.add(src);
                    }
                    for (const tid of touched) {
                        window.invalidateThreadCache(tid);
                    }
                }
            } catch (e) {
                console.warn(
                    '[group-view] thread-detail cache invalidate failed:',
                    e,
                );
            }
            // ONLY toast on partial failure. Successful moves are
            // visually obvious right under the cursor; the user
            // already saw the optimistic update land.
            if (failed > 0) {
                _groupToast(
                    'Move partial',
                    'Moved ' + ok + ' / ' + total
                        + ' (' + failed + ' failed: '
                        + (failures[0] || 'unknown')
                        + (failures.length > 1 ? ', ...' : '') + ')',
                );
                // Force a hard refresh — optimistic state may be
                // ahead of the server.
                const state = window._threadsState;
                if (state && state.path && state.path.length > 0) {
                    _refreshGroups(state.path[state.path.length - 1]);
                }
            }
            // SSE handler will _scheduleRefreshForActive when cascade
            // events arrive (none expected for plain move-item, but
            // the umbrella's parent_event_id bumps from move_item land
            // as state-changed too).
        });
    }

    // ---- Header click + delete X --------------------------------------

    window.threadsGroupHeaderClick = function (ev, threadId) {
        // Plain click on header → drill into that group sub-thread
        // via standard navigation.
        if (typeof window.threadsPushPath === "function") {
            window.threadsPushPath(threadId);
        }
    };

    window.wbAction('threadsGroupHeaderClick', function (el, e) {
        window.threadsGroupHeaderClick(e, el.dataset.threadId);
    });

    window.threadsGroupDeleteSubthread = function (threadId, hadItems) {
        if (hadItems && !window.confirm(
            'Delete this group sub-thread? It still has items inside; '
            + 'they will be dismissed along with it.\n\nIf you wanted to '
            + 'reassign them, drag them to another column first.'
        )) return;
        fetch('/api/threads/' + encodeURIComponent(threadId)
              + '/delete_group_subthread', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        })
        .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
        .then(({ ok, body }) => {
            if (!ok) {
                _groupToast('Delete failed',
                    body.error || 'Could not delete group sub-thread');
                return;
            }
            const umbrellaId = body.umbrella_id;
            if (umbrellaId) _refreshGroups(umbrellaId);
        })
        .catch(e => _groupToast('Delete failed', String(e)));
    };

    window.wbAction('threadsGroupDeleteSubthread', function (el, e) {
        e.stopPropagation();
        window.threadsGroupDeleteSubthread(
            el.dataset.threadId, el.dataset.hadItems === 'true',
        );
    });

    // ---- Approve all -------------------------------------------------

    window.threadsGroupApproveAll = function (umbrellaId) {
        if (!window.confirm(
            'Approve every non-terminal group? Each group will run its '
            + 'proposed actions.'
        )) return;
        fetch('/api/threads/' + encodeURIComponent(umbrellaId)
              + '/approve_all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        })
        .then(r => r.json().then(b => ({ ok: r.ok, body: b })))
        .then(({ ok, body }) => {
            if (!ok) {
                _groupToast('Approve failed',
                    body.error || 'Cascade approve failed');
                return;
            }
            const approved = (body.approved || []).length;
            const failed = (body.failed || []).length;
            const skipped = (body.skipped_terminal || []).length;
            const total = approved + failed;
            const msg = (failed > 0
                ? 'Approved ' + approved + ' / ' + total
                    + ' (' + failed + ' failed: '
                    + (body.failed[0].error || 'unknown') + ')'
                : 'Approved ' + approved + ' group'
                    + (approved === 1 ? '' : 's')
                    + (skipped > 0
                        ? ' (' + skipped + ' already terminal)'
                        : ''));
            _groupToast('Approve all', msg);
            _refreshGroups(umbrellaId);
        })
        .catch(e => _groupToast('Approve failed', String(e)));
    };

    window.wbAction('threadsGroupApproveAll', function (el) {
        window.threadsGroupApproveAll(el.dataset.umbrellaId);
    });

    // ---- Selection handlers (item-level) ----------------------------

    window.threadsGroupItemClick = function (ev, itemId) {
        // Shift/Ctrl/Cmd-click → multi-select for batch ops. Plain
        // click does nothing: items are observations (URLs / journal
        // lines), not navigable threads. To "enter" a group, click
        // its column header. To move an item, drag it.
        if (ev.shiftKey) {
            ev.preventDefault();
            _extendSelection(itemId);
            _refreshSelectionClasses();
            return;
        }
        if (ev.ctrlKey || ev.metaKey) {
            ev.preventDefault();
            window._groupState.selected.has(itemId)
                ? window._groupState.selected.delete(itemId)
                : window._groupState.selected.add(itemId);
            window._groupState.lastFocused = itemId;
            _refreshSelectionClasses();
            return;
        }
        // Plain click — no-op. Don't preventDefault either; if the
        // item card has an embedded link (e.g., URL on a Chrome tab)
        // future polish could let it open. For now, item cards are
        // pure drag handles.
    };

    window.wbAction('threadsGroupItemClick', function (el, e) {
        window.threadsGroupItemClick(e, el.dataset.itemId);
    });

    function _extendSelection(toItemId) {
        const all = Array.from(document.querySelectorAll(
            '.threads-group-item'
        ));
        const ids = all.map(el => el.dataset.itemId);
        const anchor = window._groupState.lastFocused;
        const a = anchor ? ids.indexOf(anchor) : -1;
        const b = ids.indexOf(toItemId);
        if (b < 0) return;
        if (a < 0) {
            window._groupState.selected.add(toItemId);
            window._groupState.lastFocused = toItemId;
            return;
        }
        const [lo, hi] = a < b ? [a, b] : [b, a];
        for (let i = lo; i <= hi; i++) {
            window._groupState.selected.add(ids[i]);
        }
    }

    function _refreshSelectionClasses() {
        const sel = window._groupState.selected;
        document.querySelectorAll('.threads-group-item').forEach(el => {
            const iId = el.dataset.itemId;
            el.classList.toggle('selected', sel.has(iId));
        });
        const bar = document.querySelector(
            '.threads-group-selection-bar'
        );
        if (bar) {
            const cnt = bar.querySelector('.count');
            if (cnt) cnt.textContent = sel.size + ' selected';
            bar.classList.toggle('show', sel.size > 0);
        }
    }

    // ---- Keyboard handler — extends tabs/threads/main's j/k --------

    if (!window._threadsGroupKbdInstalled) {
        window._threadsGroupKbdInstalled = true;
        document.addEventListener("keydown", function (ev) {
            const state = window._threadsState;
            if (!state || !state.path || state.path.length === 0) return;
            const activeId = state.path[state.path.length - 1];
            const cached = (window._threadDetailCache || {})[activeId];
            // Only fire on group umbrella threads.
            if (!cached || cached.parent_relationship !== "group") return;
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
            '.threads-group-item.threads-kbd-focus'
        ) || document.querySelector(
            '.threads-group-item[data-item-id="'
            + (window._groupState.lastFocused || '') + '"]'
        );
        if (!focusedEl) return;
        const iId = focusedEl.dataset.itemId;
        if (window._groupState.selected.has(iId)) {
            window._groupState.selected.delete(iId);
        } else {
            window._groupState.selected.add(iId);
            window._groupState.lastFocused = iId;
        }
    }

    function _stepFocus(delta, extendSelection) {
        const all = Array.from(document.querySelectorAll(
            '.threads-group-item'
        ));
        if (all.length === 0) return;
        const focused = window._groupState.lastFocused;
        let idx = focused
            ? all.findIndex(el => el.dataset.itemId === focused)
            : -1;
        if (idx < 0) idx = 0;
        let next = idx + delta;
        if (next < 0) next = 0;
        if (next >= all.length) next = all.length - 1;
        const nextIid = all[next].dataset.itemId;
        if (extendSelection) {
            window._groupState.selected.add(nextIid);
        }
        window._groupState.lastFocused = nextIid;
        all.forEach(el => el.classList.remove('threads-kbd-focus'));
        all[next].classList.add('threads-kbd-focus');
        all[next].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        _refreshSelectionClasses();
    }

    function _promptMove() {
        const sel = Array.from(window._groupState.selected);
        if (sel.length === 0) {
            _groupToast(
                'Nothing selected',
                'Press x to select items first.',
            );
            return;
        }
        const state = window._threadsState;
        const activeId = state.path[state.path.length - 1];
        const groups = (
            window._groupState.groupsByUmbrella[activeId] || []
        );
        // Active source columns = the columns containing the
        // selected items. We want to move SOMEWHERE ELSE.
        const sourceParents = new Set();
        for (const iId of sel) {
            const el = document.querySelector(
                '.threads-group-item[data-item-id="' + iId + '"]'
            );
            if (el) sourceParents.add(el.dataset.parentId);
        }
        const candidates = groups.filter(
            g => !sourceParents.has(g.thread_id)
        );
        if (candidates.length === 0) {
            _groupToast(
                'No other groups',
                'This umbrella only has one group. Drop on the '
                    + '"+ New group" zone to create another.',
            );
            return;
        }
        const lines = candidates.map((g, i) =>
            (i + 1) + ') ' + (g.title || g.thread_id)
        ).join('\n');
        const pick = window.prompt(
            'Move ' + sel.length + ' item' + (sel.length === 1 ? '' : 's')
                + ' to which group?\n\n' + lines + '\n\nEnter a number:',
        );
        const n = parseInt(pick, 10);
        if (!n || n < 1 || n > candidates.length) return;
        _moveItems(sel, candidates[n - 1].thread_id);
    }

    // Drag-and-drop (delegated; dispatcher binds drag* + drop). Columns and
    // the new-group zone carry data-parent-id / data-umbrella-id; item cards
    // carry data-item-id. Behaviour matches the former inline handlers.
    window.wbAction('threadsDragOver', function (el, e) {
        e.preventDefault();
        el.classList.add('drag-over');
    });
    window.wbAction('threadsDragLeave', function (el) {
        el.classList.remove('drag-over');
    });
    window.wbAction('threadsDropOnColumn', function (el, e) {
        threadsGroupDropOnColumn(e, el.dataset.parentId);
    });
    window.wbAction('threadsDropOnNewZone', function (el, e) {
        threadsGroupDropOnNewZone(e, el.dataset.umbrellaId);
    });
    window.wbAction('threadsItemDragStart', function (el, e) {
        threadsGroupDragStart(e, el.dataset.itemId);
    });
    window.wbAction('threadsItemDragEnd', function (el, e) {
        threadsGroupDragEnd(e, el.dataset.itemId);
    });
})();
"""


def styles() -> str:
    return r"""
/* Group-view multi-column layout. Renders inside the
 * standard thread-detail card's "Sub-threads" section when the
 * active thread is a group umbrella (parent_relationship === 'group').
 * Columns are flex children that wrap on narrow viewports.
 */
.threads-group-empty,
.threads-group-loading {
    padding: 2em;
    text-align: center;
    color: var(--text-muted, #888);
}
.threads-group-fetch-error {
    color: var(--text, #ddd);
}

/* Toolbar above the columns. Holds Approve-all + selection bar. */
.threads-group-toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
    flex-wrap: wrap;
}
.threads-group-approve-all {
    background: var(--accent, #4a7fc1);
    color: white;
    border: 1px solid var(--accent, #4a7fc1);
    border-radius: 4px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
}
.threads-group-approve-all:hover {
    filter: brightness(1.1);
}

/* Slim selection bar — only visible while items are selected. */
.threads-group-selection-bar {
    display: none;
    align-items: center;
    gap: 12px;
    padding: 6px 10px;
    background: var(--bg-tertiary, #232323);
    border: 1px solid var(--accent, #4a7fc1);
    border-radius: 6px;
    font-size: 12px;
    color: var(--text, #ddd);
    flex: 1 1 auto;
}
.threads-group-selection-bar.show {
    display: flex;
}
.threads-group-selection-bar .count {
    font-weight: 600;
    color: var(--accent, #4a7fc1);
}
.threads-group-selection-bar .hint {
    color: var(--text-muted, #888);
    font-size: 11px;
}
.threads-group-selection-bar kbd {
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 0 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 10px;
}

.threads-group-columns {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    align-items: flex-start;
}

.threads-group-column {
    flex: 1 1 280px;
    min-width: 240px;
    max-width: 360px;
    background: var(--bg-secondary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    padding: 10px;
    transition: border-color 80ms, background-color 80ms;
    position: relative;
}
.threads-group-column.drag-over {
    border-color: var(--accent, #4a7fc1);
    background: var(--bg-tertiary, #232323);
}

.threads-group-column-header {
    margin-bottom: 8px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border, #333);
    position: relative;
}
.threads-group-column-header-clickable {
    cursor: pointer;
    border-radius: 4px;
    margin: -4px -4px 8px -4px;
    padding: 4px 4px 6px 4px;
    transition: background-color 80ms;
}
.threads-group-column-header-clickable:hover,
.threads-group-column-header-clickable:focus-visible {
    background: var(--bg-tertiary, #232323);
    outline: none;
}

/* X / Delete group sub-thread button — top-right of the header,
 * visible on hover. */
.threads-group-column-delete-x {
    position: absolute;
    top: 0;
    right: 0;
    width: 22px;
    height: 22px;
    border: none;
    background: transparent;
    color: var(--text-muted, #888);
    font-size: 16px;
    line-height: 1;
    cursor: pointer;
    border-radius: 4px;
    opacity: 0;
    transition: opacity 80ms, background 80ms, color 80ms;
}
.threads-group-column-header:hover
    .threads-group-column-delete-x,
.threads-group-column-delete-x:focus-visible {
    opacity: 1;
}
.threads-group-column-delete-x:hover {
    background: rgba(220, 60, 60, 0.18);
    color: #ff8080;
}

.threads-group-column-title-row {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    padding-right: 24px;  /* room for the X button */
}
.threads-group-column-title {
    font-size: 14px;
    font-weight: 600;
}
.threads-group-column-state {
    font-size: 10px;
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 1px 5px;
    color: var(--text-muted, #888);
    text-transform: capitalize;
    white-space: nowrap;
}

/* Arrow on the right side of the title row hints "click to open
 * this group sub-thread". Subtle by default; brightens on header
 * hover. */
.threads-group-column-enter {
    margin-left: auto;
    color: var(--text-muted, #666);
    font-size: 14px;
    line-height: 1;
    transition: color 80ms, transform 80ms;
}
.threads-group-column-header-clickable:hover
    .threads-group-column-enter,
.threads-group-column-header-clickable:focus-visible
    .threads-group-column-enter {
    color: var(--accent, #4a7fc1);
    transform: translateX(2px);
}
.threads-group-column-intent {
    font-size: 11px;
    color: var(--text-muted, #aaa);
    margin-top: 4px;
    line-height: 1.35;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
}
.threads-group-column-meta {
    font-size: 11px;
    color: var(--text-muted, #888);
    margin-top: 4px;
}

.threads-group-items {
    list-style: none;
    margin: 0;
    padding: 0;
    min-height: 60px;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.threads-group-empty-col {
    color: var(--text-muted, #666);
    font-size: 12px;
    font-style: italic;
    padding: 14px 6px;
    text-align: center;
    border: 1px dashed var(--border, #333);
    border-radius: 4px;
}

/* Item card — represents a ContextItem (Chrome tab, journal line,
 * etc.), NOT a thread. */
.threads-group-item {
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
.threads-group-item:hover {
    background: var(--bg-tertiary, #1a1a1a);
}
.threads-group-item.selected {
    border-color: var(--accent, #4a7fc1);
    background: rgba(74, 127, 193, 0.08);
}
.threads-group-item.dragging-multi {
    opacity: 0.5;
}
.threads-group-item.threads-kbd-focus {
    outline: 2px solid var(--accent, #4a7fc1);
    outline-offset: -2px;
}

.threads-group-item-handle {
    color: var(--text-muted, #666);
    font-size: 14px;
    flex: 0 0 auto;
    line-height: 1.2;
    cursor: grab;
}
.threads-group-item-body {
    flex: 1 1 auto;
    min-width: 0;
}
.threads-group-item-title {
    font-size: 13px;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.threads-group-item-url {
    font-size: 11px;
    color: var(--accent, #4a7fc1);
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: ui-monospace, SFMono-Regular, monospace;
}
.threads-group-item-meta {
    font-size: 10px;
    color: var(--text-muted, #888);
    margin-top: 3px;
    text-transform: lowercase;
}

/* Action chip — sits in each column header between the meta line
 * and the items list. Shows the LLM-proposed (or user-overridden)
 * per-group action; click opens a dropdown of every available
 * per-group action.
 *
 * The chip surfaces in two visual states:
 *   - has-proposal: solid accent border, accent text. Communicates
 *     "this column has an action queued; Approve all will run it."
 *   - no proposal: dashed muted border, "Pick action" prompt.
 */
.threads-group-action-chip-wrap {
    position: relative;
    margin-top: 8px;
    /* Stop propagation visually — the chip is inside the
     * column-header-clickable area, but its click handler stops
     * propagation so opening the dropdown doesn't drill into the
     * sub-thread. */
}
.threads-group-action-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: transparent;
    border: 1px dashed var(--border, #333);
    border-radius: 12px;
    color: var(--text-muted, #888);
    padding: 3px 10px;
    font-size: 11px;
    font-family: inherit;
    cursor: pointer;
    transition: border-color 80ms, color 80ms, background-color 80ms;
}
.threads-group-action-chip:hover,
.threads-group-action-chip.open {
    background: var(--bg-tertiary, #232323);
    color: var(--text, #ddd);
    border-color: var(--text-muted, #888);
}
.threads-group-action-chip.has-proposal {
    border-style: solid;
    border-color: var(--accent, #4a7fc1);
    color: var(--accent, #4a7fc1);
    background: rgba(74, 127, 193, 0.06);
}
.threads-group-action-chip.has-proposal:hover {
    background: rgba(74, 127, 193, 0.14);
    color: var(--accent, #4a7fc1);
}
.threads-group-action-chip-arrow {
    font-weight: 600;
}
.threads-group-action-chip-caret {
    font-size: 10px;
    color: inherit;
    margin-left: 2px;
}

.threads-group-action-chip-menu {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    z-index: 10;
    min-width: 240px;
    max-width: 360px;
    /* Cap at ~5 options before scrolling so very long action
     * libraries don't push the menu off the bottom of the screen.
     * Each option is ~52px tall (label + 1-line desc); 5 ≈ 260px,
     * + 4px top padding + 4px bottom padding = 268px. Add a touch
     * of headroom for the optional Clear-proposal row. */
    max-height: 300px;
    overflow-y: auto;
    background: var(--bg-secondary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
    padding: 4px;
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.threads-group-action-chip-option {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 6px 10px;
    cursor: pointer;
    font-family: inherit;
    text-align: left;
    color: var(--text, #ddd);
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.threads-group-action-chip-option:hover {
    background: var(--bg-tertiary, #232323);
    border-color: var(--border, #333);
}
.threads-group-action-chip-option.current {
    background: rgba(74, 127, 193, 0.12);
    border-color: var(--accent, #4a7fc1);
}
.threads-group-action-chip-option .label {
    font-size: 12px;
    font-weight: 600;
}
.threads-group-action-chip-option .desc {
    font-size: 11px;
    color: var(--text-muted, #888);
    line-height: 1.3;
}
.threads-group-action-chip-option.clear {
    border-top: 1px solid var(--border, #333);
    margin-top: 2px;
    padding-top: 8px;
    border-radius: 0 0 4px 4px;
}
.threads-group-action-chip-option.clear .label {
    color: var(--text-muted, #aaa);
    font-weight: 500;
}

.threads-group-newzone {
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
    cursor: pointer;
}
.threads-group-newzone:hover,
.threads-group-newzone.drag-over {
    border-color: var(--accent, #4a7fc1);
    color: var(--accent, #4a7fc1);
    background: rgba(74, 127, 193, 0.05);
}
.threads-group-newzone-icon {
    font-size: 28px;
    line-height: 1;
}

/* Self-contained transient toast — does not register a workflow-view,
 * so it has no /api/workflow-views/.../dismiss round-trip on
 * teardown. */
#threads-group-toast-host {
    position: fixed;
    bottom: 24px;
    right: 24px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 9999;
    pointer-events: none;
}
.threads-group-toast {
    pointer-events: auto;
    min-width: 220px;
    max-width: 360px;
    background: var(--bg-secondary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-left: 3px solid var(--accent, #4a7fc1);
    border-radius: 6px;
    padding: 10px 14px;
    color: var(--text, #ddd);
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    opacity: 0;
    transform: translateY(8px);
    transition: opacity 180ms ease, transform 180ms ease;
}
.threads-group-toast.show {
    opacity: 1;
    transform: translateY(0);
}
.threads-group-toast-title {
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 2px;
}
.threads-group-toast-body {
    font-size: 12px;
    color: var(--text-muted, #aaa);
}
"""
