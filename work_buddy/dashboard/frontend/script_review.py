"""Dashboard JS for the Review tab.

Thin adapter: fetches the pending-review presentation and mounts it
through the shared ``renderTriageReview`` renderer defined in
``script_triage.py``. No custom card code — that renderer already
handles action-column layout, override flow, drag-to-reassign, new
groups, and submit. We just provide the ``onSubmit`` callback that
posts decisions to ``/api/review/execute`` (which runs the triage
executor AND marks pool entries reviewed).

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

    if (typeof renderTriageReview !== 'function') {
        container.innerHTML = '<div class="empty-state">renderTriageReview not loaded \u2014 ' +
            'script_triage.py must be included in the page.</div>';
        return;
    }

    renderTriageReview(container, presentation, {
        perGroupSubmit: true,
        showNamespaceTags: true,
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
