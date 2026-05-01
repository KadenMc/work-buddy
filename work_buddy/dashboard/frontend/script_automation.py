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


// ---------------------------------------------------------------------------
// Slice 5a — Engage view + blocked-by-context daily nudge
// ---------------------------------------------------------------------------

const ENGAGE_PRESETS = {
    at_desk: ['@filesystem', '@vault', '@web_public', '@user_workstation',
              '@user_creds', '@email_send', '@email_read', '@github',
              '@chrome_active', '@llm'],
    phone_only: ['@phone_voice', '@web_public', '@user_creds'],
    untethered: ['@physical', '@in_person', '@phone_voice'],
};

function _engageGetCurrentContexts() {
    const sel = document.getElementById('engage-context-preset');
    const preset = (sel && sel.value) || 'at_desk';
    if (preset === 'custom') {
        const stored = localStorage.getItem('engage.custom_contexts');
        if (stored) {
            try { return JSON.parse(stored) || []; }
            catch (e) { return []; }
        }
        return [];
    }
    return ENGAGE_PRESETS[preset] || ENGAGE_PRESETS.at_desk;
}

function onEngageContextPresetChange() {
    const sel = document.getElementById('engage-context-preset');
    const preset = (sel && sel.value) || 'at_desk';
    localStorage.setItem('engage.preset', preset);
    if (preset === 'custom') {
        // Bare-bones custom prompt; the proper editor lands with the
        // Slice 5b Today tab. The localStorage value persists across
        // reloads even with the lightweight prompt UI.
        const cur = _engageGetCurrentContexts().join(',');
        const next = window.prompt(
            'Comma-separated tokens (e.g. @filesystem,@vault):', cur,
        );
        if (next != null) {
            const arr = next.split(',').map(s => s.trim()).filter(Boolean);
            localStorage.setItem('engage.custom_contexts', JSON.stringify(arr));
        }
    }
    loadEngage();
}

async function loadEngage() {
    const root = document.getElementById('engage-items');
    const summary = document.getElementById('engage-summary');
    const ctxBox = document.getElementById('engage-current-contexts');
    if (!root) return;

    // Hydrate the preset dropdown from localStorage on first paint.
    const sel = document.getElementById('engage-context-preset');
    if (sel && !sel.dataset.hydrated) {
        const saved = localStorage.getItem('engage.preset');
        if (saved && Array.from(sel.options).some(o => o.value === saved)) {
            sel.value = saved;
        }
        sel.dataset.hydrated = '1';
    }

    const current = _engageGetCurrentContexts();
    if (ctxBox) {
        ctxBox.innerHTML = current.length
            ? '<span class="engage-current-label">Current contexts:</span> '
              + current.map(t =>
                  '<span class="engage-token">' + _autEsc(t) + '</span>'
              ).join(' ')
            : '<span class="engage-current-empty">No current contexts declared.</span>';
    }

    root.innerHTML = '<div class="loading">Loading engage view...</div>';
    if (summary) summary.textContent = '';

    let data;
    try {
        const url = '/api/automation/engage?contexts='
            + encodeURIComponent(current.join(','));
        const resp = await fetch(url);
        data = await resp.json();
    } catch (e) {
        root.innerHTML = '<div class="empty-state">Failed to load engage view: '
            + _autEsc(String(e)) + '</div>';
        return;
    }
    if (!data || data.status !== 'ok') {
        root.innerHTML = '<div class="empty-state">Engage view load failed'
            + (data && data.error ? ': ' + _autEsc(data.error) : '')
            + '</div>';
        return;
    }

    const hideBlocked = !!(document.getElementById('engage-hide-blocked')
        && document.getElementById('engage-hide-blocked').checked);
    const items = (data.items || []).filter(it => {
        if (!hideBlocked) return true;
        const w = it.who_can_act || {};
        const u = it.user_now || {};
        return w.agent && u.satisfied;
    });

    if (!items.length) {
        root.innerHTML = '<div class="empty-state">'
            + (hideBlocked
               ? 'No actionable tasks for the current context. Disable "Hide blocked" to see what is waiting on setup or context.'
               : 'No tasks. Capture something first or check the Tasks tab.')
            + '</div>';
        if (summary) summary.textContent = '';
        return;
    }

    if (summary) {
        const blockedCount = (data.items || []).filter(
            it => !((it.who_can_act || {}).agent && (it.user_now || {}).satisfied)
        ).length;
        summary.textContent = items.length + ' shown'
            + (blockedCount ? ' · ' + blockedCount + ' blocked or context-unmet' : '');
    }

    root.innerHTML = '';
    const grid = document.createElement('div');
    grid.className = 'aut-rq-grid';
    items.forEach(it => grid.appendChild(_engageBuildCard(it)));
    root.appendChild(grid);
}

