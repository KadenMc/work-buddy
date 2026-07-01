"""Dashboard Settings › Inference sub-view JS.

A cross-provider **inference-activity feed**: every model call work-buddy makes —
local (LM Studio) and cloud (Anthropic / Google), completions and embeddings —
newest first, each with an authored-ish *Purpose*, a local/cloud badge, model,
usage, status, and end-to-end latency. Sourced from the per-session
``inference_calls`` provenance logs (``GET /api/inference-activity``), joined with
broker scheduler latency by ``call_id`` (surfaced on hover for local rows).
Live-updates via the ``inference.call_logged`` SSE event → morphdom merge (no
frontend timer). Escalation chains (a tier failed, the next was tried) are marked
with a chain icon and share a trace id.

Per-machine occupancy / loaded-model state ("what's running on which box") is out
of scope for this provenance feed; it belongs to the separate **Local model
fleet** view (LM Studio / LM Link integration). The where/kind/status filter rail
is the shared ``wbRenderFilters`` widget (``core/filters.py``) in multi-select mode.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Inference activity (Settings sub-view) ----

// Multi-select filter sets (empty = show all), preserved across re-renders.
let _infWhere = new Set();      // 'local' | 'cloud'
let _infKind = new Set();       // 'completion' | 'embedding'
let _infStatusSet = new Set();  // 'ok' | 'errored' | 'queued'
let _infHelpOpen = false;

// Inline Lucide-style icons (the dashboard inlines Lucide SVGs; no JS lib).
const _INF_ICON_INFO = '<svg class="inf-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>';
const _INF_ICON_CHAIN = '<svg class="inf-ico" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path></svg>';

function _infMs(n) { return (n == null) ? '—' : Math.round(n).toLocaleString(); }

function _infAge(s) {
    if (s == null) return '—';
    if (s < 60) return Math.round(s) + 's';
    if (s < 3600) return Math.round(s / 60) + 'm';
    if (s < 86400) return Math.round(s / 3600) + 'h';
    return Math.round(s / 86400) + 'd';  // persisted rows can be days old after a restart
}

function _infAgeIso(iso) {
    if (!iso) return '—';
    const t = Date.parse(iso);
    return isNaN(t) ? '—' : _infAge((Date.now() - t) / 1000);
}

// Status → badge class. ok=green, queued/running=accent, error=red, the
// caller-visible wait failures / timeouts = orange(warn).
function _infStatusBadge(status) {
    let cls = 'idx-warn';
    if (status === 'ok') cls = 'idx-ok';
    else if (status === 'cached') cls = 'idx-muted-badge';
    else if (status === 'queued' || status === 'running') cls = 'inf-st-run';
    else if (status === 'error') cls = 'idx-err';
    return `<span class="idx-badge ${cls}">${escapeHtml(status || '—')}</span>`;
}

function _infStatusGroup(status) {
    if (status === 'ok' || status === 'cached') return 'ok';
    if (status === 'queued' || status === 'running') return 'queued';
    return 'errored';
}

function _infModeBadge(mode) {
    const cls = mode === 'local' ? 'inf-mode-local' : 'inf-mode-cloud';
    return `<span class="idx-badge ${cls}">${escapeHtml(mode || '—')}</span>`;
}

function _infUsage(c) {
    if (c.kind === 'embedding') return (c.item_count != null) ? `${c.item_count} docs` : '—';
    const i = c.input_tokens, o = c.output_tokens;
    if (i == null && o == null) return '—';
    return `${i || 0} in · ${o || 0} out`;
}

const _INF_HELP_HTML = `
<div class="inf-help">
    <div><b>What this is</b> — every model call work-buddy makes, newest first: local
        (LM Studio) and cloud (Anthropic / Google), completions and embeddings. Persisted
        ~7 days, so it survives restarts.</div>
    <div><b>Purpose</b> — where the call came from (the call site) plus a detail when one's
        readily available (a tab title, the IR source being embedded, …).</div>
    <div><b>Latency</b> — end-to-end time for the call. Hover a local row for the
        queue-wait vs model-service split.</div>
    <div><b>${_INF_ICON_CHAIN} Escalation</b> — marks calls in an escalation chain (a tier failed and the
        next was tried); chained attempts share a trace id.</div>
</div>`;

function _infToggleHelp() { _infHelpOpen = !_infHelpOpen; _infRender(); }

async function loadInference() {
    const container = document.getElementById('inference-content');
    if (!container) return;
    window._infActivity = await fetchJSON('/api/inference-activity');
    _infRender();
}

function _infRender() {
    const container = document.getElementById('inference-content');
    if (!container) return;
    window._wbMorphReplace(container, _infRenderHeader() + _infRenderActivity(window._infActivity));
    // The filter rail lives in a wb-filters container inside the morphed blob;
    // refill it after the diff (selection is re-derived from the _inf* Sets, so
    // it survives the refresh). No-op when the feed is empty (no container).
    wbRenderFilters('inf-filters', _infFilterConfig());
}

// Caller-owned selection: each group maps to one of the multi-select Sets.
function _infFilterConfig() {
    return {
        id: 'inf-filters',
        mode: 'multi',
        variant: 'chips',
        groups: [
            { key: 'where',  label: 'where',  options: [{ value: 'local' }, { value: 'cloud' }] },
            { key: 'kind',   label: 'kind',   options: [{ value: 'completion' }, { value: 'embedding' }] },
            { key: 'status', label: 'status', options: [{ value: 'ok' }, { value: 'errored' }, { value: 'queued' }] },
        ],
        getSelected: '_infGetSelected',
        onChange: '_infOnChange',
    };
}
function _infGetSelected(key) {
    if (key === 'where')  return _infWhere;
    if (key === 'kind')   return _infKind;
    if (key === 'status') return _infStatusSet;
    return new Set();
}
function _infOnChange(key, nextSet) {
    if (key === 'where')       _infWhere = nextSet;
    else if (key === 'kind')   _infKind = nextSet;
    else if (key === 'status') _infStatusSet = nextSet;
    _infRender();
}

function _infRenderHeader() {
    return `
        <div class="inf-header">
            <div class="inf-title-row">
                <span class="emb-section-title">Inference activity</span>
                <button class="inf-help-btn" title="What am I looking at?" ` + wbActAttrs('infToggleHelp', {}) + `>?</button>
            </div>
            <div class="idx-muted inf-intro">Every model call across work-buddy — local and cloud,
                completions and embeddings — newest first.</div>
            ${_infHelpOpen ? _INF_HELP_HTML : ''}
        </div>`;
}

function _infRenderActivity(act) {
    const all = (act && act.calls) || [];
    if (!all.length) {
        return `<div class="empty-state">No inference calls recorded yet — they appear here as work-buddy uses local or cloud models.</div>`;
    }

    // trace_ids seen more than once are escalation chains; their rows get a marker.
    const traceCounts = {};
    all.forEach(c => { if (c.trace_id) traceCounts[c.trace_id] = (traceCounts[c.trace_id] || 0) + 1; });

    const rows = all.filter(c =>
        (_infWhere.size === 0 || _infWhere.has(c.execution_mode))
        && (_infKind.size === 0 || _infKind.has(c.kind))
        && (_infStatusSet.size === 0 || _infStatusSet.has(_infStatusGroup(c.status))));

    // Shared filter rail (window.wbRenderFilters); filled by _infRender after
    // the morphdom diff. Empty container here keeps the markup serializable.
    const filterBar = `<div class="wb-filters" id="inf-filters"></div>`;

    const body = rows.length ? rows.map(c => {
        const chain = (c.trace_id && traceCounts[c.trace_id] > 1)
            ? ` <span class="inf-chain" title="escalation chain (trace ${escapeHtml(c.trace_id)})">${_INF_ICON_CHAIN}</span>` : '';
        const errInfo = c.error ? ` <span class="inf-info" title="${escapeHtml(c.error)}">${_INF_ICON_INFO}</span>` : '';
        // One end-to-end latency for every provider; broker splits (local only) on hover.
        const lat = (c.latency_ms != null) ? c.latency_ms : c.service_time_ms;
        const split = (c.queue_wait_ms != null || c.service_time_ms != null)
            ? `local scheduler — queue ${_infMs(c.queue_wait_ms)} ms · service ${_infMs(c.service_time_ms)} ms` : '';
        return `<tr>
            <td class="idx-muted" title="${escapeHtml(c.finished_at || '')}">${_infAgeIso(c.finished_at)}</td>
            <td class="inf-act-desc" title="${escapeHtml(c.description || '')}">${escapeHtml(c.description || c.call_site || '—')}${chain}</td>
            <td>${_infModeBadge(c.execution_mode)}</td>
            <td class="inf-act-model" title="${escapeHtml(c.model || '')}">${escapeHtml(c.model || '—')}</td>
            <td class="idx-r">${_infUsage(c)}</td>
            <td>${_infStatusBadge(c.status)}${errInfo}</td>
            <td class="idx-r"${split ? ` title="${split}"` : ''}>${_infMs(lat)}</td>
        </tr>`;
    }).join('') : `<tr><td colspan="7" class="idx-muted">no calls match the current filter</td></tr>`;

    return `
        <div class="inf-section">
            ${filterBar}
            <div class="inf-table-scroll">
                <table class="data-table">
                    <thead><tr>
                        <th title="time since the call finished">Age</th>
                        <th title="where the call came from + a detail when available">Purpose</th>
                        <th title="local (LM Studio) or cloud (Anthropic / Google)">Where</th>
                        <th>Model</th>
                        <th class="idx-r" title="completions: input · output tokens — embeddings: documents">Usage</th>
                        <th>Status</th>
                        <th class="idx-r" title="end-to-end time; hover a local row for the queue / service split">Latency (ms)</th>
                    </tr></thead>
                    <tbody>${body}</tbody>
                </table>
            </div>
        </div>`;
}

// Surface handle for the event-bus dispatcher (inference.call_logged → _refreshSoon).
window.inferenceSurface = {
    refresh: function() { return loadInference(); },
    isMounted: function() {
        return !!document.getElementById('inference-content')
            && (typeof WB_SETTINGS_SUBTAB === 'undefined' || WB_SETTINGS_SUBTAB === 'inference');
    },
};

// Event delegation adapter
window.wbAction('infToggleHelp', function (el) { _infToggleHelp(); });
"""


