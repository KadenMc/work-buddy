"""Dashboard JS for the Review tab.

Thin adapter: fetches the pending-review presentation and mounts the
Slice 1.5 Resolution Surface (``mountResolutionSurface`` defined in
``script_resolution.py``), which decorates the shared
``renderTriageReview`` renderer with type-aware affordances:

- pipeline-blocker badges (typed reasons per ROADMAP §3.3)
- attraction-passes count (Slice 8 will read this)
- Defer button + ``s`` keybinding → POST /api/triage/defer
- Re-direct button + ``r`` keybinding → POST /api/triage/redirect
- keyboard navigation (j/k/enter/r/s/?)

The underlying ``renderTriageReview`` still handles the per-card
edit flow (action pills, drag-to-reassign, namespace tags, per-source
open buttons). All Slice 1 patterns (user_initiated consent context,
success-only mark_reviewed filter, operation_errors surfacing,
raw-entry rendering, perGroupSubmit, bridge-timeout handling) are
preserved by delegation through that renderer + ``/api/review/execute``.

Originally (Phase 2 first pass) this file had its own simpler card
renderer, but that duplicated the Chrome renderer's logic AND its
Approve/Skip handlers were buggy (Skip was a client-only hide,
Approve broke on the pre-fix ``fetchJSON`` signature). Swapping to
the shared renderer eliminates both problems.
"""

from __future__ import annotations


