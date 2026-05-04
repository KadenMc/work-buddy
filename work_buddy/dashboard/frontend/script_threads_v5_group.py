"""Group-view frontend (v2) — multi-column drag/drop layout slotted
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

What used to be here (pre-v2)
-----------------------------

- ``move_thread_to_parent`` operated at thread granularity (each tab
  was a sub-thread). Replaced by item-level :func:`move_item`.
- A "Submit all" button per column. Replaced by an umbrella-level
  ``Approve all`` button (see ``script_threads_v5_card`` —
  ``cascade_approve_umbrella`` runs Accept on every non-terminal
  child).
- "Items moved" success toast on every drag. Removed — the move is
  visible right under the cursor; the toast was noise. Toasts now
  fire ONLY on partial failures.
- A cross-sibling cache keyed by every sibling's thread_id. The new
  cache is keyed by **umbrella_id** (one umbrella per scrape; no
  cross-sibling sharing required).

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


def _group_view_script() -> str:
    return r"""
(function () {
    if (!window._groupState) {
        window._groupState = {
            selected: new Set(),
            dragSource: null,
            lastFocused: null,
            groupsByUmbrella: {},
            errorByUmbrella: {},
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
        let host = document.getElementById('threads-v5-group-toast-host');
        if (!host) {
            host = document.createElement('div');
            host.id = 'threads-v5-group-toast-host';
            document.body.appendChild(host);
        }
        const t = document.createElement('div');
        t.className = 'threads-v5-group-toast';
        t.innerHTML = '<div class="threads-v5-group-toast-title">'
            +   _esc(title)
            + '</div>'
            + (body
                ? '<div class="threads-v5-group-toast-body">'
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

    function _setGroupsForUmbrella(umbrellaId, groups) {
        window._groupState.groupsByUmbrella[umbrellaId] = groups;
        delete window._groupState.errorByUmbrella[umbrellaId];
    }

    function _replaceGroupsInPlace(umbrellaId, fresh) {
        // Mutate the existing array in place when possible so any
        // references held by event-bus handlers stay valid.
        const cache = window._groupState.groupsByUmbrella;
        const existing = cache[umbrellaId];
        if (existing && Array.isArray(existing)) {
            existing.length = 0;
            for (const g of fresh) existing.push(g);
        } else {
            _setGroupsForUmbrella(umbrellaId, fresh);
        }
    }

    function _refreshGroups(umbrellaId) {
        return fetch('/api/threads/' + encodeURIComponent(umbrellaId)
                     + '/groups')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                _replaceGroupsInPlace(umbrellaId, data.groups || []);
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
            return '<div class="threads-v5-group-empty">'
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
                _setGroupsForUmbrella(umbrellaId, data.groups || []);
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
        return '<div class="threads-v5-group-loading">'
            +   'Loading group columns...'
            + '</div>';
    };

    function _renderFetchError(umbrellaId, msg) {
        return '<div class="threads-v5-group-empty '
            +   'threads-v5-group-fetch-error">'
            + '<h3>Couldn’t load groups</h3>'
            + '<p>' + _esc(msg) + '</p>'
            + '<button class="threads-v5-retry-btn" '
            +   'onclick="(function(){'
            +     'delete window._groupState.errorByUmbrella[\''
            +       _esc(umbrellaId) + '\'];'
            +     'window._renderActiveThread '
            +       '&& window._renderActiveThread();'
            +   '})()">Retry</button>'
            + '</div>';
    }

    function _renderColumns(umbrella, groups) {
        const umbrellaId = umbrella.thread_id;
        const selCount = window._groupState.selected.size;
        // Approve-all button: visible when at least one child is
        // non-terminal. The cascade runs Accept on each
        // awaiting_confirmation / awaiting_consent child via
        // /approve_all.
        const liveChildren = groups.filter(g => {
            const s = (g.fsm_state || '').toLowerCase();
            return s !== 'done' && s !== 'dismissed' && s !== 'handed_off';
        });
        let html = '<div class="threads-v5-group-toolbar">'
            + (liveChildren.length > 0
                ? '<button class="threads-v5-group-approve-all" '
                    + 'title="Run Accept on every non-terminal group" '
                    + 'onclick="threadsGroupApproveAll(\''
                    +   _esc(umbrellaId) + '\')">'
                    + 'Approve all (' + liveChildren.length + ')'
                + '</button>'
                : '')
            + '<div class="threads-v5-group-selection-bar'
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
            + '<div class="threads-v5-group-columns">';
        if (groups.length === 0) {
            html += '<div class="threads-v5-group-empty-col" '
                +   'style="flex:1 1 auto;">'
                +   'No groups yet. Drop here to create the first one.'
                + '</div>';
        } else {
            for (const g of groups) {
                html += _renderColumn(g);
            }
        }
        html += _renderNewGroupZone(umbrellaId);
        html += '</div>';
        return html;
    }

    function _renderColumn(child) {
        const items = child.context_items || [];
        const sId = child.thread_id;
        const stateLabel = child.fsm_state || "";
        const intentText = (child.intent && child.intent.text) || "";
        const showIntent = intentText && intentText !== child.title;
        const itemCount = items.length;
        let html = '<div class="threads-v5-group-column" '
            + 'data-parent-id="' + _esc(sId) + '" '
            + 'ondragover="event.preventDefault();'
            +   'this.classList.add(\'drag-over\');" '
            + 'ondragleave="this.classList.remove(\'drag-over\');" '
            + 'ondrop="threadsGroupDropOnColumn(event, \'' + _esc(sId)
            +   '\')">'
            + '<div class="threads-v5-group-column-header '
            +   'threads-v5-group-column-header-clickable" '
            +   'role="link" tabindex="0" '
            +   'title="Open ' + _esc(child.title || sId) + '" '
            +   'onclick="threadsGroupHeaderClick(event, \''
            +     _esc(sId) + '\')" '
            +   'onkeydown="if(event.key===\'Enter\'||event.key===\' \'){'
            +     'event.preventDefault();'
            +     'threadsPushPath(\'' + _esc(sId) + '\')}">'
            +   '<button class="threads-v5-group-column-delete-x" '
            +     'title="Delete group sub-thread" '
            +     'onclick="event.stopPropagation();'
            +       'threadsGroupDeleteSubthread(\''
            +       _esc(sId) + '\', '
            +       (itemCount > 0 ? 'true' : 'false') + ')">'
            +     '&times;'
            +   '</button>'
            +   '<div class="threads-v5-group-column-title-row">'
            +     '<span class="threads-v5-group-column-title">'
            +       _esc(child.title || sId) + '</span>'
            +     (stateLabel
                    ? '<span class="threads-v5-group-column-state">'
                        + _esc(stateLabel) + '</span>'
                    : '')
            +   '</div>';
        if (showIntent) {
            html += '<div class="threads-v5-group-column-intent" '
                +   'title="' + _esc(intentText) + '">'
                +   _esc(intentText.length > 110
                            ? intentText.slice(0, 107) + '...' : intentText)
                + '</div>';
        }
        html += '<div class="threads-v5-group-column-meta">'
            +     itemCount + ' item' + (itemCount === 1 ? '' : 's')
            +   '</div>'
            + '</div>'
            + '<ul class="threads-v5-group-items">';
        if (itemCount === 0) {
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
        const iId = item.id;
        const selected = window._groupState.selected.has(iId);
        const label = item.label || iId;
        const url = (item.payload && item.payload.url) || '';
        const source = item.source || '';
        const type = item.type || '';
        let html = '<li class="threads-v5-group-item'
            + (selected ? ' selected' : '') + '" '
            + 'data-item-id="' + _esc(iId) + '" '
            + 'data-parent-id="' + _esc(parentId) + '" '
            + 'draggable="true" '
            + 'ondragstart="threadsGroupDragStart(event, \''
            +   _esc(iId) + '\')" '
            + 'ondragend="threadsGroupDragEnd(event, \''
            +   _esc(iId) + '\')" '
            + 'onclick="threadsGroupItemClick(event, \''
            +   _esc(iId) + '\')">'
            + '<div class="threads-v5-group-item-handle" '
            +   'title="Drag to move to another group">&#8801;</div>'
            + '<div class="threads-v5-group-item-body">'
            +   '<div class="threads-v5-group-item-title" '
            +     'title="' + _esc(label) + '">'
            +     _esc(label.length > 90
                        ? label.slice(0, 87) + '...' : label)
            +   '</div>';
        if (url) {
            html += '<div class="threads-v5-group-item-url" '
                +   'title="' + _esc(url) + '">'
                +   _esc(url.length > 80 ? url.slice(0, 77) + '...' : url)
                + '</div>';
        }
        if (source || type) {
            html += '<div class="threads-v5-group-item-meta">'
                +   _esc(source)
                +   (source && type ? ' &middot; ' : '')
                +   _esc(type)
                + '</div>';
        }
        html += '</div></li>';
        return html;
    }

    function _renderNewGroupZone(umbrellaId) {
        return '<div class="threads-v5-group-newzone" '
            + 'ondragover="event.preventDefault();'
            +   'this.classList.add(\'drag-over\');" '
            + 'ondragleave="this.classList.remove(\'drag-over\');" '
            + 'ondrop="threadsGroupDropOnNewZone(event, \''
            +   _esc(umbrellaId) + '\')">'
            + '<div class="threads-v5-group-newzone-icon">+</div>'
            + 'Drop here to create a new group'
            + '</div>';
    }

    // ---- Drag-and-drop handlers ------------------------------------

    window.threadsGroupDragStart = function (ev, itemId) {
        const sel = window._groupState.selected;
        if (!sel.has(itemId)) {
            sel.clear();
            sel.add(itemId);
            document.querySelectorAll('.threads-v5-group-item.selected')
                .forEach(el => el.classList.remove('selected'));
            const me = document.querySelector(
                '.threads-v5-group-item[data-item-id="' + itemId + '"]'
            );
            if (me) me.classList.add('selected');
        }
        window._groupState.dragSource = itemId;
        ev.dataTransfer.effectAllowed = "move";
        try { ev.dataTransfer.setData("text/plain", itemId); } catch(e) {}
        document.querySelectorAll('.threads-v5-group-item.selected')
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
            '.threads-v5-group-newzone'
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
            '.threads-v5-group-column'
        );
        if (col) col.classList.remove('drag-over');
        const sel = Array.from(window._groupState.selected);
        if (sel.length === 0) return;
        // Filter: don't move items already in this destination.
        const filtered = sel.filter(itemId => {
            const el = document.querySelector(
                '.threads-v5-group-item[data-item-id="' + itemId + '"]'
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
                '.threads-v5-group-item[data-item-id="' + iId + '"]'
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

    // ---- Selection handlers (item-level) ----------------------------

    window.threadsGroupItemClick = function (ev, itemId) {
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
        // Plain click — toggle selection (items aren't navigable
        // threads; clicking a card is a way to mark it).
        ev.preventDefault();
        if (window._groupState.selected.has(itemId)) {
            window._groupState.selected.delete(itemId);
        } else {
            window._groupState.selected.add(itemId);
            window._groupState.lastFocused = itemId;
        }
        _refreshSelectionClasses();
    };

    function _extendSelection(toItemId) {
        const all = Array.from(document.querySelectorAll(
            '.threads-v5-group-item'
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
        document.querySelectorAll('.threads-v5-group-item').forEach(el => {
            const iId = el.dataset.itemId;
            el.classList.toggle('selected', sel.has(iId));
        });
        const bar = document.querySelector(
            '.threads-v5-group-selection-bar'
        );
        if (bar) {
            const cnt = bar.querySelector('.count');
            if (cnt) cnt.textContent = sel.size + ' selected';
            bar.classList.toggle('show', sel.size > 0);
        }
    }

    // ---- Keyboard handler — extends script_threads_v5's j/k --------

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
            '.threads-v5-group-item.threads-v5-kbd-focus'
        ) || document.querySelector(
            '.threads-v5-group-item[data-item-id="'
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
            '.threads-v5-group-item'
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
        all.forEach(el => el.classList.remove('threads-v5-kbd-focus'));
        all[next].classList.add('threads-v5-kbd-focus');
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
                '.threads-v5-group-item[data-item-id="' + iId + '"]'
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
})();
"""


def _group_view_styles() -> str:
    return r"""
/* Stage 5 v2: group-view multi-column layout. Renders inside the
 * standard thread-detail card's "Sub-threads" section when the
 * active thread is a group umbrella (parent_relationship === 'group').
 * Columns are flex children that wrap on narrow viewports.
 */
.threads-v5-group-empty,
.threads-v5-group-loading {
    padding: 2em;
    text-align: center;
    color: var(--text-muted, #888);
}
.threads-v5-group-fetch-error {
    color: var(--text, #ddd);
}

/* Toolbar above the columns. Holds Approve-all + selection bar. */
.threads-v5-group-toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
    flex-wrap: wrap;
}
.threads-v5-group-approve-all {
    background: var(--accent, #4a7fc1);
    color: white;
    border: 1px solid var(--accent, #4a7fc1);
    border-radius: 4px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
}
.threads-v5-group-approve-all:hover {
    filter: brightness(1.1);
}

/* Slim selection bar — only visible while items are selected. */
.threads-v5-group-selection-bar {
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
.threads-v5-group-selection-bar.show {
    display: flex;
}
.threads-v5-group-selection-bar .count {
    font-weight: 600;
    color: var(--accent, #4a7fc1);
}
.threads-v5-group-selection-bar .hint {
    color: var(--text-muted, #888);
    font-size: 11px;
}
.threads-v5-group-selection-bar kbd {
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 0 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 10px;
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
    position: relative;
}
.threads-v5-group-column.drag-over {
    border-color: var(--accent, #4a7fc1);
    background: var(--bg-tertiary, #232323);
}

.threads-v5-group-column-header {
    margin-bottom: 8px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border, #333);
    position: relative;
}
.threads-v5-group-column-header-clickable {
    cursor: pointer;
    border-radius: 4px;
    margin: -4px -4px 8px -4px;
    padding: 4px 4px 6px 4px;
    transition: background-color 80ms;
}
.threads-v5-group-column-header-clickable:hover,
.threads-v5-group-column-header-clickable:focus-visible {
    background: var(--bg-tertiary, #232323);
    outline: none;
}

/* X / Delete group sub-thread button — top-right of the header,
 * visible on hover. */
.threads-v5-group-column-delete-x {
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
.threads-v5-group-column-header:hover
    .threads-v5-group-column-delete-x,
.threads-v5-group-column-delete-x:focus-visible {
    opacity: 1;
}
.threads-v5-group-column-delete-x:hover {
    background: rgba(220, 60, 60, 0.18);
    color: #ff8080;
}

.threads-v5-group-column-title-row {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    padding-right: 24px;  /* room for the X button */
}
.threads-v5-group-column-title {
    font-size: 14px;
    font-weight: 600;
}
.threads-v5-group-column-state {
    font-size: 10px;
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 1px 5px;
    color: var(--text-muted, #888);
    text-transform: capitalize;
    white-space: nowrap;
}
.threads-v5-group-column-intent {
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
.threads-v5-group-column-meta {
    font-size: 11px;
    color: var(--text-muted, #888);
    margin-top: 4px;
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

/* Item card — represents a ContextItem (Chrome tab, journal line,
 * etc.), NOT a thread. */
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
.threads-v5-group-item-url {
    font-size: 11px;
    color: var(--accent, #4a7fc1);
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: ui-monospace, SFMono-Regular, monospace;
}
.threads-v5-group-item-meta {
    font-size: 10px;
    color: var(--text-muted, #888);
    margin-top: 3px;
    text-transform: lowercase;
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
    cursor: pointer;
}
.threads-v5-group-newzone:hover,
.threads-v5-group-newzone.drag-over {
    border-color: var(--accent, #4a7fc1);
    color: var(--accent, #4a7fc1);
    background: rgba(74, 127, 193, 0.05);
}
.threads-v5-group-newzone-icon {
    font-size: 28px;
    line-height: 1;
}

/* Self-contained transient toast — does not register a workflow-view,
 * so it has no /api/workflow-views/.../dismiss round-trip on
 * teardown. */
#threads-v5-group-toast-host {
    position: fixed;
    bottom: 24px;
    right: 24px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 9999;
    pointer-events: none;
}
.threads-v5-group-toast {
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
.threads-v5-group-toast.show {
    opacity: 1;
    transform: translateY(0);
}
.threads-v5-group-toast-title {
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 2px;
}
.threads-v5-group-toast-body {
    font-size: 12px;
    color: var(--text-muted, #aaa);
}
"""
