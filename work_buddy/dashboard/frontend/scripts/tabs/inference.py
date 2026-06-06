"""Dashboard Settings › Inference sub-view JS.

Read-only observability for the ``LocalInferenceBroker`` (the embedding-service
process's view — the high-traffic local-inference path). The panel is designed to
answer one question at a glance — *is local inference healthy, and if not, where's
the bottleneck?* — and to teach its own vocabulary rather than dump raw counters:

* **Health verdict** — ✓ Healthy / ◐ Busy / ⚠ Contended, computed from slot
  occupancy + whether interactive work is waiting. This is the "see it, don't
  guess" signal the feature exists for.
* **Occupancy cards** — one per profile, rendered as a *state* (Idle / Active /
  Queued) with a slot gauge and any waiting work spelled out in words. Profile
  keys are humanized (``lmstudio:`` → "Embedding offload"); the raw key is a tooltip.
* **Latency** — p50/p95/p99 over the last 5 min, with the window↔table distinction
  spelled out; collapses to a single number (not three identical ones) when n ≤ 1.
* **Recent calls** — last 50, newest-first, units + split definitions in the
  header tooltips; filter chips appear only once there are enough rows to matter.
* **"?" help** — explains priority classes, slots/queue, and the latency splits.

Data via ``GET /api/broker``; liveness via the ``broker.state`` SSE ping →
``inferenceSurface.refresh`` → morphdom merge (no frontend timer). Reuses the
shared ``.idx-badge`` / ``.idx-ok|warn|err`` / ``.data-table`` / ``.idx-r`` /
``.idx-muted`` primitives from the Embeddings sub-view; inference-specific classes
are defined here. All latency cells are null-coalesced (in-flight / queue_full /
timeout rows carry null splits).
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Inference (Settings sub-view) ----

// View state, preserved across re-renders (SSE refresh re-runs _infRender).
let _infPriorityFilter = 'All';
let _infStatusFilter = 'All';
let _infHelpOpen = false;

// Profile-key prefix → human role. Keys look like "lmstudio:<model>" /
// "lmstudio_native:<model>" / "openai_compat:<model>".
const _INF_ROLE = {
    'lmstudio': 'Embedding offload',
    'lmstudio_native': 'LLM (native)',
    'openai_compat': 'LLM (OpenAI-compat)',
};

function _infMs(n) { return (n == null) ? '—' : Math.round(n).toLocaleString(); }

function _infAge(s) {
    if (s == null) return '—';
    if (s < 60) return Math.round(s) + 's';
    if (s < 3600) return Math.round(s / 60) + 'm';
    if (s < 86400) return Math.round(s / 3600) + 'h';
    return Math.round(s / 86400) + 'd';  // persisted rows can be days old after a restart
}

function _infRole(profileKey) {
    const key = profileKey || '';
    const i = key.indexOf(':');
    if (i < 0) return { role: key || '—', model: '' };
    const prefix = key.slice(0, i);
    return { role: _INF_ROLE[prefix] || prefix, model: key.slice(i + 1) };
}

function _infWaiting(w) {
    w = w || {};
    return (w.INTERACTIVE || 0) + (w.WORKFLOW || 0) + (w.BACKGROUND || 0);
}

function _infWaitWords(w) {
    w = w || {};
    const parts = [];
    if (w.INTERACTIVE) parts.push(`${w.INTERACTIVE} interactive`);
    if (w.WORKFLOW) parts.push(`${w.WORKFLOW} workflow`);
    if (w.BACKGROUND) parts.push(`${w.BACKGROUND} background`);
    return parts.length ? parts.join(', ') + ' waiting' : '';
}

// Status → badge class. ok=green, queued/running=accent, the caller-visible wait
// failures + inference_timeout=orange(warn), error=red.
function _infStatusBadge(status) {
    let cls = 'idx-warn';
    if (status === 'ok') cls = 'idx-ok';
    else if (status === 'queued' || status === 'running') cls = 'inf-st-run';
    else if (status === 'error') cls = 'idx-err';
    return `<span class="idx-badge ${cls}">${escapeHtml(status || '—')}</span>`;
}

function _infPriBadge(priority) {
    const p = priority || '—';
    return `<span class="idx-badge inf-pri-${escapeHtml(p)}">${escapeHtml(p)}</span>`;
}

function _infStatusGroup(status) {
    if (status === 'ok') return 'ok';
    if (status === 'queued' || status === 'running') return 'queued';
    return 'errored';
}

// Health verdict from occupancy: interactive work waiting is the bad signal the
// whole feature exists to surface.
function _infHealth(data) {
    const profs = Object.values(data.profiles || {});
    let inFlight = 0, interWait = 0, totalWait = 0;
    for (const p of profs) {
        inFlight += (p.in_flight || 0);
        const w = p.waiting || {};
        interWait += (w.INTERACTIVE || 0);
        totalWait += _infWaiting(w);
    }
    if (interWait > 0) {
        return { cls: 'inf-verdict-warn', icon: '⚠',
                 text: 'Contended — interactive work is waiting behind other jobs' };
    }
    if (inFlight > 0 || totalWait > 0) {
        return { cls: 'inf-verdict-busy', icon: '◐',
                 text: 'Busy — calls in flight; nothing you are waiting on is queued' };
    }
    return { cls: 'inf-verdict-ok', icon: '✓', text: 'Healthy — idle, nothing queued' };
}

const _INF_HELP_HTML = `
<div class="inf-help">
    <div><b>Priority</b> — INTERACTIVE (you're waiting on it) preempts WORKFLOW (agent work),
        which preempts BACKGROUND (cron / bulk jobs) on a busy model.</div>
    <div><b>Slots</b> — each profile runs up to <i>max_concurrent</i> calls at once; extra calls
        queue, up to the per-priority cap.</div>
    <div><b>Latency splits</b> — <i>Queue wait</i>: time waiting for a free slot ·
        <i>Service</i>: the model call itself · <i>Total</i>: end-to-end.</div>
    <div><b>Contended</b> means an interactive call is stuck waiting — that's when local
        inference feels slow.</div>
</div>`;

function _infToggleHelp() { _infHelpOpen = !_infHelpOpen; _infRender(); }

async function loadInference() {
    const container = document.getElementById('inference-content');
    if (!container) return;
    window._infLast = await fetchJSON('/api/broker');
    _infRender();
}

function _infRender() {
    const container = document.getElementById('inference-content');
    if (!container) return;
    const data = window._infLast;
    if (!data) {
        window._wbMorphReplace(container, `<div class="empty-state">Inference broker state unavailable.</div>`);
        return;
    }
    let html = _infRenderHeader(data);
    if (!data.available) {
        html += `<div class="empty-state">No data from the embedding-service broker — it may be `
              + `offline or still starting. Recent activity reappears once it's reachable.</div>`;
    } else if (!Object.keys(data.profiles || {}).length && !(data.recent || []).length) {
        html += `<div class="empty-state">Idle — nothing has run through the broker yet. Occupancy, `
              + `recent calls, and latency appear here as local inference runs (persisted ~7 days).</div>`;
    } else {
        html += _infRenderCards(data) + _infRenderLatency(data) + _infRenderRecent(data);
    }
    window._wbMorphReplace(container, html);
}

function _infRenderHeader(data) {
    const v = data.available
        ? _infHealth(data)
        : { cls: 'inf-verdict-off', icon: '○', text: 'Embedding service offline — no live broker data' };
    return `
        <div class="inf-header">
            <div class="inf-verdict ${v.cls}">
                <span class="inf-verdict-icon">${v.icon}</span>
                <span class="inf-verdict-text">${escapeHtml(v.text)}</span>
                <button class="inf-help-btn" title="What am I looking at?" onclick="_infToggleHelp()">?</button>
            </div>
            <div class="idx-muted inf-intro">work-buddy schedules local model calls by priority so the work
                you're waiting on isn't stuck behind background jobs. Recent activity from the embedding-service
                broker — live-updating, and persisted ~7 days so it survives restarts.</div>
            ${_infHelpOpen ? _INF_HELP_HTML : ''}
        </div>`;
}

function _infRenderCards(data) {
    const profiles = data.profiles || {};
    const names = Object.keys(profiles).sort();
    if (!names.length) return '';
    const cards = names.map(name => {
        const p = profiles[name];
        const { role, model } = _infRole(name);
        const max = (p.max_concurrent == null) ? 1 : p.max_concurrent;
        const inf = p.in_flight || 0;
        const waiting = _infWaiting(p.waiting);
        const pct = max ? Math.min(100, (inf / max) * 100) : 0;
        let st = { label: 'Idle', cls: 'inf-st-idle' };
        if (waiting > 0) st = { label: 'Queued', cls: 'idx-warn' };
        else if (inf > 0) st = { label: 'Active', cls: 'inf-st-run' };
        const waitWords = _infWaitWords(p.waiting);
        return `
        <div class="inf-card" title="${escapeHtml(name)}">
            <div class="inf-card-head">
                <span class="inf-card-role">${escapeHtml(role)}</span>
                <span class="idx-badge ${st.cls}">${st.label}</span>
            </div>
            <div class="inf-card-model idx-muted">${escapeHtml(model || name)}</div>
            <div class="inf-gauge" title="${inf} of ${max} concurrency slots in use">
                <div class="inf-gauge-fill" style="width:${pct}%"></div>
            </div>
            <div class="idx-muted inf-gauge-label">${inf} of ${max} slot${max === 1 ? '' : 's'} busy</div>
            ${waitWords ? `<div class="inf-wait-words">${escapeHtml(waitWords)}</div>` : ''}
            <div class="idx-muted inf-card-foot" title="Max calls queued per priority class before new calls are rejected">queue cap ${(p.max_queued == null) ? '—' : p.max_queued}</div>
        </div>`;
    }).join('');
    return `
        <div class="inf-section">
            <div class="emb-section-head">
                <span class="emb-section-title">Occupancy</span>
                <span class="idx-muted">slots in use + anything waiting, per profile</span>
            </div>
            <div class="inf-cards">${cards}</div>
        </div>`;
}

function _infRenderLatency(data) {
    const lat = data.latency || {};
    const win = lat.window_s ? Math.round(lat.window_s / 60) : 5;
    const n = lat.n || 0;
    const cell = (label, v) =>
        `<div class="inf-lat-cell"><div class="inf-lat-num">${_infMs(v)}<span class="inf-lat-unit">ms</span></div>`
        + `<div class="inf-lat-label">${label}</div></div>`;
    let body;
    if (n === 0) {
        body = `<div class="idx-muted">No successful calls in the last ${win} min.</div>`;
    } else if (n === 1) {
        body = `<div class="inf-lat">${cell('total', lat.p50)}</div>`
             + `<div class="idx-muted inf-lat-note">Only 1 call in the window — needs more for meaningful percentiles.</div>`;
    } else {
        body = `<div class="inf-lat">${cell('p50', lat.p50)}${cell('p95', lat.p95)}${cell('p99', lat.p99)}</div>`;
    }
    return `
        <div class="inf-section">
            <div class="emb-section-head">
                <span class="emb-section-title">Latency</span>
                <span class="idx-muted">end-to-end time of successful calls · last ${win} min · ${n} call${n === 1 ? '' : 's'} (the table below lists recent calls)</span>
            </div>
            ${body}
        </div>`;
}

function _infChip(group, value, label, active) {
    return `<button class="inf-chip${active ? ' active' : ''}" onclick="${group}('${value}')">${escapeHtml(label)}</button>`;
}

function _infRenderRecent(data) {
    // Newest-first: snapshot_metrics returns oldest-first.
    const allRows = (data.recent || []).slice().reverse();
    // Filter chips only earn their space once there are enough rows to scan.
    const showFilters = allRows.length > 5;
    let rows = allRows;
    if (showFilters) {
        rows = rows.filter(r =>
            (_infPriorityFilter === 'All' || r.priority === _infPriorityFilter)
            && (_infStatusFilter === 'All' || _infStatusGroup(r.status) === _infStatusFilter));
    }

    let filterBar = '';
    if (showFilters) {
        const priChips = ['All', 'INTERACTIVE', 'WORKFLOW', 'BACKGROUND']
            .map(p => _infChip('_infSetPriority', p, p, _infPriorityFilter === p)).join('');
        const stChips = ['All', 'ok', 'queued', 'errored']
            .map(s => _infChip('_infSetStatus', s, s, _infStatusFilter === s)).join('');
        filterBar = `<div class="inf-filters"><span class="inf-filter-label">priority</span>${priChips}`
                  + `<span class="inf-filter-label">status</span>${stChips}</div>`;
    }

    const body = rows.length ? rows.map(r => `
        <tr>
            <td class="idx-muted" title="time since this call was queued">${_infAge(r.age_s)}</td>
            <td class="inf-prof" title="${escapeHtml(r.profile || '')}">${escapeHtml(_infRole(r.profile).role)}</td>
            <td>${_infPriBadge(r.priority)}</td>
            <td>${_infStatusBadge(r.status)}${r.error_detail ? ` <span class="idx-muted" title="${escapeHtml(r.error_detail)}">ⓘ</span>` : ''}</td>
            <td class="idx-r">${_infMs(r.queue_wait_ms)}</td>
            <td class="idx-r">${_infMs(r.service_time_ms)}</td>
            <td class="idx-r">${_infMs(r.total_latency_ms)}</td>
        </tr>`).join('')
        : `<tr><td colspan="7" class="idx-muted">no calls match the current filter</td></tr>`;

    return `
        <div class="inf-section">
            <div class="emb-section-head">
                <span class="emb-section-title">Recent calls</span>
                <span class="idx-muted">last ${allRows.length}, newest first — live + persisted (~7 days)</span>
            </div>
            ${filterBar}
            <div class="inf-table-scroll">
                <table class="data-table">
                    <thead><tr>
                        <th title="time since the call was queued">Age</th>
                        <th>Profile</th><th>Priority</th><th>Status</th>
                        <th class="idx-r" title="time spent waiting for a free slot">Queue wait (ms)</th>
                        <th class="idx-r" title="time in the actual model call">Service (ms)</th>
                        <th class="idx-r" title="end-to-end: queue wait + service">Total (ms)</th>
                    </tr></thead>
                    <tbody>${body}</tbody>
                </table>
            </div>
        </div>`;
}

function _infSetPriority(p) { _infPriorityFilter = p; _infRender(); }
function _infSetStatus(s) { _infStatusFilter = s; _infRender(); }

// Surface handle for the event-bus dispatcher (broker.state → _refreshSoon).
window.inferenceSurface = {
    refresh: function() { return loadInference(); },
    isMounted: function() {
        return !!document.getElementById('inference-content')
            && (typeof WB_SETTINGS_SUBTAB === 'undefined' || WB_SETTINGS_SUBTAB === 'inference');
    },
};
"""


