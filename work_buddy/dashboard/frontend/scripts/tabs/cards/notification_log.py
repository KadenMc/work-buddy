"""Card renderer: recent-notifications log.

Registers ``window.wbCardRenderers['core.notification_log']``. Ungated.

This renderer is asynchronous — it fetches ``/api/notification-log``
and returns a Promise of the rendered HTML. ``wbMountCards`` awaits it,
so the card is fully rendered before the morphdom merge.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Card: recent-notifications log ----
window.wbCardRenderers['core.notification_log'] = async function(state) {
    const header = '<div class="section-title" style="margin-top: 24px;">Recent Notifications</div>';
    let body;
    try {
        const logData = await fetchJSON('/api/notification-log');
        if (!logData || !logData.entries || logData.entries.length === 0) {
            body = '<div id="activity-notif-log"><div class="empty-state">No notifications yet</div></div>';
        } else {
            const logRows = logData.entries.map(e => {
                const dt = new Date(e.ts * 1000);
                const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
                const isReq = e.type === 'request';
                const pill = isReq
                    ? '<span class="type-pill request1" style="font-size:9px;padding:1px 6px">REQUEST</span>'
                    : '<span class="type-pill note1" style="font-size:9px;padding:1px 6px">NOTE</span>';
                const sid = e.short_id ? ' <code>#' + e.short_id + '</code>' : '';
                const surfaces = (e.surfaces || []).join(', ') || '—';
                return '<tr>'
                    + '<td style="white-space:nowrap;color:var(--text-muted)">' + time + '</td>'
                    + '<td>' + pill + '</td>'
                    + '<td>' + (e.title || '') + sid + '</td>'
                    + '<td style="color:var(--text-muted)">' + surfaces + '</td>'
                    + '</tr>';
            }).join('');
            body = `<div id="activity-notif-log">
                <table class="data-table">
                    <thead><tr><th>Time</th><th>Type</th><th>Title</th><th>Surfaces</th></tr></thead>
                    <tbody>${logRows}</tbody>
                </table>
            </div>`;
        }
    } catch (e) {
        body = '<div id="activity-notif-log"><div class="empty-state">No notifications yet</div></div>';
    }
    return header + body;
};
"""
