"""Dashboard Status tab JS — services and event log.

Owns the Status tab loader, the services-and-components health tree
(populated via the renderHealthTree helper from core/helpers.py), and
the recent-events / recent-actions log panes.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Status (services + event log) ----
async function loadStatus() {
    const data = await fetchJSON('/api/state');
    if (!data) return;

    // --- Obsidian bridge ---
    const b = data.bridge || {};
    const bridgeEl = document.getElementById('status-bridge');
    if (b.status) {
        const dotClass = b.status === 'healthy' ? 'healthy' : (b.status === 'timeout' ? 'unhealthy' : 'crashed');
        // Distinguish "unreachable" (port closed, Obsidian not running)
        // from "timeout" (port open but bridge hung). The api layer
        // already tags them separately; the header just surfaces it
        // with friendlier language.
        const statusLabel = b.status === 'healthy'
            ? 'connected'
            : (b.status === 'unreachable'
                ? 'Obsidian not running'
                : b.status === 'timeout'
                    ? 'bridge lagging'
                    : b.status);
        const latencyColor = (b.latency_ms || 0) > 2000 ? 'var(--red)' : (b.latency_ms || 0) > 500 ? 'var(--yellow)' : 'var(--text-primary)';

        // Log-scale sparkline, normalized so tallest bar fills 100%.
        //
        // Bar class has four possible values now:
        //   * bar-ok         → healthy probe, <=500ms
        //   * bar-slow       → healthy probe, >500ms (lag)
        //   * bar-fail       → unhealthy, timed out (Obsidian hung)
        //   * bar-unreachable → unhealthy, port closed (Obsidian not running)
        //
        // The unreachable bars render distinctly so a glance at the
        // graph tells the user "I closed Obsidian" vs "Obsidian's
        // lagging" — the previous collapsed bar-fail hid the distinction.
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
                cls = 'bar-fail';  // timeout / error / http-non-200
            }
            const dt = new Date(h.ts * 1000);
            const label = h.ok
                ? (h.ms + 'ms')
                : (h.status === 'unreachable'
                    ? 'Obsidian closed (port refused)'
                    : 'Obsidian lag/timeout (' + h.ms + 'ms)');
            const tip = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) + ' \u2014 ' + label;
            return `<div class="bar ${cls}" style="height:${pct}%" title="${tip}"></div>`;
        }).join('');

        bridgeEl.innerHTML = `
            <div class="bridge-card">
                <div class="bridge-header">
                    <h3><span class="status-dot ${dotClass}"></span> Obsidian Bridge \u2014 ${statusLabel}</h3>
                    <div class="bridge-stats">
                        <div>Latency <span class="bridge-stat-value" style="color:${latencyColor}">${b.latency_ms}ms</span></div>
                        <div>Trend <span class="bridge-stat-value">${b.ema_ms || 0}ms</span>${(() => {
                            const tc = (b.ema_ms||0) > 2000 ? 'var(--red)' : (b.ema_ms||0) > 500 ? 'var(--yellow)' : 'var(--text-primary)';
                            const arrow = b.trend === 'up' ? '\u25B2' : b.trend === 'down' ? '\u25BC' : '\u25C6';
                            const tip = b.trend === 'up' ? 'Latency increasing' : b.trend === 'down' ? 'Latency decreasing' : 'Latency stable';
                            return ` <span style="color:${tc}" title="${tip}">${arrow}</span>`;
                        })()}</div>
                        <div>Peak <span class="bridge-stat-value">${b.max_ms || 0}ms</span></div>
                    </div>
                    <div class="bridge-meta">${b.vault || ''} ${b.plugin_version ? 'v' + b.plugin_version : ''}</div>
                </div>
                ${bars ? `<div class="bridge-sparkline">${bars}</div>` : ''}
            </div>
        `;
    } else {
        bridgeEl.innerHTML = '<div class="empty-state">Bridge status unavailable</div>';
    }

    // --- Component health tree ---
    const health = data.health;
    const svcEl = document.getElementById('status-services');
    if (!health || !health.components || health.components.length === 0) {
        // Fallback to flat services table if health not available
        const services = Object.entries(data.services || {});
        if (services.length === 0) {
            svcEl.innerHTML = '<div class="empty-state">No services</div>';
        } else {
            let rows = services.map(([name, s]) => `
                <tr>
                    <td><strong>${name}</strong></td>
                    <td>${statusBadge(s.status)}</td>
                    <td>${s.port}</td>
                    <td>${s.pid || '\u2014'}</td>
                    <td>${s.crash_count || 0}</td>
                    <td>${timeAgo(s.last_check)}</td>
                </tr>
            `).join('');
            svcEl.innerHTML = `
                <table class="data-table">
                    <thead><tr><th>Service</th><th>Status</th><th>Port</th><th>PID</th><th>Crashes</th><th>Last Check</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            `;
        }
    } else {
        // Skip health tree re-render if a diagnostic panel is open —
        // re-rendering would destroy the panel and the user's context.
        if (_diagOpen) {
            // Just update the summary bar and badge statuses in-place
            // (full re-render deferred until diag panel is closed)
        } else {
            // Preserve expand/collapse state across re-renders
            const expanded = new Set();
            svcEl.querySelectorAll('.health-item:not(.collapsed)').forEach(el => {
                const id = el.dataset.component;
                if (id) expanded.add(id);
            });
            svcEl.innerHTML = renderHealthTree(health, data.requirements);
            // Restore: items start collapsed by default, expand the ones that were open
            if (expanded.size > 0) {
                svcEl.querySelectorAll('.health-item.collapsed').forEach(el => {
                    if (expanded.has(el.dataset.component)) {
                        el.classList.remove('collapsed');
                    }
                });
            }
        }
    }

    // --- Event log ---
    const events = (data.events || []).slice().reverse();  // newest first
    if (events.length === 0) {
        document.getElementById('status-log').innerHTML = '<div class="empty-state">No events yet</div>';
        return;
    }

    // Store for copy
    window._logEvents = events;

    const logHtml = events.map((e, i) => {
        const dt = new Date(e.ts * 1000);
        const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const kind = (e.kind || '').replace(/_/g, ' ');
        const level = e.level || 'info';
        const actions = (!_readOnly && (level === 'error' || level === 'warn'))
            ? `<span class="log-actions"><button class="btn-investigate ${level}" onclick="investigateEvent(${i})" title="Spawn agent to investigate">Investigate</button></span>`
            : '';
        return `<div class="log-entry ${level}">
            <span class="log-ts">${time}</span>
            <span class="log-kind">${kind}</span>
            <span class="log-msg"><strong>${e.source}</strong> \u2014 ${e.summary}</span>
            ${actions}
        </div>`;
    }).join('');

    document.getElementById('status-log').innerHTML = `<div class="log-container">${logHtml}</div>`;

    // --- Notification log ---
    try {
        const logData = await fetchJSON('/api/notification-log');
        const logEl = document.getElementById('status-notif-log');
        if (!logEl) return;
        if (!logData || !logData.entries || logData.entries.length === 0) {
            logEl.innerHTML = '<div class="empty-state">No notifications yet</div>';
        } else {
            const logRows = logData.entries.map(e => {
                const dt = new Date(e.ts * 1000);
                const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
                const isReq = e.type === 'request';
                const pill = isReq
                    ? '<span class="type-pill request1" style="font-size:9px;padding:1px 6px">REQUEST</span>'
                    : '<span class="type-pill note1" style="font-size:9px;padding:1px 6px">NOTE</span>';
                const sid = e.short_id ? ' <code>#' + e.short_id + '</code>' : '';
                const surfaces = (e.surfaces || []).join(', ') || '\u2014';
                return '<tr>'
                    + '<td style="white-space:nowrap;color:var(--text-muted)">' + time + '</td>'
                    + '<td>' + pill + '</td>'
                    + '<td>' + (e.title || '') + sid + '</td>'
                    + '<td style="color:var(--text-muted)">' + surfaces + '</td>'
                    + '</tr>';
            }).join('');
            logEl.innerHTML = `
                <table class="data-table">
                    <thead><tr><th>Time</th><th>Type</th><th>Title</th><th>Surfaces</th></tr></thead>
                    <tbody>${logRows}</tbody>
                </table>
            `;
        }
    } catch(e) { /* notification log optional */ }
}

// ---- Log actions ----
function copyLog() {
    const events = window._logEvents || [];
    if (!events.length) return;
    const text = events.map(e => {
        const dt = new Date(e.ts * 1000);
        const time = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const kind = (e.kind || '').replace(/_/g, ' ').padEnd(16);
        const level = (e.level || 'info').toUpperCase().padEnd(5);
        return `${time}  ${level}  ${kind}  ${e.source}: ${e.summary}`;
    }).join('\\n');
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.querySelector('.log-toolbar-btn');
        if (btn) { btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy Log', 1500); }
    });
}

async function investigateEvent(idx) {
    if (_readOnly) return;
    const e = (window._logEvents || [])[idx];
    if (!e) return;

    const btn = event.target;
    btn.textContent = 'Launching...';
    btn.disabled = true;

    try {
        const r = await fetch('/api/investigate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({event: e}),
        });
        const data = await r.json();
        if (data.success) {
            btn.textContent = 'Launched';
            btn.style.background = 'var(--green-subtle)';
            btn.style.borderColor = 'var(--green)';
            btn.style.color = 'var(--green)';
        } else {
            btn.textContent = data.error || 'Failed';
            btn.disabled = false;
        }
    } catch (err) {
        btn.textContent = 'Error';
        btn.disabled = false;
        console.error('Investigate failed:', err);
    }
}
"""