def _review_script() -> str:
    return r"""
// ---- Review tab: background-triage pending-review pool ----

async function loadReview() {
    const container = document.getElementById('review-groups');
    const narrative = document.getElementById('review-narrative');
    if (!container) return;
    container.innerHTML = '<div class="loading">Loading review items...</div>';
    if (narrative) narrative.textContent = '';

    const sourceSel = document.getElementById('review-source-filter');
    const qs = sourceSel && sourceSel.value ? ('?source=' + encodeURIComponent(sourceSel.value)) : '';

    // Persist source filter selection into the URL hash so reload restores it.
    if (typeof _persistHash === 'function') _persistHash();

    const data = await fetchJSON('/api/review' + qs);
    if (!data || data.status === 'error') {
        container.innerHTML = '<div class="empty-state">Failed to load review pool' +
            (data && data.error ? ': ' + data.error : '') + '</div>';
        return;
    }
    if (data.status === 'empty' || !(data.presentation && data.presentation.total_items)) {
        container.innerHTML = '<div class="empty-state">No pending triage proposals. ' +
            'The hourly journal-triage cron populates this pool; wait for the next run ' +
            'or trigger one via <code>wb_run("journal_triage_scan")</code>.</div>';
        return;
    }

    const presentation = data.presentation;
    if (narrative) narrative.textContent = presentation.narrative || '';

    // Clear the loading state and mount the shared renderer directly
    // into our panel container. The renderer fills `container` with
    // its own header + stats + action-column grid.
    container.innerHTML = '';

    if (typeof window.mountResolutionSurface !== 'function') {
        container.innerHTML = '<div class="empty-state">Resolution Surface not loaded \u2014 ' +
            'script_resolution.py and script_triage.py must be included in the page.</div>';
        return;
    }

    // Slice 1.5: mount the Resolution Surface (decorator over
    // renderTriageReview). Adds typed pipeline-blocker badges,
    // attraction_passes display, Defer + Re-direct affordances,
    // and the j/k/enter/r/s/? keyboard layer.
    window.mountResolutionSurface(container, presentation, {
        onSubmit: async (gd, reassignments) => {
            const resp = await fetchJSON('/api/review/execute', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    presentation: presentation,
                    decisions: { group_decisions: gd, reassignments: reassignments },
                }),
            });
            if (!resp || (resp.status !== 'ok' && resp.status !== 'partial')) {
                throw new Error('execute failed: ' + JSON.stringify(resp));
            }
        },
        onComplete: () => {
            // Refresh the tab so newly-cleared pool entries drop out
            // and any entries added by a recent cron cycle appear.
            closeReviewDrawer();
            loadReview();
        },
        onItemClick: (item, group) => openReviewDrawer(item, group, presentation),
    });
}

// ---- Right-side item-detail drawer ----
// Persistent in the DOM (see html.py). Slides in/out via the
// `.open` class on `#review-drawer`. Populated on demand from the
// item + its enclosing group. If the pool entry carries IR context
// in item.metadata.ir_context, surface the top hits.

function openReviewDrawer(item, group, presentation) {
    const drawer = document.getElementById('review-drawer');
    const body = document.getElementById('review-drawer-body');
    const titleEl = document.getElementById('review-drawer-title');
    if (!drawer || !body || !titleEl) return;

    titleEl.textContent = item.label || item.id || 'Item detail';
    body.innerHTML = '';

    // --- Full text ---
    const fullText = item.summary || item.text || item.label || '';
    if (fullText) {
        const sec = document.createElement('div');
        sec.className = 'review-drawer-section';
        sec.innerHTML = '<span class="review-drawer-section-label">Full text</span>';
        const pre = document.createElement('div');
        pre.className = 'review-drawer-text';
        pre.textContent = fullText;
        sec.appendChild(pre);
        body.appendChild(sec);
    }

    // --- Agent's rationale for this item's group ---
    if (group && group.rationale) {
        const sec = document.createElement('div');
        sec.className = 'review-drawer-section';
        sec.innerHTML = '<span class="review-drawer-section-label">Agent rationale</span>' +
            '<div class="review-drawer-rationale">' + _reviewEscape(group.rationale) + '</div>';
        body.appendChild(sec);
    }

    // --- Suggested task text (for create_task groups) ---
    if (group && group.suggested_task_text) {
        const sec = document.createElement('div');
        sec.className = 'review-drawer-section';
        sec.innerHTML = '<span class="review-drawer-section-label">Proposed task</span>' +
            '<div class="review-drawer-rationale">' + _reviewEscape(group.suggested_task_text) + '</div>';
        body.appendChild(sec);
    }

    // --- URL (Chrome) ---
    if (item.url) {
        const sec = document.createElement('div');
        sec.className = 'review-drawer-section';
        sec.innerHTML = '<span class="review-drawer-section-label">URL</span>';
        const a = document.createElement('a');
        a.href = item.url;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = item.url;
        sec.appendChild(a);
        body.appendChild(sec);
    }

    // --- Per-source "open in app" actions (e.g. email → Thunderbird) ---
    // Same shape and POST target as the inline buttons in script_triage.py.
    // Source-of-truth: work_buddy.triage.card_actions.
    if (Array.isArray(item.actions) && item.actions.length > 0) {
        const sec = document.createElement('div');
        sec.className = 'review-drawer-section';
        sec.innerHTML = '<span class="review-drawer-section-label">Actions</span>';
        for (const act of item.actions) {
            const btn = document.createElement('button');
            btn.className = 'review-drawer-action-btn';
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
                            // Self-heal: source is gone. Quarantine the
                            // entry so the stale card vanishes from the
                            // pool on next refresh. The drawer doesn't
                            // have a row reference to fade out — feedback
                            // is via button state + title only.
                            await window._wvQuarantineEntry(group, item, errorKind, btn, null);
                        } else {
                            console.error('[review-drawer] action failed:', msg);
                            btn.title = msg;
                        }
                    }
                } catch (err) {
                    console.error('[review-drawer] action threw:', err);
                    btn.title = String(err);
                } finally {
                    btn.disabled = false;
                }
            });
            sec.appendChild(btn);
        }
        body.appendChild(sec);
    }

    // --- IR context hits (if the adapter attached any) ---
    const meta = item.metadata || {};
    const irHits = meta.ir_context || [];
    if (irHits.length > 0) {
        const sec = document.createElement('div');
        sec.className = 'review-drawer-section';
        sec.innerHTML = '<span class="review-drawer-section-label">Related context</span>';
        irHits.slice(0, 5).forEach(hit => {
            const h = document.createElement('div');
            h.className = 'review-drawer-ir-hit';
            const src = (hit.source || '') + (hit.doc_id ? ' · ' + hit.doc_id : '');
            h.innerHTML = '<div class="review-drawer-ir-src">' + _reviewEscape(src) + '</div>' +
                '<div class="review-drawer-ir-text">' + _reviewEscape(hit.display_text || '') + '</div>';
            sec.appendChild(h);
        });
        body.appendChild(sec);
    }

    if (!body.children.length) {
        body.innerHTML = '<div class="review-drawer-empty">No additional detail for this item.</div>';
    }

    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
}

function closeReviewDrawer() {
    const drawer = document.getElementById('review-drawer');
    if (!drawer) return;
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
}

function _reviewEscape(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}
"""
