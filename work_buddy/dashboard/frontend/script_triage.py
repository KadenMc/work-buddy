"""Triage clarify and review view renderer JS."""

from __future__ import annotations


def _triage_clarify_script() -> str:
    return r"""
registerViewRenderer('triage_clarify', function(container, viewId, payload) {
    const pres = payload.presentation || {};
    const ACTIONS = ['close', 'create_task', 'record_into_task', 'group', 'leave'];
    const allGroups = [];
    for (const action of ACTIONS) {
        for (const g of (pres.groups_by_action || {})[action] || []) allGroups.push(g);
    }

    const withQuestions = allGroups.filter(g => g.clarifying_questions && g.clarifying_questions.length > 0);
    const answers = {};
    let answeredCount = 0;

    // Source-to-UI mapping. Modal is source-agnostic: new sources
    // add a row here and everything else (action columns, approval
    // flow) just works. Fallback is the generic clipboard icon.
    const sourceMap = {
        chrome:  { icon: '\u{1F310}', label: 'Chrome Tab' },
        journal: { icon: '\u{1F4D3}', label: 'Journal Thread' },
    };
    const srcInfo = sourceMap[pres.source] || { icon: '\u{1F4CB}', label: 'Item' };
    const sourceIcon = srcInfo.icon;
    const sourceLabel = srcInfo.label;

    const header = document.createElement('div');
    header.className = 'wv-header';
    header.innerHTML = `
        <h2><span class="wv-source-icon">${sourceIcon}</span> ${sourceLabel} Triage \u2014 Clarifying Questions</h2>
        ${pres.narrative ? '<div class="wv-narrative">' + pres.narrative + '</div>' : ''}
        <div class="wv-stats">
            <span class="wv-stat"><span class="wv-stat-num">${pres.total_groups || 0}</span> groups</span>
            <span class="wv-stat"><span class="wv-stat-num">${pres.total_items || 0}</span> items</span>
            <span class="wv-stat"><span class="wv-stat-num">${withQuestions.length}</span> need answers</span>
        </div>`;
    container.appendChild(header);

    const totalQuestions = withQuestions.reduce((n, g) => n + g.clarifying_questions.length, 0);
    const progress = document.createElement('div');
    progress.className = 'wv-progress';
    progress.innerHTML = `
        <div class="wv-progress-bar"><div class="wv-progress-fill" id="wv-prog-fill-${viewId}" style="width: 0%"></div></div>
        <span class="wv-progress-text" id="wv-prog-text-${viewId}">0 / ${totalQuestions} answered</span>`;
    container.appendChild(progress);

    function updateProgress() {
        answeredCount = 0;
        for (const qas of Object.values(answers)) {
            for (const [key, a] of Object.entries(qas)) {
                if (key === '_state') continue;
                if (typeof a === 'string') { if (a.trim()) answeredCount++; }
                else if (a && (a.text?.trim() || (a.confirmed_theories && a.confirmed_theories.length > 0))) answeredCount++;
            }
        }
        const pct = totalQuestions > 0 ? Math.round(answeredCount / totalQuestions * 100) : 0;
        const fill = document.getElementById('wv-prog-fill-' + viewId);
        const text = document.getElementById('wv-prog-text-' + viewId);
        if (fill) fill.style.width = pct + '%';
        if (text) text.textContent = answeredCount + ' / ' + totalQuestions + ' answered';
    }

    if (withQuestions.length === 0) {
        container.innerHTML += '<div class="empty-state">No clarifying questions needed.</div>';
    }

    for (const group of withQuestions) {
        answers[group.index] = {};
        const card = document.createElement('div');
        card.className = 'wv-question-card';

        const hdr = document.createElement('div');
        hdr.className = 'wv-group-header';
        hdr.innerHTML = '<span class="wv-group-intent">' + group.intent + '</span><span class="wv-badge ' + group.confidence + '">' + group.confidence + '</span>';
        card.appendChild(hdr);

        const chips = document.createElement('div');
        chips.className = 'wv-items-context';
        for (const item of group.items || []) {
            const chip = document.createElement('span');
            chip.className = 'wv-context-chip';
            if (item.url) { chip.innerHTML = '<a href="' + item.url + '" target="_blank">' + item.label + '</a>'; }
            else { chip.textContent = item.label; }
            chips.appendChild(chip);
        }
        card.appendChild(chips);

        for (const raw of group.clarifying_questions) {
            // Support both plain string and {question, theories} formats
            const qText = typeof raw === 'string' ? raw : (raw.question || '');
            const theories = (typeof raw === 'object' && Array.isArray(raw.theories)) ? raw.theories : [];

            const qDiv = document.createElement('div');
            qDiv.className = 'wv-question';

            // Row container: question+input on left, theories on right
            const row = document.createElement('div');
            row.style.cssText = 'display:flex; gap:16px; align-items:flex-start;';

            // Left: question label + text input
            const left = document.createElement('div');
            left.style.cssText = 'flex:1; min-width:0;';
            const label = document.createElement('label');
            label.textContent = qText;
            left.appendChild(label);
            const input = document.createElement('input');
            input.type = 'text';
            input.placeholder = 'Type your answer...';
            const idx = group.index;

            // Initialize answer state for this question
            if (!answers[idx]._state) answers[idx]._state = {};
            answers[idx]._state[qText] = {text: '', confirmed: []};

            const updateAnswer = () => {
                const st = answers[idx]._state[qText];
                // Build composite answer: text always present, confirmed theories listed
                const parts = [];
                if (st.confirmed.length > 0) parts.push('[Confirmed: ' + st.confirmed.join('; ') + ']');
                if (st.text.trim()) parts.push(st.text.trim());
                answers[idx][qText] = {text: st.text.trim(), confirmed_theories: [...st.confirmed]};
                updateProgress();
            };
            input.addEventListener('input', () => {
                answers[idx]._state[qText].text = input.value;
                updateAnswer();
            });
            left.appendChild(input);
            row.appendChild(left);

            // Right: theory checkboxes (if any)
            if (theories.length > 0) {
                const right = document.createElement('div');
                right.style.cssText = 'flex:0 0 auto; max-width:280px; display:flex; flex-direction:column; gap:4px; padding-top:22px;';
                for (const theory of theories) {
                    const lbl = document.createElement('label');
                    lbl.style.cssText = 'display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer; padding:3px 8px; border-radius:4px; background:var(--bg-secondary, #f5f5f5); border:1px solid var(--border-color, #ddd);';
                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.style.cssText = 'margin:0; cursor:pointer;';
                    cb.addEventListener('change', () => {
                        const st = answers[idx]._state[qText];
                        if (cb.checked) {
                            if (!st.confirmed.includes(theory)) st.confirmed.push(theory);
                        } else {
                            st.confirmed = st.confirmed.filter(t => t !== theory);
                        }
                        updateAnswer();
                    });
                    lbl.appendChild(cb);
                    const span = document.createElement('span');
                    span.textContent = theory;
                    lbl.appendChild(span);
                    right.appendChild(lbl);
                }
                row.appendChild(right);
            }

            qDiv.appendChild(row);
            card.appendChild(qDiv);
        }
        container.appendChild(card);
    }

    const footer = document.createElement('div');
    footer.className = 'wv-footer';
    const submitBtn = document.createElement('button');
    submitBtn.className = 'wv-submit';
    submitBtn.textContent = 'Submit Answers';
    submitBtn.addEventListener('click', async () => {
        const filtered = {};
        for (const [idx, qas] of Object.entries(answers)) {
            const nonEmpty = {};
            for (const [q, a] of Object.entries(qas)) {
                if (q === '_state') continue;
                if (typeof a === 'string') { if (a.trim()) nonEmpty[q] = a.trim(); }
                else if (a && (a.text?.trim() || (a.confirmed_theories && a.confirmed_theories.length > 0))) {
                    nonEmpty[q] = {text: a.text || '', confirmed_theories: a.confirmed_theories || []};
                }
            }
            if (Object.keys(nonEmpty).length > 0) filtered[idx] = nonEmpty;
        }
        submitBtn.textContent = 'Submitting...';
        submitBtn.disabled = true;
        try {
            await fetch('/api/workflow-views/' + viewId + '/respond', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phase: 'clarify', answers: filtered}),
            });
            container.innerHTML = '<div class="empty-state">\u2705 Answers submitted. Processing...</div>';
            setTimeout(() => removeWorkflowTab(viewId), 2000);
        } catch (e) { submitBtn.textContent = 'Error \u2014 try again'; submitBtn.disabled = false; }
    });
    footer.appendChild(submitBtn);
    container.appendChild(footer);
});
"""


