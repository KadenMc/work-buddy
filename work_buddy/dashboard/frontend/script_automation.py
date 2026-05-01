"""Slice 4 dashboard JS: Review Queue + Daily Log surfaces.

Two thin renderers:

* ``loadReviewQueue()`` — fetches ``/api/automation/review-queue`` and
  renders one card per tier-3 task, each with its typed pipeline-
  blocker badge (per ROADMAP §3.3) and tier badge.  No keyboard
  layer here yet (the main Review tab owns the j/k/r/s flow); we
  reuse the same visual language so cards feel consistent.

* ``loadDailyLog()`` — fetches ``/api/automation/daily-log`` and
  renders collapsible category groups.  Read-only in Slice 4; the
  demote-category action lands in a follow-up.

Both renderers are forgiving on empty / error: they write
informative empty-state text rather than throwing, so a malformed
backend doesn't break the rest of the dashboard.
"""

from __future__ import annotations


def _automation_script() -> str:
    return r"""
// ---------------------------------------------------------------------------
// Slice 4 — Review Queue (tier-3 outputs awaiting accept/revise/reject)
// ---------------------------------------------------------------------------

async function loadReviewQueue() {
    const root = document.getElementById('review-queue-items');
    const summary = document.getElementById('review-queue-summary');
    if (!root) return;
    root.innerHTML = '<div class="loading">Loading review queue...</div>';
    if (summary) summary.textContent = '';

    let data;
    try {
        const resp = await fetch('/api/automation/review-queue');
        data = await resp.json();
    } catch (e) {
        root.innerHTML = '<div class="empty-state">Failed to load review queue: '
            + _autEsc(String(e)) + '</div>';
        return;
    }
    if (!data || data.status !== 'ok') {
        root.innerHTML = '<div class="empty-state">Review queue load failed'
            + (data && data.error ? ': ' + _autEsc(data.error) : '')
            + '</div>';
        return;
    }

    if (data.count === 0) {
        root.innerHTML = '<div class="empty-state">No tier-3 outputs awaiting review. '
            + 'Tasks land here when their risk profile resolves to operating tier 3 '
            + '(critical-accuracy work — the agent produces the output, you accept '
            + 'or revise).</div>';
        if (summary) summary.textContent = '';
        return;
    }

    if (summary) {
        summary.textContent = data.count + ' task'
            + (data.count === 1 ? '' : 's') + ' awaiting review';
    }

    root.innerHTML = '';
    const grid = document.createElement('div');
    grid.className = 'aut-rq-grid';
    data.items.forEach(item => grid.appendChild(_autBuildReviewQueueCard(item)));
    root.appendChild(grid);
}

function _autBuildReviewQueueCard(item) {
    const card = document.createElement('div');
    card.className = 'aut-rq-card';
    card.dataset.taskId = item.task_id || '';

    // Header row: text + tier badge + actor
    const header = document.createElement('div');
    header.className = 'aut-rq-header';

    const title = document.createElement('div');
    title.className = 'aut-rq-title';
    title.textContent = item.text || item.task_id || '(untitled)';
    header.appendChild(title);

    const meta = document.createElement('div');
    meta.className = 'aut-rq-meta';

    const tierBadge = document.createElement('span');
    tierBadge.className = 'aut-tier-badge tier-' + (item.operating || 0);
    tierBadge.title = 'Operating tier (achievable=' + (item.achievable ?? '?')
        + ', allowed_under_risk=' + (item.allowed_under_risk ?? '?') + ')';
    tierBadge.textContent = 'tier ' + (item.operating ?? '?');
    meta.appendChild(tierBadge);

    if (item.last_actor) {
        const actor = document.createElement('span');
        actor.className = 'aut-actor-badge actor-' + item.last_actor;
        actor.textContent = item.last_actor;
        actor.title = 'Last actor';
        meta.appendChild(actor);
    }

    header.appendChild(meta);
    card.appendChild(header);

    // Pipeline blocker badge (typed reason)
    if (item.pipeline_blocker) {
        const blk = item.pipeline_blocker;
        const wrap = document.createElement('div');
        wrap.className = 'wv-blocker-badge tone-' + (blk.tone || 'info');
        wrap.title = blk.detail || blk.label || '';
        const icon = blk.tone === 'blocked' ? '⛔'
                   : blk.tone === 'deferred' ? '⏳'
                   : 'ℹ';
        wrap.innerHTML = '<span class="wv-blocker-icon">' + icon + '</span>'
            + '<span class="wv-blocker-label">' + _autEsc(blk.label || blk.kind) + '</span>';
        if (blk.deep_link) {
            const link = document.createElement('a');
            link.href = blk.deep_link;
            link.className = 'wv-blocker-link';
            link.textContent = blk.deep_link_label || 'Open';
            link.addEventListener('click', e => e.stopPropagation());
            wrap.appendChild(link);
        }
        card.appendChild(wrap);
    }

    // Reasons list — what the resolver capped on. Helps the user
    // calibrate tolerance settings ("oh, I can bump accuracy
    // tolerance for this category").
    if (Array.isArray(item.reasons) && item.reasons.length) {
        const r = document.createElement('ul');
        r.className = 'aut-rq-reasons';
        item.reasons.forEach(reason => {
            const li = document.createElement('li');
            li.textContent = reason;
            r.appendChild(li);
        });
        card.appendChild(r);
    }

    // Footer — contract / state / updated_at
    const footer = document.createElement('div');
    footer.className = 'aut-rq-footer';
    const bits = [];
    if (item.contract) bits.push('contract: ' + _autEsc(item.contract));
    if (item.state) bits.push('state: ' + _autEsc(item.state));
    if (item.urgency && item.urgency !== 'medium') {
        bits.push('urgency: ' + _autEsc(item.urgency));
    }
    if (item.updated_at) {
        bits.push(new Date(item.updated_at).toLocaleString());
    }
    footer.innerHTML = bits.join(' · ');
    card.appendChild(footer);

    return card;
}


// ---------------------------------------------------------------------------
// Slice 4 — Daily Log (tier-4 actions, collapsible by category)
// ---------------------------------------------------------------------------

async function loadDailyLog() {
    const root = document.getElementById('daily-log-categories');
    const summary = document.getElementById('daily-log-summary');
    const win = document.getElementById('daily-log-window');
    const days = (win && win.value) || '1';
    if (!root) return;
    root.innerHTML = '<div class="loading">Loading daily log...</div>';
    if (summary) summary.textContent = '';

    let data;
    try {
        const resp = await fetch('/api/automation/daily-log?days=' + encodeURIComponent(days));
        data = await resp.json();
    } catch (e) {
        root.innerHTML = '<div class="empty-state">Failed to load daily log: '
            + _autEsc(String(e)) + '</div>';
        return;
    }
    if (!data || data.status !== 'ok') {
        root.innerHTML = '<div class="empty-state">Daily log load failed'
            + (data && data.error ? ': ' + _autEsc(data.error) : '')
            + '</div>';
        return;
    }

    if (!data.total_events) {
        root.innerHTML = '<div class="empty-state">No tier-4 actions in this window. '
            + 'When the agent acts autonomously on a tier-4 task '
            + '(maintenance / cleanup), the action appears here grouped by '
            + 'category. The window is configurable above.</div>';
        if (summary) summary.textContent = '';
        return;
    }

    if (summary) {
        summary.textContent = data.total_events + ' autonomous action'
            + (data.total_events === 1 ? '' : 's')
            + ' across ' + data.categories.length + ' categor'
            + (data.categories.length === 1 ? 'y' : 'ies')
            + ' (last ' + data.window_days + ' day'
            + (data.window_days === 1 ? '' : 's') + ')';
    }

    root.innerHTML = '';
    data.categories.forEach(cat => root.appendChild(_autBuildCategoryGroup(cat)));
}

function _autBuildCategoryGroup(cat) {
    const wrap = document.createElement('details');
    wrap.className = 'aut-dl-group';
    // Default open if ≤3 events, collapsed otherwise — long lists
    // shouldn't dominate the surface.
    if ((cat.count || 0) <= 3) wrap.open = true;

    const summary = document.createElement('summary');
    summary.className = 'aut-dl-summary';
    summary.innerHTML =
        '<span class="aut-dl-cat-name">' + _autEsc(cat.category) + '</span> '
        + '<span class="aut-dl-cat-count">(' + (cat.count || 0) + ')</span>';
    wrap.appendChild(summary);

    const list = document.createElement('ul');
    list.className = 'aut-dl-events';
    (cat.events || []).forEach(ev => {
        const li = document.createElement('li');
        li.className = 'aut-dl-event';
        const when = ev.changed_at
            ? new Date(ev.changed_at).toLocaleString()
            : '';
        const transition = (ev.old_state || '?') + ' → ' + (ev.new_state || '?');
        const reason = ev.reason ? ' (' + _autEsc(ev.reason) + ')' : '';
        li.innerHTML =
            '<span class="aut-dl-time">' + _autEsc(when) + '</span> '
            + '<span class="aut-dl-text">' + _autEsc(ev.text || ev.task_id || '') + '</span> '
            + '<span class="aut-dl-transition">' + _autEsc(transition) + '</span>'
            + '<span class="aut-dl-reason">' + reason + '</span>'
            + (ev.last_actor
                ? ' <span class="aut-actor-badge actor-' + _autEsc(ev.last_actor) + '">'
                  + _autEsc(ev.last_actor) + '</span>'
                : '');
        list.appendChild(li);
    });
    wrap.appendChild(list);
    return wrap;
}


// ---- Helpers --------------------------------------------------------------

function _autEsc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}
"""