function _engageBuildCard(item) {
    const card = document.createElement('div');
    card.className = 'aut-rq-card engage-card';
    card.dataset.taskId = item.task_id || '';

    const header = document.createElement('div');
    header.className = 'aut-rq-header';
    const title = document.createElement('div');
    title.className = 'aut-rq-title';
    title.textContent = item.text || item.task_id || '(untitled)';
    header.appendChild(title);

    const meta = document.createElement('div');
    meta.className = 'aut-rq-meta';
    const tier = (item.auto && item.auto.operating != null) ? item.auto.operating : '?';
    const tierBadge = document.createElement('span');
    tierBadge.className = 'aut-tier-badge tier-' + tier;
    tierBadge.title = 'Operating tier (achievable=' + (item.auto && item.auto.achievable) + ')';
    tierBadge.textContent = 'tier ' + tier;
    meta.appendChild(tierBadge);

    const w = item.who_can_act || {};
    if (w.agent_handoff_eligible) {
        const handoff = document.createElement('span');
        handoff.className = 'engage-handoff-badge';
        handoff.title = 'Agent prepared what it can; you take from here.';
        handoff.textContent = '↪ handoff';
        meta.appendChild(handoff);
    }
    if (item.auto && item.auto.last_actor) {
        const actor = document.createElement('span');
        actor.className = 'aut-actor-badge actor-' + item.auto.last_actor;
        actor.textContent = item.auto.last_actor;
        actor.title = 'Last actor';
        meta.appendChild(actor);
    }
    header.appendChild(meta);
    card.appendChild(header);

    // Required-context badges row (always shown when populated)
    if ((w.agent_required_contexts || []).length
        || (w.user_required_contexts || []).length) {
        const ctx = document.createElement('div');
        ctx.className = 'engage-context-row';
        const renderSide = (label, tokens, unmet) => {
            if (!tokens.length) return '';
            const unmetSet = new Set(unmet || []);
            const chips = tokens.map(t => {
                const cls = 'engage-token' + (unmetSet.has(t) ? ' unmet' : ' met');
                const tip = unmetSet.has(t) ? ' title="Not satisfied"' : ' title="Satisfied"';
                return '<span class="' + cls + '"' + tip + '>' + _autEsc(t) + '</span>';
            }).join(' ');
            return '<span class="engage-side-label">' + label + ':</span> ' + chips;
        };
        ctx.innerHTML = [
            renderSide('agent', w.agent_required_contexts || [], w.agent_unmet || []),
            renderSide('user',  w.user_required_contexts || [],
                       (item.user_now && item.user_now.unmet) || w.user_unmet || []),
        ].filter(Boolean).join(' &nbsp;·&nbsp; ');
        card.appendChild(ctx);
    }

    // Pipeline blocker badge (typed reason)
    const blk = item.auto && item.auto.pipeline_blocker;
    if (blk) {
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

    const footer = document.createElement('div');
    footer.className = 'aut-rq-footer';
    const bits = [];
    if (item.contract) bits.push('contract: ' + _autEsc(item.contract));
    if (item.state) bits.push('state: ' + _autEsc(item.state));
    if (w.source) bits.push('contexts: ' + _autEsc(w.source));
    footer.innerHTML = bits.join(' · ');
    card.appendChild(footer);

    return card;
}

async function loadBlockedByContext() {
    const host = document.getElementById('daily-log-blocked-by-context');
    if (!host) return;
    let data;
    try {
        const resp = await fetch('/api/automation/blocked-by-context');
        data = await resp.json();
    } catch (e) { host.innerHTML = ''; return; }
    if (!data || data.status !== 'ok' || !(data.items || []).length) {
        host.innerHTML = '';
        return;
    }
    const lines = data.items.map(it => {
        const tip = (it.task_ids || []).slice(0, 5).join(', ');
        const link = it.setup_link
            ? ' <a href="' + _autEsc(it.setup_link) + '" class="wv-blocker-link">Set up</a>'
            : '';
        return '<li title="' + _autEsc(tip) + '">'
            + '<span class="engage-token unmet">' + _autEsc(it.context) + '</span> — '
            + '<strong>' + it.count + '</strong> task'
            + (it.count === 1 ? '' : 's') + ' blocked'
            + (it.tool_ids.length
                ? ' (needs ' + it.tool_ids.map(_autEsc).join(', ') + ')'
                : '')
            + link
            + '</li>';
    }).join('');
    host.innerHTML = '<div class="blocked-by-context-card">'
        + '<div class="blocked-by-context-title">'
        + 'Blocked on missing contexts'
        + '</div>'
        + '<ul class="blocked-by-context-list">' + lines + '</ul>'
        + '</div>';
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

/* Slice 5a — Engage view + blocked-by-context nudge */

.engage-toggle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: var(--text-muted, #555);
    margin-left: 8px;
}
.engage-current-contexts {
    font-size: 12px;
    color: var(--text-muted, #555);
    padding: 6px 10px;
    margin-bottom: 8px;
    background: var(--bg-tertiary, #f5f5f5);
    border-radius: 4px;
}
.engage-current-label { font-weight: 500; margin-right: 6px; }
.engage-current-empty { font-style: italic; color: var(--text-muted, #888); }
.engage-token {
    display: inline-block;
    padding: 1px 7px;
    margin: 0 2px;
    border-radius: 10px;
    font-family: monospace;
    font-size: 11px;
    background: var(--bg-secondary, #eef);
    color: var(--text-primary, #225);
    border: 1px solid var(--border-muted, #dde);
}
.engage-token.met {
    background: #e3f4d4;
    color: #2d5a2d;
    border-color: #b9d8a3;
}
.engage-token.unmet {
    background: #fde2e2;
    color: #8a1f1f;
    border-color: #e2a3a3;
}
.engage-context-row {
    margin: 6px 0;
    font-size: 11px;
    color: var(--text-muted, #666);
}
.engage-side-label {
    font-weight: 500;
    color: var(--text-muted, #555);
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.5px;
    margin-right: 4px;
}
.engage-handoff-badge {
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 10px;
    background: #fff4d6;
    color: #6a4b00;
    cursor: help;
}
.blocked-by-context {
    margin-bottom: 8px;
}
.blocked-by-context-card {
    padding: 8px 12px;
    background: #fff8e1;
    border: 1px solid #f4e4a3;
    border-radius: 6px;
}
.blocked-by-context-title {
    font-weight: 500;
    margin-bottom: 4px;
    color: #6a4b00;
}
.blocked-by-context-list {
    margin: 4px 0 0 16px;
    padding: 0;
    font-size: 12px;
    color: var(--text-primary, #222);
}
.blocked-by-context-list li {
    line-height: 1.6;
}
"""
