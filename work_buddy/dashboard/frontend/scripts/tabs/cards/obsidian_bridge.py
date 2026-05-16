"""Card renderer: Obsidian bridge latency sparkline.

Registers ``window.wbCardRenderers['obsidian.bridge_sparkline']``. The
server-side descriptor (``work_buddy/dashboard/cards.py``) gates this
card on the ``obsidian`` component, so it is absent from the mounted set
whenever Obsidian is opted out — and ``/api/state`` then reports
``bridge: null`` anyway.

Reads ``state.bridge`` (probe status, latency stats, rolling history).
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Card: Obsidian bridge sparkline ----
window.wbCardRenderers['obsidian.bridge_sparkline'] = function(state) {
    const b = (state && state.bridge) || {};
    if (!b.status) {
        return '<div id="activity-bridge">'
            + '<div class="empty-state">Bridge status unavailable</div></div>';
    }
    const dotClass = b.status === 'healthy'
        ? 'healthy'
        : (b.status === 'timeout' ? 'unhealthy' : 'crashed');
    const statusLabel = b.status === 'healthy'
        ? 'connected'
        : (b.status === 'unreachable'
            ? 'Obsidian not running'
            : b.status === 'timeout'
                ? 'bridge lagging'
                : b.status);
    const latencyColor = (b.latency_ms || 0) > 2000
        ? 'var(--red)'
        : (b.latency_ms || 0) > 500 ? 'var(--yellow)' : 'var(--text-primary)';

    // Log-scale sparkline, normalized so the tallest bar fills 100%.
    // Bar classes: bar-ok (<=500ms), bar-slow (>500ms), bar-fail
    // (timeout), bar-unreachable (port closed / Obsidian not running).
    const hist = b.history || [];
    const logMax = Math.max(1, ...hist.map(h => Math.log10(Math.max(1, h.ms))));
    const bars = hist.map(h => {
        const logMs = Math.log10(Math.max(1, h.ms));
        const pct = Math.max(8, (logMs / logMax) * 100);
        let cls;
        if (h.ok) {
            cls = h.ms > 500 ? 'bar-slow' : 'bar-ok';
        } else if (h.status === 'unreachable') {
            cls = 'bar-unreachable';
        } else {
            cls = 'bar-fail';
        }
        const dt = new Date(h.ts * 1000);
        const label = h.ok
            ? (h.ms + 'ms')
            : (h.status === 'unreachable'
                ? 'Obsidian closed (port refused)'
                : 'Obsidian lag/timeout (' + h.ms + 'ms)');
        const tip = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) + ' — ' + label;
        return `<div class="bar ${cls}" style="height:${pct}%" title="${tip}"></div>`;
    }).join('');

    return `
        <div id="activity-bridge">
            <div class="bridge-card">
                <div class="bridge-header">
                    <h3><span class="status-dot ${dotClass}"></span> Obsidian Bridge — ${statusLabel}</h3>
                    <div class="bridge-stats">
                        <div>Latency <span class="bridge-stat-value" style="color:${latencyColor}">${b.latency_ms}ms</span></div>
                        <div>Trend <span class="bridge-stat-value">${b.ema_ms || 0}ms</span>${(() => {
                            const tc = (b.ema_ms||0) > 2000 ? 'var(--red)' : (b.ema_ms||0) > 500 ? 'var(--yellow)' : 'var(--text-primary)';
                            const arrow = b.trend === 'up' ? '▲' : b.trend === 'down' ? '▼' : '◆';
                            const tip = b.trend === 'up' ? 'Latency increasing' : b.trend === 'down' ? 'Latency decreasing' : 'Latency stable';
                            return ` <span style="color:${tc}" title="${tip}">${arrow}</span>`;
                        })()}</div>
                        <div>Peak <span class="bridge-stat-value">${b.max_ms || 0}ms</span></div>
                    </div>
                    <div class="bridge-meta">${b.vault || ''} ${b.plugin_version ? 'v' + b.plugin_version : ''}</div>
                </div>
                ${bars ? `<div class="bridge-sparkline">${bars}</div>` : ''}
            </div>
        </div>
    `;
};
"""