def _automation_styles() -> str:
    return r"""
/* Slice 4 — Review Queue + Daily Log surfaces */

.section-subtitle {
    font-size: 11px;
    font-weight: normal;
    color: var(--text-muted, #888);
    margin-left: 8px;
}

/* Review Queue grid */
.aut-rq-grid {
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.aut-rq-card {
    border: 1px solid var(--border-muted, #ddd);
    border-left: 3px solid var(--accent, #4a6fa5);
    background: var(--bg-secondary, #fafafa);
    border-radius: 6px;
    padding: 10px 12px;
}
.aut-rq-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 6px;
}
.aut-rq-title {
    font-size: 14px;
    font-weight: 500;
    color: var(--text-primary, #222);
    flex: 1 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
}
.aut-rq-meta { display: flex; gap: 6px; flex: 0 0 auto; }
.aut-tier-badge {
    font-size: 10px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 10px;
    background: var(--bg-tertiary, #eee);
    color: var(--text-muted, #555);
    cursor: help;
}
.aut-tier-badge.tier-3 { background: #fff4d6; color: #6a4b00; }
.aut-tier-badge.tier-4 { background: #e3f4d4; color: #2d5a2d; }
.aut-tier-badge.tier-2 { background: #fde2e2; color: #8a1f1f; }
.aut-actor-badge {
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 10px;
    background: var(--bg-tertiary, #eee);
    color: var(--text-muted, #555);
}
.aut-actor-badge.actor-user  { background: #e2ecfd; color: #1f3f8a; }
.aut-actor-badge.actor-agent { background: #f0e5fa; color: #4a2d6a; }
.aut-rq-reasons {
    margin: 6px 0 0 16px;
    padding: 0;
    font-size: 11px;
    color: var(--text-muted, #666);
}
.aut-rq-reasons li { line-height: 1.4; }
.aut-rq-footer {
    margin-top: 8px;
    font-size: 11px;
    color: var(--text-muted, #888);
}

/* Daily Log */
.aut-dl-group {
    border: 1px solid var(--border-muted, #ddd);
    border-radius: 6px;
    padding: 6px 10px;
    margin-bottom: 8px;
    background: var(--bg-secondary, #fafafa);
}
.aut-dl-summary {
    cursor: pointer;
    font-weight: 500;
    color: var(--text-primary, #222);
    list-style: none;
}
.aut-dl-summary::-webkit-details-marker { display: none; }
.aut-dl-summary::before {
    content: '\25b8';  /* ▸ */
    display: inline-block;
    width: 12px;
    transition: transform 0.1s;
}
details[open] > .aut-dl-summary::before { transform: rotate(90deg); }
.aut-dl-cat-name { color: var(--text-primary, #222); }
.aut-dl-cat-count { color: var(--text-muted, #888); font-weight: normal; }
.aut-dl-events {
    list-style: none;
    margin: 8px 0 0 12px;
    padding: 0;
    font-size: 12px;
}
.aut-dl-event {
    padding: 4px 0;
    border-bottom: 1px dashed var(--border-muted, #eee);
    line-height: 1.5;
}
.aut-dl-event:last-child { border-bottom: none; }
.aut-dl-time {
    color: var(--text-muted, #888);
    font-family: monospace;
    font-size: 11px;
}
.aut-dl-text {
    color: var(--text-primary, #222);
    margin-left: 8px;
}
.aut-dl-transition {
    color: var(--text-muted, #555);
    font-family: monospace;
    margin-left: 8px;
}
.aut-dl-reason {
    color: var(--text-muted, #888);
    font-style: italic;
}
"""
