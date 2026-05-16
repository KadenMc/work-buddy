"""Card renderer: sidecar event log.

Registers ``window.wbCardRenderers['core.event_log']``. Ungated — the
event log shows system-wide sidecar events, not anything component-
scoped.

Reads ``state.events``. Stashes the rendered list on
``window._logActivityEvents`` for the ``copyActivityLog`` /
``investigateActivityEvent`` handlers, which remain defined in
``tabs/settings.py`` (they share ``_spawnInvestigate`` with the Status
sub-view's per-component event chips).
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Card: sidecar event log ----
window.wbCardRenderers['core.event_log'] = function(state) {
    const events = ((state && state.events) || []).slice().reverse();  // newest first
    window._logActivityEvents = events;

    const toolbar = `
        <div class="log-toolbar" style="margin-top: 24px;">
            <span class="section-title">Event Log</span>
            <button class="log-toolbar-btn" onclick="copyActivityLog()" title="Copy log to clipboard">Copy Log</button>
        </div>`;

    let body;
    if (events.length === 0) {
        body = '<div id="activity-log"><div class="empty-state">No events yet</div></div>';
    } else {
        const logHtml = events.map((e, i) => {
            const dt = new Date(e.ts * 1000);
            const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
            const kind = (e.kind || '').replace(/_/g, ' ');
            const level = e.level || 'info';
            const actions = (!WB_READ_ONLY_MODE && (level === 'error' || level === 'warn'))
                ? `<span class="log-actions"><button class="btn-investigate ${level}" onclick="investigateActivityEvent(${i}, this)" title="Spawn agent to investigate">Investigate</button></span>`
                : '';
            return `<div class="log-entry ${level}">
                <span class="log-ts">${time}</span>
                <span class="log-kind">${kind}</span>
                <span class="log-msg"><strong>${e.source}</strong> — ${e.summary}</span>
                ${actions}
            </div>`;
        }).join('');
        body = `<div id="activity-log"><div class="log-container">${logHtml}</div></div>`;
    }
    return toolbar + body;
};
"""