def styles() -> str:
    return """
/* Inference sub-view — reuses .idx-badge/.idx-ok|warn|err/.data-table/.idx-r/.idx-muted
   from the Embeddings sub-view; only inference-specific classes below. */
.inf-section { margin-bottom: 22px; }

/* Header: health verdict + orientation + help */
.inf-header { margin-bottom: 20px; }
.inf-verdict { display:flex; align-items:center; gap:8px; font-size:1.05em; font-weight:600;
    padding:10px 14px; border-radius:8px; border-left:4px solid var(--text-muted);
    background: var(--bg-tertiary); color: var(--text-primary); }
.inf-verdict-icon { font-size:1.1em; }
.inf-verdict-ok   { border-left-color: var(--green);  background: color-mix(in srgb, var(--green) 10%, var(--bg-tertiary)); }
.inf-verdict-busy { border-left-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, var(--bg-tertiary)); }
.inf-verdict-warn { border-left-color: var(--yellow); background: color-mix(in srgb, var(--yellow) 14%, var(--bg-tertiary)); }
.inf-verdict-off  { border-left-color: var(--text-muted); color: var(--text-secondary); }
.inf-help-btn { margin-left:auto; width:22px; height:22px; border-radius:50%; cursor:pointer;
    border:1px solid var(--text-muted); background:transparent; color: var(--text-secondary);
    font-weight:700; line-height:1; flex:0 0 auto; }
.inf-help-btn:hover { color: var(--text-primary); border-color: var(--text-primary); }
.inf-intro { margin-top:8px; font-size:0.85em; max-width:70ch; }
.inf-help { margin-top:10px; padding:12px 14px; border-radius:8px; background: var(--bg-tertiary);
    font-size:0.85em; color: var(--text-secondary); display:flex; flex-direction:column; gap:6px; }
.inf-help b { color: var(--text-primary); }

/* Occupancy cards */
.inf-cards { display:flex; flex-wrap:wrap; gap:12px; }
.inf-card { background: var(--bg-tertiary); border:1px solid var(--border, var(--bg-tertiary));
    border-radius:8px; padding:12px 14px; min-width:210px; flex:0 1 250px; }
.inf-card-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.inf-card-role { font-weight:600; color: var(--text-primary); }
.inf-card-model { font-size:0.78em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin:2px 0 8px; }
.inf-gauge { height:6px; border-radius:3px; background: color-mix(in srgb, var(--text-muted) 25%, transparent); overflow:hidden; }
.inf-gauge-fill { height:100%; background: var(--accent); border-radius:3px; transition:width .3s ease; }
.inf-gauge-label { font-size:0.8em; margin-top:4px; }
.inf-wait-words { font-size:0.82em; color: var(--yellow); font-weight:600; margin-top:6px; }
.inf-card-foot { margin-top:6px; font-size:0.78em; }
.inf-st-idle { background: color-mix(in srgb, var(--text-muted) 22%, transparent); color: var(--text-muted); }
.inf-st-run  { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }

/* Priority color coding (shared by waiting words context + table badges) */
.inf-pri-INTERACTIVE { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
.inf-pri-WORKFLOW { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }
.inf-pri-BACKGROUND { background: color-mix(in srgb, var(--text-muted) 22%, transparent); color: var(--text-muted); }

/* Latency summary */
.inf-lat { display:flex; gap:28px; }
.inf-lat-cell { text-align:center; }
.inf-lat-num { font-size:1.8em; font-weight:700; color: var(--text-primary); font-variant-numeric:tabular-nums; }
.inf-lat-unit { font-size:0.5em; color: var(--text-muted); margin-left:3px; }
.inf-lat-label { font-size:0.8em; color: var(--text-muted); margin-top:2px; }
.inf-lat-note { font-size:0.8em; margin-top:6px; }

/* Filter chips */
.inf-filters { display:flex; align-items:center; gap:6px; margin-bottom:8px; flex-wrap:wrap; }
.inf-filter-label { font-size:0.75em; color: var(--text-muted); margin-left:8px; }
.inf-filter-label:first-child { margin-left:0; }
.inf-chip { font-size:0.78em; padding:2px 10px; border-radius:12px; cursor:pointer;
    border:1px solid var(--border, var(--bg-tertiary)); background: var(--bg-tertiary); color: var(--text-secondary); }
.inf-chip.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.inf-prof { font-variant-numeric:tabular-nums; }

/* Recent-calls scroll viewport (mirrors the Activity Event Log's .log-container) */
.inf-table-scroll { max-height: 360px; overflow-y: auto; border:1px solid var(--border, var(--bg-tertiary)); border-radius:8px; }
.inf-table-scroll thead th { position: sticky; top: 0; background: var(--bg-secondary); z-index: 1; }
"""
