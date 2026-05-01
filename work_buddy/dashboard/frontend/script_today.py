"""Slice 5b dashboard JS: Today tab.

The Today tab is the re-runnable engage surface — fetches
``/api/automation/today``, renders:

* A current-time banner with the work-hour bounds.
* An active-contract / WIP banner (if any constraints).
* The top 1-2 recommendation cards (computed server-side via
  ``work_buddy.task_me.top_recommendations``).
* The clamp-to-now time-blocked plan.

Shares the engage-view's localStorage preset key so the user's
context choice persists across both surfaces.
"""

from __future__ import annotations


def _today_script() -> str:
    return r"""
const TODAY_PRESETS = {
    at_desk: ['@filesystem', '@vault', '@web_public', '@user_workstation',
              '@user_creds', '@email_send', '@email_read', '@github',
              '@chrome_active', '@llm'],
    phone_only: ['@phone_voice', '@web_public', '@user_creds'],
    untethered: ['@physical', '@in_person', '@phone_voice'],
};

function _todayGetCurrentContexts() {
    // Reuse the Engage tab's localStorage so the preset choice is
    // shared across both surfaces.  Falls back to the same default.
    const sel = document.getElementById('today-context-preset');
    let preset = (sel && sel.value) || localStorage.getItem('engage.preset')
        || 'at_desk';
    if (sel && sel.value !== preset) sel.value = preset;
    if (preset === 'custom') {
        const stored = localStorage.getItem('engage.custom_contexts');
        if (stored) {
            try { return JSON.parse(stored) || []; }
            catch (e) { return []; }
        }
        return [];
    }
    return TODAY_PRESETS[preset] || TODAY_PRESETS.at_desk;
}

function onTodayContextPresetChange() {
    const sel = document.getElementById('today-context-preset');
    const preset = (sel && sel.value) || 'at_desk';
    localStorage.setItem('engage.preset', preset);
    if (preset === 'custom') {
        const cur = _todayGetCurrentContexts().join(',');
        const next = window.prompt(
            'Comma-separated tokens (e.g. @filesystem,@vault):', cur,
        );
        if (next != null) {
            const arr = next.split(',').map(s => s.trim()).filter(Boolean);
            localStorage.setItem('engage.custom_contexts', JSON.stringify(arr));
        }
    }
    loadToday();
}

async function loadToday() {
    const banner = document.getElementById('today-now-banner');
    const contracts = document.getElementById('today-contracts-banner');
    const recs = document.getElementById('today-recommendations');
    const plan = document.getElementById('today-plan');
    if (!plan) return;

    // Hydrate the preset dropdown if not yet wired.
    const sel = document.getElementById('today-context-preset');
    if (sel && !sel.dataset.hydrated) {
        const saved = localStorage.getItem('engage.preset');
        if (saved && Array.from(sel.options).some(o => o.value === saved)) {
            sel.value = saved;
        }
        sel.dataset.hydrated = '1';
    }

    plan.innerHTML = '<div class="loading">Loading...</div>';
    if (recs) recs.innerHTML = '';
    if (banner) banner.innerHTML = '';
    if (contracts) contracts.innerHTML = '';

    const current = _todayGetCurrentContexts();

    let data;
    try {
        const url = '/api/automation/today?contexts='
            + encodeURIComponent(current.join(','));
        const resp = await fetch(url);
        data = await resp.json();
    } catch (e) {
        plan.innerHTML = '<div class="empty-state">Failed to load Today: '
            + _autEsc(String(e)) + '</div>';
        return;
    }
    if (!data || (data.status !== 'ok' && data.status !== 'degraded')) {
        plan.innerHTML = '<div class="empty-state">Today load failed'
            + (data && data.error ? ': ' + _autEsc(data.error) : '')
            + '</div>';
        return;
    }

    // Now-banner
    if (banner) {
        const wh = data.work_hours || [9, 17];
        banner.innerHTML =
            '<span class="today-now-time">' + _autEsc(data.now.local_hhmm) + '</span>'
            + ' <span class="today-now-bounds">work hours '
            + wh[0] + ':00–' + wh[1] + ':00</span>'
            + (data.status === 'degraded'
               ? ' <span class="today-degraded">⚠ partial context</span>'
               : '');
    }

    // Contract banner
    if (contracts) {
        const active = data.active_contracts || [];
        const constraints = data.contract_constraints || [];
        if (active.length || constraints.length) {
            const lines = [];
            if (active.length) {
                lines.push('<strong>Active contracts (' + active.length + '):</strong> '
                    + active.slice(0, 3).map(c =>
                        '<span class="today-contract">'
                        + _autEsc(c.title || c.slug || 'untitled')
                        + '</span>'
                    ).join(' · '));
            }
            if (constraints.length) {
                const cstr = constraints.slice(0, 2).map(c =>
                    _autEsc(c.text || c.message || c.label || '')
                ).filter(Boolean).join(' · ');
                if (cstr) lines.push('<em>' + cstr + '</em>');
            }
            contracts.innerHTML = lines.join(' &nbsp;|&nbsp; ');
        } else {
            contracts.innerHTML = '<em class="today-no-contract">'
                + 'No active contracts. Per CLAUDE.local.md: exploration mode.'
                + '</em>';
        }
    }

    // Recommendations (top 1-2)
    if (recs) {
        const items = data.recommendations || [];
        if (!items.length) {
            recs.innerHTML = '<div class="empty-state">'
                + 'Nothing actionable in current context. '
                + 'Try a different context preset above, or check the Engage tab '
                + 'to see what is blocked and why.'
                + '</div>';
        } else {
            recs.innerHTML = items.map((it, i) =>
                _todayBuildRecCard(it, i + 1)).join('');
        }
    }

    // Plan
    const planEntries = data.plan || [];
    if (!planEntries.length) {
        plan.innerHTML = '<div class="empty-state">'
            + (data.focused_count ? 'Plan generated empty — work window may be over.'
                                  : 'No focused tasks to schedule. Promote one from the Tasks tab.')
            + '</div>';
    } else {
        const rows = planEntries.map(p => {
            const time = p.time_start
                ? _autEsc(p.time_start) + (p.time_end ? '–' + _autEsc(p.time_end) : '')
                : '<em>unscheduled</em>';
            const cls = p.is_calendar ? 'today-plan-row cal' : 'today-plan-row task';
            return '<li class="' + cls + '">'
                + '<span class="today-plan-time">' + time + '</span>'
                + '<span class="today-plan-text">' + _autEsc(p.text || '') + '</span>'
                + '</li>';
        }).join('');
        plan.innerHTML = '<ul class="today-plan-list">' + rows + '</ul>';
    }
}

function _todayBuildRecCard(item, rank) {
    const w = item.who_can_act || {};
    const handoff = w.agent_handoff_eligible
        ? '<span class="engage-handoff-badge">↪ handoff</span>'
        : '';
    const tier = (item.auto && item.auto.operating != null) ? item.auto.operating : '?';
    const tierBadge = '<span class="aut-tier-badge tier-' + tier + '">tier '
        + tier + '</span>';
    const contract = item.contract
        ? '<span class="today-contract-pill">contract: '
          + _autEsc(item.contract) + '</span>'
        : '<span class="today-exploration-pill">exploration</span>';
    return '<div class="today-rec-card rank-' + rank + '">'
        + '<div class="today-rec-header">'
        + '<span class="today-rec-rank">#' + rank + '</span>'
        + '<span class="today-rec-text">' + _autEsc(item.text || item.task_id) + '</span>'
        + tierBadge + handoff
        + '</div>'
        + '<div class="today-rec-meta">'
        + contract
        + (item.state ? ' · state ' + _autEsc(item.state) : '')
        + (item.urgency && item.urgency !== 'medium'
            ? ' · urgency ' + _autEsc(item.urgency) : '')
        + '</div>'
        + '</div>';
}
"""


