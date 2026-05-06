"""Dashboard Overview tab JS — at-a-glance home panel.

Renders the at-a-glance state cards (status, scheduled jobs preview,
recent thread highlights). The Overview tab is the default landing
panel when no URL hash is present.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Overview ----
async function loadOverview() {
    const data = await fetchJSON('/api/state');
    if (!data) return;
    _readOnly = !!data.read_only;

    const sidecarEl = document.getElementById('sidecar-status');
    const roTag = _readOnly ? ' <span style="color:var(--warn);font-size:0.85em;opacity:0.8">(read-only)</span>' : '';
    if (data.status === 'running') {
        sidecarEl.innerHTML = '<span class="status-dot healthy"></span> sidecar running' + roTag;
    } else {
        sidecarEl.innerHTML = '<span class="status-dot stopped"></span> sidecar stopped' + roTag;
    }

    const services = Object.values(data.services || {});
    const healthy = services.filter(s => s.status === 'healthy').length;

    document.getElementById('overview-cards').innerHTML = `
        <div class="card">
            <div class="card-label">Uptime</div>
            <div class="card-value">${formatUptime(data.uptime_seconds || 0)}</div>
        </div>
        <div class="card">
            <div class="card-label">Services</div>
            <div class="card-value">${healthy}<span class="unit"> / ${services.length} healthy</span></div>
        </div>
        <div class="card">
            <div class="card-label">Jobs</div>
            <div class="card-value">${(data.jobs || []).length}</div>
        </div>
        <div class="card">
            <div class="card-label">Last Tick</div>
            <div class="card-value small">${timeAgo(data.last_tick_at)}</div>
        </div>
    `;

}
"""
