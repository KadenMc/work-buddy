"""Group-view frontend — multi-column re-organisable layout slotted
into the standard thread detail UI's "Sub-threads" section.

The renderer here exposes ``window.renderGroupSubThreads(thread)``,
which the standard ``renderConfirmationCard``'s sub-threads section
calls when ``parent_relationship === 'group'``. This means group-
parents reuse the **whole** standard thread UI — breadcrumbs, intent,
namespace tags, thread actions, state badge, timeline button — and
the only swap is the section body: a flat list becomes a horizontally
laid-out grid of columns, one per sibling group-parent in the scrape.

Each column shows its sibling's title, intent (truncated), state
badge, and item count. Sibling column headers are clickable and use
``threadsPushPath`` — drilling into "B" pushes that path normally,
breadcrumbs and back-button work as for any sub-thread navigation.

Items in any column can:

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
- ``window._groupState.siblingsByParent: { threadId: [renderedSibling, ...] }``
  — the **same** siblings array is keyed under every sibling
  thread_id in the scrape. Swapping between siblings therefore
  doesn't re-fetch, and a single in-place mutation (optimistic
  move, SSE-driven refresh) updates every keyed view at once.
- ``window._groupStateBusWired`` — guard so the
  ``thread.state_changed`` SSE handler installs exactly once across
  morphdom re-renders. The handler debounces a real refresh (250
  ms) so a cascade of FSM events from one move coalesces into a
  single ``GET /group_siblings``.

Toasts are self-contained (``_groupToast``); they don't talk to the
``/api/workflow-views`` registry, so the tab they create dismisses
itself locally without 404 noise on the dismiss endpoint.
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

    // Self-contained toast — does NOT register a workflow-view, so
    // there's no /api/workflow-views/.../dismiss 404 when it goes
    // away. Three-second auto-dismiss; multiple stack vertically.
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

    // Cross-sibling cache primitives -------------------------------
    //
    // The /group_siblings response contains EVERY sibling in the
    // scrape; we share that single array under each sibling's
    // thread_id key. So swapping between siblings reads from cache,
    // and an in-place mutation (optimistic move, SSE refresh)
    // updates every keyed view simultaneously.
    function _setSiblings(siblings) {
        const cache = window._groupState.siblingsByParent;
        const errs = window._groupState.siblingErrors;
        for (const sib of siblings) {
            cache[sib.thread_id] = siblings;
            delete errs[sib.thread_id];
        }
    }

    function _replaceSiblingsInPlace(parentId, fresh) {
        // Mutate the existing array (if any) so cross-keyed
        // references stay valid. New sibling thread_ids get keyed
        // via _setSiblings.
        const cache = window._groupState.siblingsByParent;
        const existing = cache[parentId];
        if (existing && Array.isArray(existing)) {
            existing.length = 0;
            for (const sib of fresh) existing.push(sib);
            _setSiblings(existing);
        } else {
            _setSiblings(fresh);
        }
    }

    function _recountStates(items) {
        const counts = {};
        for (const it of items) {
            const s = it.fsm_state;
            if (!s) continue;
            counts[s] = (counts[s] || 0) + 1;
        }
        return counts;
    }

    // Apply a move locally so the user sees the item jump columns
    // immediately. The server-side response is best-effort merged
    // afterwards via _refreshSiblings (debounced through the SSE
    // handler) — if the server disagrees, the morphdom diff
    // reconciles silently.
    function _applyOptimisticMove(threadIds, destParentId) {
        const cache = window._groupState.siblingsByParent;
        const moved = new Set(threadIds);
        let siblings = null;
        for (const key in cache) {
            const arr = cache[key];
            if (!arr) continue;
            const has = arr.some(s =>
                (s.children_render || []).some(
                    c => moved.has(c.thread_id)
                )
            );
            if (has) { siblings = arr; break; }
        }
        if (!siblings) return false;
        const destSib = siblings.find(s => s.thread_id === destParentId);
        if (!destSib) return false;
        const moving = [];
        for (const sib of siblings) {
            const orig = sib.children_render || [];
            const kept = [];
            for (const item of orig) {
                if (moved.has(item.thread_id)) moving.push(item);
                else kept.push(item);
            }
            if (orig.length === kept.length) continue;
            sib.children_render = kept;
            sib.sub_thread_state_counts = _recountStates(kept);
        }
        destSib.children_render = (destSib.children_render || [])
            .concat(moving);
        destSib.sub_thread_state_counts =
            _recountStates(destSib.children_render);
        return true;
    }

    function _refreshSiblings(parentId) {
        return fetch('/api/threads/' + encodeURIComponent(parentId)
                     + '/group_siblings')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                _replaceSiblingsInPlace(parentId, data.siblings || []);
                if (typeof window._renderActiveThread === "function") {
                    window._renderActiveThread();
                }
            })
            .catch(err => {
                // Don't blow away the cached view on a transient
                // failure; just log.
                console.warn('[group-view] refresh failed:', err);
            });
    }

    // SSE wiring — debounced so a cascade of FSM events from a
    // single move coalesces into one /group_siblings call.
    let _refreshTimer = null;
    function _scheduleRefreshForActive() {
        if (_refreshTimer) clearTimeout(_refreshTimer);
        _refreshTimer = setTimeout(() => {
            _refreshTimer = null;
            const state = window._threadsState;
            if (!state || !state.path || state.path.length === 0) return;
            const activeId = state.path[state.path.length - 1];
            // Only refresh if the active view is currently a group.
            const cache = window._groupState.siblingsByParent;
            if (!cache[activeId]) return;
            _refreshSiblings(activeId);
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
                    const cache = window._groupState.siblingsByParent;
                    // Cheap relevance check: is this thread one of
                    // our cached siblings or one of their children?
                    let touched = false;
                    for (const key in cache) {
                        const arr = cache[key];
                        if (!arr) continue;
                        for (const sib of arr) {
                            if (sib.thread_id === tid
                                || (sib.children_render || []).some(
                                    c => c.thread_id === tid
                                )) {
                                touched = true;
                                break;
                            }
                        }
                        if (touched) break;
                    }
                    if (touched) _scheduleRefreshForActive();
                } catch (e) {
                    console.warn('[group-view] bus handler:', e);
                }
            });
        }
        _wire();
    }

    // Public API: invoked by script_threads_v5_card._renderSubThreadsLink
    // when the active thread has parent_relationship === 'group'.
    // Returns just the in-section markup (suggestions banner + columns
    // + new-group drop zone). The standard card supplies the section
    // wrapper, the "Sub-threads (N)" label, and aggregated state badges.
    window.renderGroupSubThreads = function (thread) {
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
        // Lazy-fetch siblings for this group. The response covers
        // EVERY sibling so we key it under each one — sibling-swap
        // is a cache hit afterwards.
        fetch('/api/threads/' + encodeURIComponent(parentId) + '/group_siblings')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                _setSiblings(data.siblings || []);
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
        return '<div class="threads-v5-group-loading">Loading group columns...</div>';
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
        const selCount = window._groupState.selected.size;
        // Slim selection bar — only visible while a selection exists.
        // Replaces the (deleted) outer "X groups in this scrape · N
        // selected" header line. Refreshed in-place by
        // _refreshSelectionClasses.
        let html = _renderSuggestionsPanel(activeId)
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
            + '<div class="threads-v5-group-columns">';
        for (const sib of siblings) {
            html += _renderColumn(sib, sib.thread_id === activeId);
        }
        // Drop-here-to-spawn-a-new-group zone. Visible only when
        // there's at least one sibling to use as a reference for
        // scope inheritance.
        if (siblings.length > 0) {
            html += _renderNewGroupZone(activeId);
        }
        html += '</div>';
        return html;
    }

    function _renderColumn(sib, isActive) {
        const items = sib.children_render || [];
        const sId = sib.thread_id;
        const stateCounts = sib.sub_thread_state_counts || {};
        const awaitingCount = stateCounts.awaiting_confirmation || 0;
        const stateLabel = sib.fsm_state || "";
        const intentText = (sib.intent && sib.intent.text) || "";
        const showIntent = intentText && intentText !== sib.title;
        const headerClickable = !isActive;
        let html = '<div class="threads-v5-group-column'
            + (isActive ? ' threads-v5-group-column-active' : '') + '" '
            + 'data-parent-id="' + _esc(sId) + '" '
            + 'ondragover="event.preventDefault();this.classList.add(\'drag-over\');" '
            + 'ondragleave="this.classList.remove(\'drag-over\');" '
            + 'ondrop="threadsGroupDropOnColumn(event, \'' + _esc(sId) + '\')">'
            + '<div class="threads-v5-group-column-header'
            +   (headerClickable
                    ? ' threads-v5-group-column-header-clickable'
                    : '') + '"'
            +   (headerClickable
                    ? ' role="link"'
                    +   ' tabindex="0"'
                    +   ' title="Open ' + _esc(sib.title || sId) + '"'
                    +   ' onclick="threadsPushPath(\''
                    +     _esc(sId) + '\')"'
                    +   ' onkeydown="if(event.key===\'Enter\'||event.key===\' \''
                    +     '){event.preventDefault();threadsPushPath(\''
                    +     _esc(sId) + '\')}"'
                    : '')
            + '>'
            +   '<div class="threads-v5-group-column-title-row">'
            +     '<span class="threads-v5-group-column-title">'
            +       _esc(sib.title || sId) + '</span>'
            +     (stateLabel
                    ? '<span class="threads-v5-group-column-state">'
                        + _esc(stateLabel) + '</span>'
                    : '')
            +     (isActive
                    ? '<span class="threads-v5-group-column-active-pill">'
                        + 'you are here</span>'
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
            +     items.length + ' item' + (items.length === 1 ? '' : 's')
            +     (awaitingCount > 0
                    ? ' &middot; ' + awaitingCount + ' awaiting'
                    : '')
            +   '</div>';
        if (awaitingCount > 0) {
            // event.stopPropagation prevents the header's navigation
            // onclick from firing when clicking the submit-all button.
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
                _groupToast(
                    'New group failed',
                    body.error || 'Could not spawn sibling group'
                );
                return;
            }
            const newParentId = body.parent_id;
            _moveBatch(sel, newParentId);
        })
        .catch(e => {
            _groupToast('New group failed', String(e));
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
        // Optimistic update — items jump columns immediately so the
        // user gets instant feedback. The server-driven refresh
        // (debounced SSE handler) reconciles whatever the server
        // actually did via morphdom.
        const optimistic = _applyOptimisticMove(threadIds, destParentId);
        window._groupState.selected.clear();
        if (optimistic && typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
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
            _groupToast(
                ok === total ? "Items moved" : "Move partial",
                msg
            );
            if (failed > 0) {
                // Optimistic update may have over-promised; force a
                // hard refresh from the server.
                const state = window._threadsState;
                if (state && state.path && state.path.length > 0) {
                    _refreshSiblings(state.path[state.path.length - 1]);
                }
            }
            // The SSE event-bus handler will _scheduleRefreshForActive
            // once cascade events arrive (auto-DISMISS of empty
            // groups, etc.); no explicit re-render needed here.
        });
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
        // Update the slim selection bar without a full re-render.
        const bar = document.querySelector(
            '.threads-v5-group-selection-bar'
        );
        if (bar) {
            const cnt = bar.querySelector('.count');
            if (cnt) cnt.textContent = sel.size + ' selected';
            bar.classList.toggle('show', sel.size > 0);
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
                _groupToast(
                    'Submit failed',
                    body.error || 'Group submit failed'
                );
                return;
            }
            const submitted = body.submitted || 0;
            const failed = body.failed || 0;
            const skipped = body.skipped || 0;
            const msg = 'Submitted ' + submitted
                + (failed ? ', ' + failed + ' failed' : '')
                + (skipped ? ', ' + skipped + ' skipped' : '');
            _groupToast('Group submitted', msg);
            // Server-side state changes will arrive over SSE and the
            // bus handler will refresh — no need to nuke caches here.
        })
        .catch(e => {
            _groupToast('Submit failed', String(e));
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
            _groupToast(
                'Nothing selected',
                'Press x to select items first.'
            );
            return;
        }
        // Pull sibling labels from the cache so the user picks by label.
        const state = window._threadsState;
        const activeId = state.path[state.path.length - 1];
        const sibs = (window._groupState.siblingsByParent[activeId] || []);
        const candidates = sibs.filter(s => s.thread_id !== activeId);
        if (candidates.length === 0) {
            _groupToast(
                'No other groups',
                'This scrape only has one group.'
            );
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
/* Stage 5: group-view multi-column layout. Renders inside the
 * standard thread-detail card's "Sub-threads" section when the
 * active thread has parent_relationship === 'group'. Columns are
 * flex children that wrap on narrow viewports.
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

/* Slim selection bar — only visible while items are selected. */
.threads-v5-group-selection-bar {
    display: none;
    align-items: center;
    gap: 12px;
    margin: 0 0 10px 0;
    padding: 6px 10px;
    background: var(--bg-tertiary, #232323);
    border: 1px solid var(--accent, #4a7fc1);
    border-radius: 6px;
    font-size: 12px;
    color: var(--text, #ddd);
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
/* Non-active columns: header is a clickable link to navigate into
 * that sibling group-parent (uses threadsPushPath). The drag handle
 * + items below stay independent. */
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

.threads-v5-group-column-title-row {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
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
.threads-v5-group-column-active-pill {
    font-size: 10px;
    background: var(--accent, #4a7fc1);
    color: white;
    border-radius: 3px;
    padding: 1px 6px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
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