def _today_styles() -> str:
    return r"""
/* Slice 5b — Today tab */

.today-now-banner {
    padding: 8px 12px;
    background: var(--bg-secondary, #fafafa);
    border-radius: 6px;
    margin-bottom: 8px;
    font-size: 13px;
}
.today-now-time {
    font-family: monospace;
    font-size: 16px;
    font-weight: 600;
    color: var(--accent, #4a6fa5);
}
.today-now-bounds {
    color: var(--text-muted, #666);
    margin-left: 8px;
}
.today-degraded {
    margin-left: 12px;
    color: #8a4b00;
    font-size: 12px;
}
.today-contracts-banner {
    padding: 6px 12px;
    background: #f4f9ff;
    border: 1px solid #d6e6ff;
    border-radius: 6px;
    margin-bottom: 8px;
    font-size: 12px;
    color: var(--text-primary, #222);
}
.today-no-contract {
    color: var(--text-muted, #888);
}
.today-contract {
    display: inline-block;
    padding: 1px 7px;
    background: var(--bg-tertiary, #eef);
    border-radius: 10px;
    font-size: 11px;
}
.today-recommendations {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 8px;
}
.today-rec-card {
    border: 1px solid var(--border-muted, #ddd);
    border-left: 4px solid var(--accent, #4a6fa5);
    background: var(--bg-secondary, #fafafa);
    border-radius: 6px;
    padding: 10px 14px;
}
.today-rec-card.rank-1 { border-left-color: #2d6a3a; }
.today-rec-card.rank-2 { border-left-color: #6a4b00; }
.today-rec-header {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin-bottom: 4px;
}
.today-rec-rank {
    font-size: 13px;
    font-weight: 700;
    color: var(--text-muted, #555);
}
.today-rec-text {
    flex: 1 1 auto;
    font-size: 14px;
    font-weight: 500;
    color: var(--text-primary, #222);
}
.today-rec-meta {
    font-size: 11px;
    color: var(--text-muted, #666);
}
.today-contract-pill {
    display: inline-block;
    padding: 1px 7px;
    background: #e2ecfd;
    color: #1f3f8a;
    border-radius: 10px;
    font-size: 11px;
}
.today-exploration-pill {
    display: inline-block;
    padding: 1px 7px;
    background: #fff4d6;
    color: #6a4b00;
    border-radius: 10px;
    font-size: 11px;
    font-style: italic;
}
.today-plan-list {
    list-style: none;
    margin: 0;
    padding: 0;
    border: 1px solid var(--border-muted, #ddd);
    border-radius: 6px;
    overflow: hidden;
}
.today-plan-row {
    display: flex;
    gap: 12px;
    padding: 6px 12px;
    border-bottom: 1px solid var(--border-muted, #eee);
    font-size: 12px;
}
.today-plan-row:last-child { border-bottom: none; }
.today-plan-row.cal {
    background: #faf6e4;
}
.today-plan-time {
    flex: 0 0 100px;
    font-family: monospace;
    color: var(--text-muted, #666);
}
.today-plan-text {
    flex: 1 1 auto;
    color: var(--text-primary, #222);
}
"""
