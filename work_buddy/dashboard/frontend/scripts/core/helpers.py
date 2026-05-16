"""Dashboard shared helper JS — utilities used by every tab.

Tab-agnostic primitives only: HTTP fetch, status badges, time
formatters. Concatenated BEFORE every tab module so the helpers exist
in scope when tab loaders run.

Component-health UI is NOT here — the Settings tab's control graph
(``scripts/tabs/settings.py``) owns it.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Helpers ----
function statusBadge(status, tooltip) {
    const map = {
        healthy: 'badge-green', running: 'badge-green', active: 'badge-green', ok: 'badge-green',
        unhealthy: 'badge-yellow', stalled: 'badge-yellow', waiting: 'badge-yellow', degraded: 'badge-yellow',
        crashed: 'badge-red', blocked: 'badge-red', error: 'badge-red',
        stopped: 'badge-muted', done: 'badge-muted', unknown: 'badge-muted', disabled: 'badge-muted',
        focused: 'badge-blue', next: 'badge-blue',
        inbox: 'badge-purple', someday: 'badge-muted',
        consent_required: 'badge-yellow',
    };
    const tip = tooltip ? ` title="${escapeHtml(tooltip)}" style="cursor:help"` : '';
    return `<span class="badge ${map[status] || 'badge-muted'}"${tip}>${status}</span>`;
}

function formatUptime(seconds) {
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h + 'h ' + m + 'm';
}

function timeAgo(epoch) {
    if (!epoch) return '—';
    const diff = Math.floor(Date.now() / 1000 - epoch);
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
}

function formatTimestamp(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        const now = new Date();
        const time = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
        const sameDay = d.toDateString() === now.toDateString();
        if (sameDay) return 'Today ' + time;
        const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
        if (d.toDateString() === yesterday.toDateString()) return 'Yesterday ' + time;
        const month = d.toLocaleString('default', {month: 'short'});
        return month + ' ' + d.getDate() + ' ' + time;
    } catch (e) { return iso; }
}

function timeUntil(epoch) {
    if (!epoch) return '—';
    const diff = Math.floor(epoch - Date.now() / 1000);
    if (diff < 0) return 'overdue';
    if (diff < 60) return 'now';
    if (diff < 3600) return 'in ' + Math.floor(diff / 60) + 'm';
    if (diff < 86400) return 'in ' + Math.floor(diff / 3600) + 'h';
    return 'in ' + Math.floor(diff / 86400) + 'd';
}

async function fetchJSON(url, options) {
    // Accept either ``fetchJSON(url)`` (GET) or ``fetchJSON(url, {method, body, headers})``.
    // Previously silently dropped the options argument, which meant POST
    // callers sent plain GETs and Flask replied 405 — see the Review tab
    // approve path that was a no-op until this fix landed (2026-04-20).
    try {
        const r = await fetch(url, options);
        return await r.json();
    } catch (e) {
        console.error('Fetch failed:', url, e);
        return null;
    }
}
"""