# ---------------------------------------------------------------------------
# Triage Review view renderer (state-driven, drag-and-drop)
# ---------------------------------------------------------------------------


def _triage_review_script() -> str:
    return r"""
// Mount-anywhere renderer for the triage-review UI. Originally only
// used inside the workflow-view modal (via `registerViewRenderer` on
// the dispatch_review path); now also mounted inline by the Review
// tab so the same action-column layout, override flow, and submit
// pipeline serve both use-cases. Keeps drag-and-drop reassignment,
// override reasons, new-group creation — everything the Chrome
// modal already does.
//
// Options:
//   onSubmit(group_decisions, reassignments): called on submit. The
//       caller is responsible for the actual HTTP request (modal
//       path posts to /api/workflow-views/<viewId>/respond; Review
//       tab posts to /api/review/execute with presentation attached).
//       Must return a Promise.
//   onComplete(): optional; called after a successful submit. Modal
//       path uses it to remove the workflow tab; Review tab uses it
//       to refresh the pool.
// Card-action helpers shared by the inline button (renderItem below)
// and the Review-drawer (script_review.py). Module-scoped so they're
// not redefined per render; defined as window properties so the drawer
// can call them across script files.
//
// _wvExecuteAction(action) -> Promise<{success, error?, error_kind?, ...}>
//   POSTs to /api/palette/execute and returns the parsed body, with a
//   conservative default shape on network errors. Never raises.
//
// _wvExtractErrorKind(data) -> string | null
//   Reads the response's nested capability-result error_kind. The
//   /api/palette/execute envelope shape is:
//      {success: true, result: <stringified-json>, provider: "work-buddy"}
//   for short results, or a palette_result view for long ones. The
//   underlying capability's error_kind lives inside the parsed result.
//
// _wvQuarantineEntry(group, item, errorKind, btn, row) -> Promise<void>
//   Self-heal helper: when an action click learns the source is gone,
//   POST a triage_pool_quarantine_entry call so the stale card vanishes.
//   Visually fades out the row; the next pool fetch confirms removal.
if (!window._wvExecuteAction) {
    window._wvExecuteAction = async function(action) {
        try {
            const resp = await fetch('/api/palette/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    command_id: action.command_id,
                    params: action.params || {},
                }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok && data.success !== false) {
                return { success: false, error: 'HTTP ' + resp.status, http_status: resp.status };
            }
            return data || { success: false, error: 'Empty response' };
        } catch (err) {
            return { success: false, error: String(err) };
        }
    };
}
if (!window._wvExtractErrorKind) {
    window._wvExtractErrorKind = function(envelope) {
        // Envelope is the /api/palette/execute response. The wrapped
        // capability's verdict lives in `result` as a JSON string when
        // small, or as a payload.result for palette_result views.
        if (!envelope) return null;
        // Newer-style envelope already exposes error_kind directly.
        if (typeof envelope.error_kind === 'string' && envelope.error_kind) return envelope.error_kind;
        const r = envelope.result;
        if (typeof r === 'string') {
            try {
                const parsed = JSON.parse(r);
                if (parsed && typeof parsed.error_kind === 'string') return parsed.error_kind;
            } catch (_) { /* not JSON — opaque string result */ }
        } else if (r && typeof r === 'object' && typeof r.error_kind === 'string') {
            return r.error_kind;
        }
        return null;
    };
}
if (!window._wvQuarantineEntry) {
    window._wvQuarantineEntry = async function(group, item, errorKind, btn, row) {
        const runId = group && group.pool_run_id;
        const itemId = item && item.id;
        if (!runId || !itemId) {
            // Can't self-heal without both ids. Surface the original
            // error verbatim instead.
            if (btn) {
                btn.title = 'Source gone (' + errorKind + ') — could not auto-quarantine: missing run_id or item_id';
                btn.classList.add('wv-item-action-btn-failed');
            }
            return;
        }
        try {
            const resp = await fetch('/api/palette/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    command_id: 'work-buddy::triage_pool_quarantine_entry',
                    params: { run_id: runId, item_id: itemId, reason: 'source_removed' },
                }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || data.success === false) {
                if (btn) {
                    btn.title = 'Source gone — quarantine failed: ' + (data.error || 'unknown');
                    btn.classList.add('wv-item-action-btn-failed');
                }
                return;
            }
            // Visually fade the row so the user sees the self-healing.
            // The next /api/triage poll will drop it from the rendered
            // list; this is just immediate feedback.
            if (row) {
                row.classList.add('wv-item-quarantined');
                row.title = 'Source no longer found (' + errorKind + ') — quarantined';
            }
            if (btn) {
                btn.textContent = 'Source gone — quarantined';
                btn.classList.add('wv-item-action-btn-quarantined');
            }
        } catch (err) {
            if (btn) {
                btn.title = 'Source gone — quarantine threw: ' + String(err);
                btn.classList.add('wv-item-action-btn-failed');
            }
        }
    };
}

function renderTriageReview(container, presentation, options) {
    const pres = presentation || {};
    options = options || {};
    const onSubmit = options.onSubmit || (async () => {});
    const onComplete = options.onComplete || (() => {});
    // Slice 1.5 hook: callers (notably the Resolution Surface) supply
    // a per-card decoration callback that runs after a card is rendered
    // and after any targeted re-render. Default is a no-op so the
    // Chrome triage modal and tests keep their existing behaviour.
    const decorateCard = options.decorateCard || (() => {});
    const ACTIONS = ['close', 'create_task', 'record_into_task', 'group', 'leave'];
    const ACTION_LABELS = {
        close: 'Close', create_task: 'Create Task', record_into_task: 'Record Into Task',
        group: 'Group Together', leave: 'Leave As-Is',
    };
    const ACTION_ICONS = { close: '\u2716', create_task: '+', record_into_task: '\ud83d\udccb', group: '\ud83d\udd17', leave: '\u2714' };
    const ITEM_ACTIONS = ACTIONS.filter(a => a !== 'group');

    const state = {
        groups: [], decisions: {}, itemOverrides: {},
        reassignments: [], newGroups: [], nextTempIndex: -1,
        taskAssignments: {},  // {groupIndex: taskId} for record_into_task
        newTaskTexts: {},     // {groupIndex: text} for create_task
        overrideReasons: {},  // {groupIndex: reason} for non-obvious action changes
        namespaceTags: {},    // {groupIndex: [tag, tag, ...]} applied to new/recorded tasks
    };

    // Pre-fetched namespace universe for autocomplete. Populated once per
    // Review-tab render; stays empty for modal callers that never use it.
    let _nsUniverse = null;
    function ensureNamespaceUniverse() {
        if (_nsUniverse !== null) return Promise.resolve(_nsUniverse);
        return fetch('/api/namespaces').then(r => r.json()).then(d => {
            _nsUniverse = (d && Array.isArray(d.namespaces)) ? d.namespaces : [];
            return _nsUniverse;
        }).catch(() => { _nsUniverse = []; return _nsUniverse; });
    }

    for (const action of ACTIONS) {
        for (const gi of (pres.groups_by_action || {})[action] || []) {
            const g = typeof gi === 'number' ? pres.groups[gi] : gi;
            if (!g) continue;
            state.groups.push({...g, _items: [...(g.items || [])]});
            state.decisions[g.index !== undefined ? g.index : state.groups.length - 1] = g.suggested_action;
            // Pre-populate task assignments from pipeline
            if (g.likely_task_id) state.taskAssignments[g.index] = g.likely_task_id;
            if (g.suggested_task_text) state.newTaskTexts[g.index] = g.suggested_task_text;
            if (Array.isArray(g.suggested_namespace_tags)) {
                state.namespaceTags[g.index] = [...g.suggested_namespace_tags];
            }
        }
    }

    let dragItem = null, dragSourceGroup = null;

    function render() {
        const scrollY = container.parentElement ? container.parentElement.scrollTop : 0;
        container.innerHTML = '';
        const sourceIcon = pres.source === 'chrome' ? '\ud83c\udf10' : '\ud83d\udccb';
        const sourceLabel = pres.source === 'chrome' ? 'Chrome Tab' : 'Item';
        const header = document.createElement('div');
        header.className = 'wv-header';
        let totalItems = state.groups.reduce((n, g) => n + (g._items || g.items).length, 0)
            + state.newGroups.reduce((n, ng) => n + ng.items.length, 0);
        let statsHtml = '<span class="wv-stat"><span class="wv-stat-num">' + (state.groups.length + state.newGroups.length) + '</span> groups</span>'
            + '<span class="wv-stat"><span class="wv-stat-num">' + totalItems + '</span> items</span>';
        // revisions count removed — internal pipeline detail, not useful to user
        header.innerHTML = '<h2><span class="wv-source-icon">' + sourceIcon + '</span> ' + sourceLabel + ' Triage \u2014 Review Actions</h2>'
            + (pres.narrative ? '<div class="wv-narrative">' + pres.narrative + '</div>' : '')
            + '<div class="wv-stats">' + statsHtml + '</div>';
        container.appendChild(header);

        // Flat seriated order (semantically similar groups adjacent)
        const displayOrder = pres.display_order || state.groups.map(g => g.index);
        const groupByIdx = {};
        for (const g of state.groups) groupByIdx[g.index] = g;

        for (const idx of displayOrder) {
            const g = groupByIdx[idx];
            if (g) renderGroupCard(container, g);
        }
        // Any groups not in display_order (shouldn't happen, but safety)
        for (const g of state.groups) {
            if (!displayOrder.includes(g.index)) renderGroupCard(container, g);
        }

        if (state.newGroups.length > 0) {
            const section = document.createElement('div');
            section.className = 'wv-section';
            const hdr = document.createElement('div');
            hdr.className = 'wv-section-header';
            hdr.innerHTML = '<h3><span class="wv-section-icon">\u2728</span> New Groups <span class="wv-section-count">' + state.newGroups.length + '</span></h3>';
            section.appendChild(hdr);
            const body = document.createElement('div');
            for (const ng of state.newGroups) renderNewGroupCard(body, ng);
            section.appendChild(body);
            container.appendChild(section);
        }

        const dropZone = document.createElement('div');
        dropZone.className = 'wv-new-group-zone';
        dropZone.innerHTML = '<div class="wv-drop-icon">+</div>Drop item here to create a new group';
        dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drag-active'); });
        dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-active'));
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault(); dropZone.classList.remove('drag-active');
            if (!dragItem || !dragSourceGroup) return;
            showNewGroupPrompt(container, dragItem, dragSourceGroup);
            dragItem = null; dragSourceGroup = null;
        });
        container.appendChild(dropZone);

        const footer = document.createElement('div');
        footer.className = 'wv-footer';
        const submitBtn = document.createElement('button');
        submitBtn.className = 'wv-submit';
        submitBtn.textContent = 'Submit All';
        submitBtn.addEventListener('click', () => submitDecisions(submitBtn));
        footer.appendChild(submitBtn);
        container.appendChild(footer);

        // Restore scroll position after re-render
        if (scrollY && container.parentElement) {
            requestAnimationFrame(() => { container.parentElement.scrollTop = scrollY; });
        }
    }

    function renderGroupCard(parent, group) {
        const card = document.createElement('div');
        card.className = 'wv-group-card';
        card.dataset.groupIndex = String(group.index);
        // Per-card SSE addressing. The card identifies its underlying
        // pool entry via (pool_run_id, item_id-of-first-item). Multi-
        // item clusters (Slice 3) still resolve to the cluster's
        // primary item; SSE state-change events fire per entry, so
        // the dominant single-item case maps cleanly. See
        // architecture/event-bus.
        if (group.pool_run_id) card.dataset.poolRunId = group.pool_run_id;
        const _firstItem = (group._items || group.items || [])[0];
        if (_firstItem && _firstItem.id) card.dataset.itemId = _firstItem.id;
        card.addEventListener('dragover', (e) => { e.preventDefault(); card.classList.add('drag-over'); });
        card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
        card.addEventListener('drop', (e) => {
            e.preventDefault(); card.classList.remove('drag-over');
            if (!dragItem || !dragSourceGroup || dragSourceGroup.index === group.index) return;
            moveItemToGroup(dragItem, dragSourceGroup, group.index);
            dragItem = null; dragSourceGroup = null;
        });

        const hdr = document.createElement('div');
        hdr.className = 'wv-group-header';
        const hdrLeft = document.createElement('div');
        hdrLeft.className = 'wv-group-header-left';
        hdrLeft.innerHTML = '<div class="wv-group-intent">' + group.intent + '</div>';
        if (group.context) hdrLeft.innerHTML += '<div class="wv-context-subtitle">' + group.context + '</div>';
        const hdrRight = document.createElement('div');
        hdrRight.className = 'wv-group-header-right';
        // Gap 3: Confidence tooltip
        const confTips = {high: 'High confidence \u2014 strong signal from tab content + task matches', medium: 'Medium confidence \u2014 reasonable match but some ambiguity', low: 'Low confidence \u2014 weak signal, may need manual review'};
        hdrRight.innerHTML = '<span class="wv-badge ' + group.confidence + '" title="' + (confTips[group.confidence] || '') + '">' + group.confidence + '</span>';
        hdr.appendChild(hdrLeft);
        hdr.appendChild(hdrRight);
        card.appendChild(hdr);

        // Body: CSS Grid — left col (rationale + pills + items), right col (task area)
        const cur = state.decisions[group.index] || group.suggested_action;
        const hasTaskArea = (cur === 'create_task' || cur === 'record_into_task');

        const body = document.createElement('div');
        body.className = 'wv-card-body' + (hasTaskArea ? ' has-task-area' : '');

        // Left column
        const mainCol = document.createElement('div');
        mainCol.className = 'wv-card-main';

        if (group.rationale) { const r = document.createElement('div'); r.className = 'wv-rationale'; r.textContent = group.rationale; mainCol.appendChild(r); }

        const pills = document.createElement('div');
        pills.className = 'wv-action-pills';
        for (const a of ACTIONS) {
            const pill = document.createElement('button');
            pill.className = 'wv-pill' + (a === cur ? ' selected' : '');
            pill.dataset.action = a;
            pill.textContent = ACTION_LABELS[a];
            pill.addEventListener('click', () => {
                state.decisions[group.index] = a;
                pills.querySelectorAll('.wv-pill').forEach(p =>
                    p.classList.toggle('selected', p.dataset.action === a)
                );
                // Toggle grid layout + task area
                const needsTask = (a === 'create_task' || a === 'record_into_task');
                body.classList.toggle('has-task-area', needsTask);
                updateTaskArea(taskCol, group, a);
                // Gap 7: Smart override reason — only for non-obvious changes
                const isOverride = (a !== group.suggested_action);
                const needsReason = isOverride && a !== 'create_task'; // create_task has its own text
                updateOverrideReason(mainCol, group.index, needsReason);
            });
            pills.appendChild(pill);
        }
        mainCol.appendChild(pills);

        // Namespace tag chips — user can set tags that apply to the task
        // produced/updated by this group (create_task or record_into_task).
        // Only the Review tab opts in via options.showNamespaceTags; other
        // callers (modal) ignore this surface.
        if (options.showNamespaceTags) {
            renderNamespaceTagsArea(mainCol, group);
        }

        // Items in left column
        const items = group._items || group.items || [];
        const itemsArea = document.createElement('div');
        itemsArea.className = 'wv-items-area';
        if (items.length === 0) {
            // Gap 11: Empty group — show dismiss button
            const empty = document.createElement('div');
            empty.className = 'wv-empty-group';
            empty.innerHTML = '<span>No items</span>';
            const dismissBtn = document.createElement('button');
            dismissBtn.className = 'wv-dismiss-btn';
            dismissBtn.textContent = '\u2715 Remove group';
            dismissBtn.addEventListener('click', () => card.remove());
            empty.appendChild(dismissBtn);
            itemsArea.appendChild(empty);
        } else {
            for (const item of items) renderItem(itemsArea, item, group);
        }
        mainCol.appendChild(itemsArea);

        body.appendChild(mainCol);

        // Right column (task area) — always in DOM, grid hides it when not needed
        const taskCol = document.createElement('div');
        taskCol.className = 'wv-card-task-col';
        if (hasTaskArea) {
            updateTaskArea(taskCol, group, cur);
        }
        body.appendChild(taskCol);

        card.appendChild(body);

        // Per-group submit button — only when the caller opts in.
        // The Review tab enables this so users can act on one group
        // without committing the entire batch; the Chrome triage
        // modal keeps bulk-only (opts.perGroupSubmit=false|unset).
        // The bottom Submit All button is always there as a
        // batch fallback.
        if (options.perGroupSubmit) {
            const footer = document.createElement('div');
            footer.className = 'wv-group-footer';
            const btn = document.createElement('button');
            btn.className = 'wv-group-submit';
            btn.type = 'button';
            btn.textContent = 'Submit';
            btn.addEventListener('click', async () => {
                btn.disabled = true;
                btn.textContent = 'Submitting\u2026';
                try {
                    await submitSingleGroup(group, btn);
                } catch (e) {
                    btn.disabled = false;
                    btn.textContent = 'Error \u2014 try again';
                    console.error('per-group submit failed', e);
                }
            });
            footer.appendChild(btn);
            card.appendChild(footer);
        }

        parent.appendChild(card);

        // Slice 1.5 decoration hook — invoked after the card lands in
        // its parent so callers can add per-card affordances (defer
        // button, redirect button, blocker badge) without forking the
        // renderer. Re-runs on every targeted re-render via rerenderCard.
        try { decorateCard(card, group); } catch (e) {
            console.error('[triage] decorateCard threw:', e);
        }
    }

    // Collect the single-group decision payload + send it via the
    // shared onSubmit callback. Mirrors submitDecisions but for one
    // group only. On success removes the card, marks the group as
    // locally-done in `state`, and updates the header stats so the
    // remaining batch submit won't re-send this entry.
    async function submitSingleGroup(group, btnRef) {
        const ov = [];
        for (const item of (group._items || group.items)) {
            if (state.itemOverrides[item.id]) {
                ov.push({item_id: item.id, action: state.itemOverrides[item.id]});
            }
        }
        const entry = {
            group_index: group.index,
            action: state.decisions[group.index] || group.suggested_action,
            item_overrides: ov,
        };
        if (state.taskAssignments[group.index]) entry.target_task_id = state.taskAssignments[group.index];
        if (state.newTaskTexts[group.index]) entry.new_task_text = state.newTaskTexts[group.index];
        if (state.overrideReasons[group.index]) entry.override_reason = state.overrideReasons[group.index];
        if ((state.namespaceTags[group.index] || []).length > 0) {
            entry.namespace_tags = [...state.namespaceTags[group.index]];
        }

        await onSubmit([entry], []);

        // Drop this group from local state so the bottom batch
        // submit doesn't re-send it.
        state.groups = state.groups.filter(g => g.index !== group.index);
        delete state.decisions[group.index];
        delete state.taskAssignments[group.index];
        delete state.newTaskTexts[group.index];
        delete state.overrideReasons[group.index];
        delete state.namespaceTags[group.index];

        // Visually retire the card in place so the user sees the
        // ack without the whole list flickering.
        const card = btnRef.closest('.wv-group-card');
        if (card) {
            card.style.transition = 'opacity 0.25s';
            card.style.opacity = '0.4';
            btnRef.textContent = 'Submitted \u2713';
        }
    }

    function renderItem(parent, item, group) {
        const row = document.createElement('div');
        row.className = 'wv-item';
        row.draggable = true;
        row.addEventListener('dragstart', (e) => {
            dragItem = item; dragSourceGroup = group;
            row.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });
        row.addEventListener('dragend', () => {
            row.classList.remove('dragging');
            document.querySelectorAll('.drag-over, .drag-active').forEach(el => el.classList.remove('drag-over', 'drag-active'));
        });

        const handle = document.createElement('span');
        handle.className = 'wv-drag-handle';
        handle.textContent = '\u2261';
        row.appendChild(handle);

        // Gap 12: Full URL shown on hover
        const labelEl = document.createElement('div');
        labelEl.className = 'wv-item-label';
        if (item.url) { labelEl.innerHTML = '<a href="' + item.url + '" target="_blank" title="' + item.url + '">' + item.label + '</a>'; }
        else { labelEl.textContent = item.label; }
        row.appendChild(labelEl);

        // Per-source "open in app" actions (declared in SourceDescriptor
        // config; resolved by work_buddy.clarify.card_actions). Buttons
        // sit next to the label; click POSTs to /api/palette/execute
        // (the same endpoint the command palette uses). When no actions
        // are emitted (chrome / journal / inline today), this block is a
        // no-op and the existing href-link behavior is preserved.
        if (Array.isArray(item.actions) && item.actions.length > 0) {
            for (const act of item.actions) {
                const btn = document.createElement('button');
                btn.className = 'wv-item-action-btn';
                btn.type = 'button';
                btn.textContent = act.label;
                btn.title = act.label + ' (via ' + (act.command_id || '') + ')';
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    btn.disabled = true;
                    try {
                        const data = await window._wvExecuteAction(act);
                        if (data && data.success === false) {
                            const msg = data.error || 'Action failed';
                            const errorKind = window._wvExtractErrorKind(data);
                            const ek = (act.quarantine_on_error_kinds || []);
                            if (errorKind && ek.indexOf(errorKind) >= 0) {
                                // Self-heal: source is gone. Mark this entry
                                // quarantined so the stale card vanishes on
                                // next pool read.
                                await window._wvQuarantineEntry(group, item, errorKind, btn, row);
                            } else {
                                console.error('[triage] action failed:', msg);
                                btn.title = msg;
                                btn.classList.add('wv-item-action-btn-failed');
                            }
                        }
                    } catch (err) {
                        console.error('[triage] action threw:', err);
                        btn.title = String(err);
                        btn.classList.add('wv-item-action-btn-failed');
                    } finally {
                        btn.disabled = false;
                    }
                });
                row.appendChild(btn);
            }
        }

        // Item-detail eye icon. Only rendered when the caller
        // supplied an onItemClick handler (Review tab wires this to
        // the right-side drawer). Modal callers (workflow-view
        // dispatch) pass nothing — existing behaviour preserved.
        if (options.onItemClick) {
            const eye = document.createElement('button');
            eye.className = 'wv-item-detail-btn';
            eye.type = 'button';
            eye.title = 'Show full text / rationale / context';
            eye.textContent = '\u{1F441}';  // 👁
            eye.addEventListener('click', (e) => {
                e.stopPropagation();
                options.onItemClick(item, group);
            });
            row.appendChild(eye);
        }

        const overrideSelect = document.createElement('select');
        overrideSelect.className = 'wv-item-override';
        overrideSelect.title = 'Override action for this item';
        const dOpt = document.createElement('option'); dOpt.value = ''; dOpt.textContent = '\u2014'; overrideSelect.appendChild(dOpt);
        for (const a of ITEM_ACTIONS) {
            const opt = document.createElement('option'); opt.value = a; opt.textContent = ACTION_LABELS[a];
            if (state.itemOverrides[item.id] === a) opt.selected = true;
            overrideSelect.appendChild(opt);
        }
        overrideSelect.addEventListener('change', () => {
            if (overrideSelect.value) state.itemOverrides[item.id] = overrideSelect.value;
            else delete state.itemOverrides[item.id];
        });
        row.appendChild(overrideSelect);
        parent.appendChild(row);
    }

    // ---- Override reason (Gap 7) ----

    function updateOverrideReason(mainCol, groupIndex, show) {
        let existing = mainCol.querySelector('.wv-override-reason');
        if (!show) { if (existing) existing.remove(); delete state.overrideReasons[groupIndex]; return; }
        if (existing) return; // already showing
        const wrap = document.createElement('div');
        wrap.className = 'wv-override-reason';
        const input = document.createElement('input');
        input.type = 'text';
        input.placeholder = 'Reason for change (optional)...';
        input.value = state.overrideReasons[groupIndex] || '';
        input.addEventListener('input', () => { state.overrideReasons[groupIndex] = input.value; });
        wrap.appendChild(input);
        // Insert after pills
        const pills = mainCol.querySelector('.wv-action-pills');
        if (pills && pills.nextSibling) mainCol.insertBefore(wrap, pills.nextSibling);
        else mainCol.appendChild(wrap);
    }

    // ---- Namespace tag chips (applies to new/recorded tasks) ----

    function _sanitizeTag(raw) {
        const cleaned = String(raw || '').trim().replace(/^#+/, '').trim();
        if (!cleaned) return '';
        // Mirror mutations.NAMESPACE_TAG_RE: lowercase letters/digits, '-_/'.
        if (!/^[a-z0-9][a-z0-9_/-]*$/i.test(cleaned)) return '';
        return cleaned;
    }

    function renderNamespaceTagsArea(mainCol, group) {
        // Only meaningful for actions that produce/touch a task. For the
        // others we still render the container but keep it hidden via the
        // body class, so toggling action pills doesn't rebuild layout.
        const wrap = document.createElement('div');
        wrap.className = 'wv-namespace-tags';

        const label = document.createElement('div');
        label.className = 'wv-namespace-tags-label';
        label.textContent = 'Namespace tags';
        wrap.appendChild(label);

        const chipRow = document.createElement('div');
        chipRow.className = 'wv-namespace-chip-row';
        wrap.appendChild(chipRow);

        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'wv-namespace-input';
        input.placeholder = 'paper/ecg-classifier, admin/taxes, \u2026';
        input.autocomplete = 'off';
        wrap.appendChild(input);

        const suggestions = document.createElement('div');
        suggestions.className = 'wv-namespace-suggestions';
        suggestions.style.display = 'none';
        wrap.appendChild(suggestions);

        function redrawChips() {
            chipRow.innerHTML = '';
            const tags = state.namespaceTags[group.index] || [];
            if (tags.length === 0) {
                const empty = document.createElement('span');
                empty.className = 'wv-namespace-empty';
                empty.textContent = '(none)';
                chipRow.appendChild(empty);
                return;
            }
            for (const t of tags) {
                const chip = document.createElement('span');
                chip.className = 'wv-namespace-chip';
                chip.textContent = '#' + t;
                const x = document.createElement('span');
                x.className = 'wv-namespace-chip-x';
                x.textContent = '\u2715';
                x.title = 'Remove';
                x.addEventListener('click', () => {
                    state.namespaceTags[group.index] =
                        (state.namespaceTags[group.index] || []).filter(v => v !== t);
                    redrawChips();
                });
                chip.appendChild(x);
                chipRow.appendChild(chip);
            }
        }

        function addTag(raw) {
            const tag = _sanitizeTag(raw);
            if (!tag) return false;
            const current = state.namespaceTags[group.index] || [];
            if (current.includes(tag)) return false;
            state.namespaceTags[group.index] = [...current, tag];
            redrawChips();
            return true;
        }

        function hideSuggestions() { suggestions.style.display = 'none'; suggestions.innerHTML = ''; }

        function showSuggestions(prefix) {
            if (!_nsUniverse || _nsUniverse.length === 0) { hideSuggestions(); return; }
            const pl = (prefix || '').toLowerCase();
            const current = new Set(state.namespaceTags[group.index] || []);
            const matches = _nsUniverse
                .filter(row => !current.has(row.tag))
                .filter(row => !pl || row.tag.toLowerCase().includes(pl))
                .slice(0, 8);
            if (matches.length === 0) { hideSuggestions(); return; }
            suggestions.innerHTML = '';
            for (const row of matches) {
                const opt = document.createElement('div');
                opt.className = 'wv-namespace-suggestion';
                opt.innerHTML = '<span>#' + row.tag + '</span>'
                    + '<span class="wv-namespace-count">' + row.count + '</span>';
                opt.addEventListener('mousedown', (e) => {
                    e.preventDefault();  // keep focus on input
                    if (addTag(row.tag)) { input.value = ''; hideSuggestions(); }
                });
                suggestions.appendChild(opt);
            }
            suggestions.style.display = 'block';
        }

        input.addEventListener('input', () => { showSuggestions(input.value); });
        input.addEventListener('focus', () => {
            ensureNamespaceUniverse().then(() => showSuggestions(input.value));
        });
        input.addEventListener('blur', () => setTimeout(hideSuggestions, 150));
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ',') {
                e.preventDefault();
                if (addTag(input.value)) { input.value = ''; hideSuggestions(); }
            } else if (e.key === 'Backspace' && input.value === '') {
                const current = state.namespaceTags[group.index] || [];
                if (current.length > 0) {
                    state.namespaceTags[group.index] = current.slice(0, -1);
                    redrawChips();
                }
            }
        });

        redrawChips();
        // Prime the universe in the background so the first focus is snappy.
        ensureNamespaceUniverse();

        // Insert after the action pills (and after any override-reason input
        // that may appear there).
        const pills = mainCol.querySelector('.wv-action-pills');
        const reason = mainCol.querySelector('.wv-override-reason');
        const anchor = reason || pills;
        if (anchor && anchor.nextSibling) mainCol.insertBefore(wrap, anchor.nextSibling);
        else mainCol.appendChild(wrap);
    }

    // ---- Task search / assignment UI ----

    let _searchDebounce = null;
    const _taskCache = {};

    function updateTaskArea(container, group, action) {
        container.innerHTML = '';
        if (action === 'record_into_task') {
            renderRecordTaskArea(container, group);
        } else if (action === 'create_task') {
            renderCreateTaskArea(container, group);
        }
    }

    function renderRecordTaskArea(container, group) {
        const area = document.createElement('div');
        area.className = 'wv-task-area';
        area.innerHTML = '<div class="wv-task-area-label">Record into task</div>';
        const selectedId = state.taskAssignments[group.index];
        if (selectedId) {
            const chip = document.createElement('div');
            chip.className = 'wv-task-selected';
            const cached = _taskCache[selectedId];
            const isTaskId = selectedId.startsWith('t-');
            const idLabel = isTaskId ? selectedId : '';
            const textLabel = cached ? cached.text : (isTaskId ? '(loading...)' : selectedId);
            chip.innerHTML = (idLabel ? '<span class="wv-task-match-id">' + idLabel + '</span> ' : '')
                + '<span class="wv-task-match-text">' + textLabel + '</span>'
                + ' <span class="wv-task-clear" title="Clear">\u2715</span>';
            chip.querySelector('.wv-task-clear').addEventListener('click', () => {
                delete state.taskAssignments[group.index];
                updateTaskArea(container, group, 'record_into_task');
            });
            area.appendChild(chip);
            if (isTaskId && !cached) fetchTaskById(selectedId).then(() => {
                const c2 = _taskCache[selectedId];
                if (c2) { const t = chip.querySelector('.wv-task-match-text'); if (t) t.textContent = c2.text; }
            });
        }
        renderTaskSearchInput(area, (task) => {
            state.taskAssignments[group.index] = task.id || task.text;
            _taskCache[task.id || task.text] = task;
            updateTaskArea(container, group, 'record_into_task');
        });
        container.appendChild(area);
    }

    function renderCreateTaskArea(container, group) {
        const area = document.createElement('div');
        area.className = 'wv-task-area';
        area.innerHTML = '<div class="wv-task-area-label">New task</div>';
        const input = document.createElement('input');
        input.className = 'wv-new-task-input';
        input.type = 'text';
        input.placeholder = 'New task name...';
        input.value = state.newTaskTexts[group.index] || '';
        input.addEventListener('input', () => { state.newTaskTexts[group.index] = input.value; });
        area.appendChild(input);
        container.appendChild(area);
    }

    function renderTaskSearchInput(parent, onSelect) {
        const wrap = document.createElement('div');
        wrap.className = 'wv-task-search-wrap';
        const input = document.createElement('input');
        input.className = 'wv-task-search';
        input.type = 'text';
        input.placeholder = 'Search tasks...';
        const dropdown = document.createElement('div');
        dropdown.className = 'wv-task-dropdown';
        dropdown.style.display = 'none';
        input.addEventListener('input', () => {
            const q = input.value.trim();
            if (q.length < 2) { dropdown.style.display = 'none'; return; }
            clearTimeout(_searchDebounce);
            _searchDebounce = setTimeout(() => doTaskSearch(q, dropdown, onSelect), 250);
        });
        input.addEventListener('blur', () => setTimeout(() => { dropdown.style.display = 'none'; }, 200));
        input.addEventListener('focus', () => { if (dropdown.children.length > 0) dropdown.style.display = 'block'; });
        wrap.appendChild(input);
        wrap.appendChild(dropdown);
        parent.appendChild(wrap);
    }

    async function doTaskSearch(query, dropdown, onSelect) {
        try {
            const r = await fetch('/api/tasks/search?q=' + encodeURIComponent(query) + '&limit=8&method=hybrid');
            const data = await r.json();
            dropdown.innerHTML = '';
            if (!data.tasks || data.tasks.length === 0) {
                dropdown.innerHTML = '<div class="wv-task-match" style="color:var(--text-muted)">No tasks found</div>';
                dropdown.style.display = 'block';
                return;
            }
            for (const task of data.tasks) {
                _taskCache[task.id || task.text] = task;
                const row = document.createElement('div');
                row.className = 'wv-task-match';
                row.innerHTML = '<span class="wv-task-match-id">' + (task.id || '?') + '</span>'
                    + '<span class="wv-task-match-text">' + (task.text || '') + '</span>'
                    + '<span class="badge badge-muted" style="font-size:9px">' + (task.state || '') + '</span>';
                row.addEventListener('pointerdown', (e) => {
                    e.preventDefault(); e.stopPropagation();
                    const selected = {id: task.id || '', text: task.text || '', state: task.state || ''};
                    dropdown.style.display = 'none';
                    onSelect(selected);
                });
                dropdown.appendChild(row);
            }
            dropdown.style.display = 'block';
        } catch (e) { console.error('Task search failed:', e); }
    }

    async function fetchTaskById(taskId) {
        try {
            const r = await fetch('/api/tasks/search?q=' + encodeURIComponent(taskId) + '&limit=1');
            const data = await r.json();
            if (data.tasks && data.tasks.length > 0) _taskCache[taskId] = data.tasks[0];
        } catch (e) { /* silent */ }
    }

    /**
     * Replace a single group card in-place without full re-render.
     * Falls back to full render() if the card isn't found in the DOM.
     */
    function rerenderCard(index) {
        const old = container.querySelector('.wv-group-card[data-group-index="' + index + '"]');
        if (!old) { render(); return; }

        const parent = old.parentElement;
        const group = state.groups.find(g => g.index === index);
        const newGroup = state.newGroups.find(ng => ng.tempIndex === index);

        if (group) {
            const tmp = document.createElement('div');
            renderGroupCard(tmp, group);
            parent.replaceChild(tmp.firstElementChild, old);
        } else if (newGroup) {
            const tmp = document.createElement('div');
            renderNewGroupCard(tmp, newGroup);
            parent.replaceChild(tmp.firstElementChild, old);
        }
    }

    function moveItemToGroup(item, fromGroup, toIndex) {
        const fromIdx = fromGroup.index !== undefined ? fromGroup.index : fromGroup.tempIndex;
        state.reassignments.push({ item_id: item.id, from_group: fromIdx, to_group: toIndex });
        if (fromGroup._items) fromGroup._items = fromGroup._items.filter(i => i.id !== item.id);
        else if (fromGroup.items) fromGroup.items = fromGroup.items.filter(i => i.id !== item.id);
        // Preserve item override — don't clear it on move
        const target = state.groups.find(g => g.index === toIndex);
        if (target) { if (!target._items) target._items = [...target.items]; target._items.push(item); }
        else { const tNew = state.newGroups.find(ng => ng.tempIndex === toIndex); if (tNew) tNew.items.push(item); }
        // Targeted re-render: only the two affected cards
        rerenderCard(fromIdx);
        rerenderCard(toIndex);
    }

    function showNewGroupPrompt(parent, item, fromGroup) {
        let existing = parent.querySelector('.wv-new-group-input');
        if (existing) existing.remove();
        const inputRow = document.createElement('div');
        inputRow.className = 'wv-new-group-input';
        const input = document.createElement('input');
        input.type = 'text'; input.placeholder = 'Name this new group...';
        const createBtn = document.createElement('button');
        createBtn.className = 'primary'; createBtn.textContent = 'Create Group';
        const cancelBtn = document.createElement('button');
        cancelBtn.textContent = 'Cancel';
        createBtn.addEventListener('click', () => {
            const intent = input.value.trim() || 'New group (' + item.label + ')';
            createNewGroupWithItem(item, fromGroup, intent); inputRow.remove();
        });
        cancelBtn.addEventListener('click', () => { inputRow.remove(); render(); });
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') createBtn.click(); if (e.key === 'Escape') { inputRow.remove(); render(); } });
        inputRow.appendChild(input); inputRow.appendChild(createBtn); inputRow.appendChild(cancelBtn);
        const footer = parent.querySelector('.wv-footer');
        if (footer) parent.insertBefore(inputRow, footer); else parent.appendChild(inputRow);
        input.focus();
    }

    function createNewGroupWithItem(item, fromGroup, intent) {
        const tempIdx = state.nextTempIndex--;
        const ng = { tempIndex: tempIdx, intent, items: [item] };
        state.newGroups.push(ng);
        const fromIdx = fromGroup.index !== undefined ? fromGroup.index : fromGroup.tempIndex;
        state.reassignments.push({ item_id: item.id, from_group: fromIdx, to_group: tempIdx });
        if (fromGroup._items) fromGroup._items = fromGroup._items.filter(i => i.id !== item.id);
        else if (fromGroup.items) fromGroup.items = fromGroup.items.filter(i => i.id !== item.id);
        state.decisions[tempIdx] = 'leave';
        // Targeted: re-render source card + append new group card (no full render)
        rerenderCard(fromIdx);
        // Insert new group card before the drop zone
        const dropZone = container.querySelector('.wv-new-group-zone');
        if (dropZone) {
            const tmp = document.createElement('div');
            renderNewGroupCard(tmp, ng);
            dropZone.parentElement.insertBefore(tmp.firstElementChild, dropZone);
        } else {
            render();  // fallback
        }
    }

    function renderNewGroupCard(parent, ng) {
        // Reuse renderGroupCard with a synthetic group object
        const synth = {
            index: ng.tempIndex,
            intent: '\u2728 ' + ng.intent,
            confidence: '',
            _items: ng.items,
            items: ng.items,
            rationale: '',
            context: 'User-created group',
            ambiguities: [],
            likely_task_id: '',
            suggested_action: state.decisions[ng.tempIndex] || 'leave',
        };
        renderGroupCard(parent, synth);
        // Add dashed border to distinguish
        const card = parent.querySelector('[data-group-index="' + ng.tempIndex + '"]');
        if (card) card.classList.add('wv-new-group-card');
    }

    async function submitDecisions(btn) {
        const gd = [];
        for (const g of state.groups) {
            const ov = [];
            for (const item of (g._items || g.items)) { if (state.itemOverrides[item.id]) ov.push({item_id: item.id, action: state.itemOverrides[item.id]}); }
            const entry = { group_index: g.index, action: state.decisions[g.index] || g.suggested_action, item_overrides: ov };
            if (state.taskAssignments[g.index]) entry.target_task_id = state.taskAssignments[g.index];
            if (state.newTaskTexts[g.index]) entry.new_task_text = state.newTaskTexts[g.index];
            if (state.overrideReasons[g.index]) entry.override_reason = state.overrideReasons[g.index];
            if ((state.namespaceTags[g.index] || []).length > 0) {
                entry.namespace_tags = [...state.namespaceTags[g.index]];
            }
            gd.push(entry);
        }
        for (const ng of state.newGroups) {
            const entry = { group_index: ng.tempIndex, intent: ng.intent, action: state.decisions[ng.tempIndex] || 'leave', items: ng.items.map(i => i.id), item_overrides: [] };
            if (state.taskAssignments[ng.tempIndex]) entry.target_task_id = state.taskAssignments[ng.tempIndex];
            if (state.newTaskTexts[ng.tempIndex]) entry.new_task_text = state.newTaskTexts[ng.tempIndex];
            if ((state.namespaceTags[ng.tempIndex] || []).length > 0) {
                entry.namespace_tags = [...state.namespaceTags[ng.tempIndex]];
            }
            gd.push(entry);
        }
        btn.textContent = 'Submitting...'; btn.disabled = true;
        try {
            await onSubmit(gd, state.reassignments);
            container.innerHTML = '<div class="empty-state">\u2705 Decisions submitted.</div>';
            setTimeout(() => onComplete(), 2000);
        } catch (e) { btn.textContent = 'Error \u2014 try again'; btn.disabled = false; }
    }

    render();

    // ------------------------------------------------------------------
    // Per-card mutation handle (SSE-driven incremental updates)
    // ------------------------------------------------------------------
    //
    // The dispatcher in script_event_bus.py calls these mutators when a
    // pool.* event arrives. The handle closes over the same `state`,
    // `dragItem`/`dragSourceGroup`, and helpers as the rest of this
    // function — no closure-lift required. SSE handlers MUST NOT call
    // any panel-wide loader (e.g. loadReview()); the regression test
    // ``test_no_wholesale_loader_calls_in_event_handlers`` enforces.
    //
    // See architecture/event-bus for the full per-card mutation
    // contract: animation cancellation, focus capture/restore,
    // aria-live announcements, drag-state nulling, state-dict pruning,
    // ordering-inversion mitigation via _pendingRemovals.

    const _pendingRemovals = new Set();  // keys: `${run_id}::${item_id}`
    const _ariaLive = (function() {
        let el = container.querySelector('[data-wb-live-region]');
        if (el) return el;
        el = document.createElement('div');
        el.setAttribute('role', 'status');
        el.setAttribute('aria-live', 'polite');
        el.setAttribute('aria-atomic', 'true');
        el.dataset.wbLiveRegion = '';
        el.className = 'visually-hidden';
        container.appendChild(el);
        return el;
    })();
    function _announce(msg) { if (_ariaLive) _ariaLive.textContent = msg; }
    function _findCard(run_id, item_id) {
        return container.querySelector(
            '[data-pool-run-id="' + run_id + '"][data-item-id="' + item_id + '"]'
        );
    }
    function _decorate(card, group) {
        // Resolution Surface decorator is idempotent (script_resolution.py:85).
        const dec = (typeof options.decorateCard === 'function')
            ? options.decorateCard
            : (() => {});
        try { dec(card, group); }
        catch (e) { console.error('[surface] decorateCard threw:', e); }
    }

    return {
        appendCard(group) {
            if (!group || !group.pool_run_id) return;
            const firstItem = (group.items || [])[0];
            if (!firstItem || !firstItem.id) return;
            const key = group.pool_run_id + '::' + firstItem.id;
            // Ordering inversion: if a removal already arrived for
            // this key (in-process state-change beat the cross-process
            // add through the messaging bridge), the card is already
            // resolved server-side — discard the late add.
            if (_pendingRemovals.has(key)) {
                _pendingRemovals.delete(key);
                return;
            }
            // DOM-presence guard — never double-mount the same entry.
            if (_findCard(group.pool_run_id, firstItem.id)) return;
            // Splice into closure state so per-card handlers (pill
            // clicks, drag) can find the group via state.groups.
            const synth = {...group, _items: [...(group.items || [])]};
            if (synth.index === undefined || synth.index === null) {
                synth.index = state.groups.length + state.newGroups.length;
            }
            state.groups.push(synth);
            if (synth.suggested_action) {
                state.decisions[synth.index] = synth.suggested_action;
            }
            if (synth.likely_task_id) state.taskAssignments[synth.index] = synth.likely_task_id;
            if (synth.suggested_task_text) state.newTaskTexts[synth.index] = synth.suggested_task_text;
            if (Array.isArray(synth.suggested_namespace_tags)) {
                state.namespaceTags[synth.index] = [...synth.suggested_namespace_tags];
            }
            renderGroupCard(container, synth);
            const card = _findCard(group.pool_run_id, firstItem.id);
            if (card) {
                card.classList.add('wv-incoming');
                _decorate(card, synth);
            }
            _announce('1 new triage item');
        },

        removeCard(run_id, item_id) {
            const card = _findCard(run_id, item_id);
            if (!card) {
                _pendingRemovals.add(run_id + '::' + item_id);
                return;
            }
            // Capture focus so we can restore to a sibling after removal.
            const focused = document.activeElement;
            const focusInside = focused && card.contains(focused);
            let focusTarget = null;
            if (focusInside) {
                focusTarget = card.nextElementSibling
                    || card.previousElementSibling
                    || container;
            }
            // Cancel in-flight animations (e.g. wv-incoming) so the
            // wv-leaving animation gets a clean run.
            try {
                if (typeof card.getAnimations === 'function') {
                    card.getAnimations({subtree: true}).forEach(a => {
                        try { a.cancel(); } catch (_) {}
                    });
                }
            } catch (_) {}
            // Null any drag state pointing into this group so the
            // drop handler doesn't dereference a removed reference.
            const groupIndex = parseInt(card.dataset.groupIndex, 10);
            const group = state.groups.find(g => g.index === groupIndex);
            if (group && dragSourceGroup === group) {
                dragItem = null;
                dragSourceGroup = null;
            }
            card.classList.add('wv-leaving');
            let done = false;
            const cleanup = () => {
                if (done) return;
                done = true;
                if (group) {
                    state.groups = state.groups.filter(g => g.index !== groupIndex);
                    delete state.decisions[groupIndex];
                    delete state.taskAssignments[groupIndex];
                    delete state.newTaskTexts[groupIndex];
                    delete state.namespaceTags[groupIndex];
                    delete state.overrideReasons[groupIndex];
                    for (const it of (group._items || group.items || [])) {
                        delete state.itemOverrides[it.id];
                    }
                }
                card.remove();
                if (focusTarget && typeof focusTarget.focus === 'function') {
                    try { focusTarget.focus({preventScroll: true}); } catch (_) {}
                }
            };
            card.addEventListener('animationend', cleanup, {once: true});
            // Failsafe: if animationend never fires (browser quirk
            // when element is detached, or display:none on parent).
            setTimeout(cleanup, 350);
            _announce('Item resolved');
        },

        updateCard(run_id, item_id, freshGroup) {
            const card = _findCard(run_id, item_id);
            if (!card || !freshGroup) return;
            // Render the fresh card off-DOM so morphdom can diff it.
            const stage = document.createElement('div');
            const synth = {...freshGroup, _items: [...(freshGroup.items || [])]};
            // Preserve the existing groupIndex so internal handlers
            // continue to address the same state slot.
            const groupIndex = parseInt(card.dataset.groupIndex, 10);
            if (!Number.isNaN(groupIndex)) synth.index = groupIndex;
            renderGroupCard(stage, synth);
            const fresh = stage.firstElementChild;
            if (!fresh) return;
            if (typeof window.morphdom === 'function') {
                window.morphdom(card, fresh, {
                    onBeforeElUpdated(fromEl, toEl) {
                        // Preserve user-entered text across diffs:
                        // never clobber a focused input, and never
                        // replace a non-empty value with an empty one.
                        if (fromEl.tagName === 'INPUT' || fromEl.tagName === 'TEXTAREA') {
                            if (document.activeElement === fromEl) return false;
                            const v = (fromEl.value || '').trim();
                            if (v && (toEl.value || '').trim() === '') return false;
                        }
                        return !fromEl.isEqualNode(toEl);
                    },
                });
                _decorate(card, synth);
            }
        },

        bumpAttractionPasses(run_id, item_id, count) {
            const card = _findCard(run_id, item_id);
            if (!card) return;
            const groupIndex = parseInt(card.dataset.groupIndex, 10);
            const group = state.groups.find(g => g.index === groupIndex);
            if (group) group.attraction_passes = count;
            let badge = card.querySelector('.wv-pass-count');
            if (!badge && group) {
                _decorate(card, group);  // Resolution Surface mounts the badge.
                badge = card.querySelector('.wv-pass-count');
            }
            if (!badge) return;
            badge.textContent = '⏳ ' + count;
            // Reflow trick: removing+re-adding the same animation
            // class requires a layout flush to restart the keyframes.
            badge.classList.remove('wv-pulse');
            void badge.offsetWidth;
            badge.classList.add('wv-pulse');
        },

        setForcedContextStored(run_id, item_id) {
            const card = _findCard(run_id, item_id);
            if (!card) return;
            // Same visual retire as the user-initiated Re-direct path
            // in script_resolution.py:385-392 — keeps consistency.
            card.classList.add('wv-card-redirected');
        },

        isMounted() {
            return document.body.contains(container);
        },
    };
}

// Thin wrapper preserving the workflow-view modal's original
// behaviour: post to /api/workflow-views/<viewId>/respond and
// remove the tab on completion. The inline Review tab mounts
// renderTriageReview directly with its own onSubmit.
registerViewRenderer('triage_review', function(container, viewId, payload) {
    renderTriageReview(container, payload.presentation || {}, {
        onSubmit: async (gd, reassignments) => {
            await fetch('/api/workflow-views/' + viewId + '/respond', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ phase: 'review', group_decisions: gd, reassignments: reassignments }),
            });
        },
        onComplete: () => removeWorkflowTab(viewId),
    });
});
"""


# ---------------------------------------------------------------------------
# Thread Chat JS — decoupled, mountable component
# ---------------------------------------------------------------------------