def styles() -> str:
    return """
/* Inference sub-view — reuses .idx-badge/.idx-ok|warn|err/.data-table/.idx-r/.idx-muted
   from the Embeddings sub-view; only inference-specific classes below. */
.inf-section { margin-bottom: 22px; }

/* Header: title + orientation + help */
.inf-header { margin-bottom: 16px; }
.inf-title-row { display:flex; align-items:center; gap:8px; }
.inf-help-btn { margin-left:auto; width:22px; height:22px; border-radius:50%; cursor:pointer;
    border:1px solid var(--text-muted); background:transparent; color: var(--text-secondary);
    font-weight:700; line-height:1; flex:0 0 auto; }
.inf-help-btn:hover { color: var(--text-primary); border-color: var(--text-primary); }
.inf-intro { margin-top:6px; font-size:0.85em; max-width:70ch; }
.inf-help { margin-top:10px; padding:12px 14px; border-radius:8px; background: var(--bg-tertiary);
    font-size:0.85em; color: var(--text-secondary); display:flex; flex-direction:column; gap:6px; }
.inf-help b { color: var(--text-primary); }

/* Status badges specific to this feed */
.inf-st-run { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }
.idx-muted-badge { background: color-mix(in srgb, var(--text-muted) 20%, transparent); color: var(--text-muted); }
.inf-mode-local { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
.inf-mode-cloud { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }

/* Filter rail spacing — the chips themselves are the shared wb-filter-* widget. */
.inf-section .wb-filters { margin-bottom: 8px; }

/* Recent-calls scroll viewport (mirrors the Activity Event Log's .log-container) */
.inf-table-scroll { max-height: 420px; overflow-y: auto; border:1px solid var(--border, var(--bg-tertiary)); border-radius:8px; }
.inf-table-scroll thead th { position: sticky; top: 0; background: var(--bg-secondary); z-index: 1; user-select: none; }
.inf-table-scroll thead th[title] { cursor: help; }
.inf-act-desc { font-weight:600; color: var(--text-primary); max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.inf-act-model { font-variant-numeric:tabular-nums; max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
/* Right-aligned columns: align the HEADER with its values (out-specifies .data-table th). */
.inf-table-scroll thead th.idx-r { text-align: right; }
.inf-chain { color: var(--accent); cursor: help; }
.inf-info { color: var(--text-muted); cursor: help; }
.inf-ico { vertical-align: -2px; }
"""
