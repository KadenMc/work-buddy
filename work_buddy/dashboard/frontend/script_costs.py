"""Dashboard Costs tab JS — fetches /api/costs and renders charts/tables.

Charting via Chart.js v4.4.0, vendored at
``work_buddy/dashboard/frontend/vendor/chart.umd.min.js`` and served by
the Flask ``/vendor/<path>`` route in ``service.py``.

UX model (post-2026-04-25 redesign): the user picks a **project** (the
primary axis, modeled on the Chats project filter). When project =
``work-buddy``, an Activity sub-pill exposes Claude Code / API / Local /
All. For every other project, only Claude Code data is meaningful so
the activity pill is hidden. Conditional plot/card visibility is driven
by (project, activity).
"""

from __future__ import annotations


def _costs_script() -> str:
    return r"""
// ---- Costs tab state ----
const COSTS_PAGE_SIZE = 60;

let costsState = {
    raw: null,                    // last /api/costs?source=all response
    project: '',                  // '' = all projects, else exact project name
    activity: 'all',              // 'all' | 'claude_code' | 'programmatic' | 'api' | 'local'
                                  // (only meaningful when project === 'work-buddy')
    range: '30',                  // 'today' | '7' | '30' | '90' | 'all'
    selectedModels: null,         // null = all selected; Set of strings otherwise
    sessionPage: 1,
    sessionSort: { key: 'last', dir: 'desc' },
    charts: {},                   // Chart.js instances keyed by canvas id
    projectsLoaded: false,
};

// Token color encoding — matches claude-usage so cross-tool comparisons stay coherent.
const COSTS_TOKEN_COLORS = {
    input:          'rgba(79,142,247,0.85)',
    output:         'rgba(167,139,250,0.85)',
    cache_read:     'rgba(74,222,128,0.65)',
    cache_creation: 'rgba(251,191,36,0.65)',
};
const COSTS_MODEL_PALETTE = [
    '#D87857', '#4f8ef7', '#3fb950', '#bc8cff',
    '#d29922', '#f472b6', '#34d399', '#60a5fa',
    '#f85149', '#a78bfa',
];

// ---- Number formatting ----
function costsFmtN(n) {
    if (n == null) return '-';
    n = Number(n);
    if (!isFinite(n)) return '-';
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return n.toString();
}
function costsFmtCost(c) {
    if (c == null) return '-';
    c = Number(c);
    if (!isFinite(c)) return '-';
    if (c < 0.01) return '$' + c.toFixed(4);
    if (c < 1)    return '$' + c.toFixed(3);
    return '$' + c.toFixed(2);
}
function costsFmtDate(s) {
    if (!s) return '-';
    return s.slice(0, 16).replace('T', ' ');
}
function costsEsc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---- Project list population ----
async function costsLoadProjects() {
    if (costsState.projectsLoaded) return;
    const data = await fetchJSON('/api/costs/projects');
    if (!data || !data.projects) return;
    costsState.projectsLoaded = true;
    const sel = document.getElementById('costs-project');
    if (!sel) return;
    // "All projects" stays first; "work-buddy" is pinned by the backend.
    let html = '<option value="">All projects</option>';
    for (const p of data.projects) {
        const sub = p.session_count ? ' (' + p.session_count + ')' : '';
        html += '<option value="' + costsEsc(p.name) + '">'
              + costsEsc(p.name) + sub + '</option>';
    }
    sel.innerHTML = html;
    // Restore selection if the user had picked something earlier.
    if (costsState.project) sel.value = costsState.project;
}

// ---- Fetch ----
async function loadCosts(force) {
    await costsLoadProjects();

    const meta = document.getElementById('costs-meta');
    if (meta) meta.textContent = 'loading...';

    const params = new URLSearchParams();
    params.set('source', 'all');
    if (costsState.project) params.set('project', costsState.project);
    // For api / local activity, ask the backend to filter rows by
    // execution_mode so by_model / sessions / etc. are properly sliced.
    // Programmatic = work-buddy runner activity (cloud + local) — no
    // execution_mode filter, but we restrict to internal source on render.
    if (costsState.activity === 'api')   params.set('execution_mode', 'cloud');
    if (costsState.activity === 'local') params.set('execution_mode', 'local');

    const data = await fetchJSON('/api/costs?' + params.toString());
    if (!data || data.error) {
        document.getElementById('costs-cards').innerHTML =
            '<div class="empty-state">Failed to load cost data: ' +
            costsEsc((data && data.error) || 'unknown error') + '</div>';
        if (meta) meta.textContent = '';
        return;
    }

    costsState.raw = data;
    _costsSyncActivityVisibility();
    costsState.sessionPage = 1;

    // Reset selectedModels to all-known when refetching (new project may
    // expose a different model set).
    const all = _costsCurrentModels();
    costsState.selectedModels = new Set(all);

    costsRenderAll();
}

// ---- Toolbar handlers ----
function costsProjectChanged(v) {
    costsState.project = v || '';
    // Reset activity to "all" whenever the project changes.
    costsState.activity = 'all';
    _costsSyncActivityVisibility();
    loadCosts(true);
}
function costsRangeChanged(v) {
    costsState.range = v;
    costsRenderAll();
}
function costsActivityChanged(v) {
    const prev = costsState.activity;
    costsState.activity = v;
    document.querySelectorAll('#costs-activity-pills .costs-pill').forEach(b => {
        b.classList.toggle('active', b.dataset.activity === v);
    });
    // api / local require a backend refetch (execution_mode filter
    // applies at row level — by_model / sessions need to be re-aggregated).
    // Switching back to claude_code / all / programmatic doesn't need a
    // refetch as long as we already have the all-source response.
    const needsRefetch = (v === 'api' || v === 'local')
                       || (prev === 'api' || prev === 'local');
    if (needsRefetch) loadCosts(true);
    else costsRenderAll();
}

function _costsSyncActivityVisibility() {
    const row = document.getElementById('costs-activity-row');
    if (!row) return;
    const isWB = (costsState.project || '').toLowerCase() === 'work-buddy';
    row.style.display = isWB ? '' : 'none';
}

async function costsRescanClaudeCode(btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Scanning...'; }
    try {
        const r = await fetch('/api/costs/rescan', { method: 'POST' });
        await r.json();
    } catch (e) {
        console.error('rescan failed', e);
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Rescan Claude Code'; }
    await loadCosts(true);
}

// ---- Active-data resolver ----
// Given (project, activity), returns the read-model dict the renderers
// read from, plus a ``shape`` discriminator. Per the design notes:
//   - project='' (All)        → claude_code (the dominant signal)
//   - project='work-buddy'    → activity-driven:
//       'all'          → claude_code primary; internal sidecar card
//       'claude_code'  → claude_code only
//       'programmatic' → internal (cloud + local combined; runner activity)
//       'api'          → internal, backend-filtered to cloud
//       'local'        → internal, backend-filtered to local
//   - project=other           → claude_code
function _costsActiveData() {
    const raw = costsState.raw || {};
    const project = costsState.project || '';
    const isWB = project.toLowerCase() === 'work-buddy';

    if (!isWB) {
        return { data: raw.claude_code || {}, shape: 'claude_code',
                 activity: null };
    }

    const activity = costsState.activity || 'all';
    if (activity === 'claude_code' || activity === 'all') {
        return { data: raw.claude_code || {}, shape: 'claude_code',
                 activity, sidecar: raw.internal || null };
    }
    // programmatic / api / local — internal source.
    // For api / local, the backend has already filtered raw.internal by
    // execution_mode; we just render it. For programmatic, raw.internal
    // is unfiltered (cloud + local combined, which is what we want).
    return { data: raw.internal || {}, shape: 'internal', activity };
}

// ---- Range filter helpers ----
function _costsRangeStartIso() {
    if (costsState.range === 'all') return null;
    if (costsState.range === 'today') {
        const d = new Date();
        d.setHours(0, 0, 0, 0);
        return d.toISOString().slice(0, 10);
    }
    const days = parseInt(costsState.range, 10);
    if (!isFinite(days) || days <= 0) return null;
    const d = new Date();
    d.setDate(d.getDate() - days + 1);
    d.setHours(0, 0, 0, 0);
    return d.toISOString().slice(0, 10);
}
function _costsFilterDay(d) {
    const start = _costsRangeStartIso();
    if (start && d.day < start) return false;
    return true;
}
function _costsFilterSession(s) {
    const start = _costsRangeStartIso();
    if (start && (s.last || '').slice(0, 10) < start) return false;
    if (costsState.selectedModels && costsState.selectedModels.size > 0) {
        const sm = s.models || (s.model ? [s.model] : []);
        if (sm.length > 0 && !sm.some(m => costsState.selectedModels.has(m))) {
            return false;
        }
    }
    return true;
}
function _costsFilterModelRow(r) {
    if (costsState.selectedModels && costsState.selectedModels.size > 0) {
        return costsState.selectedModels.has(r.model);
    }
    return true;
}

function _costsCurrentModels() {
    const { data } = _costsActiveData();
    return data.all_models || [];
}

// ---- Rendering ----
function costsRenderAll() {
    if (!costsState.raw) return;

    const { data, shape, activity, sidecar } = _costsActiveData();

    // Empty-state for the Claude Code source when nothing is cached yet.
    if (shape === 'claude_code' && data && data.available === false) {
        document.getElementById('costs-cards').innerHTML =
            `<div class="empty-state" style="padding:24px;text-align:center;">
                <div style="margin-bottom:12px;">${costsEsc(data.message || 'No Claude Code usage cached yet.')}</div>
                <button class="chats-accent-btn"
                        onclick="costsRescanClaudeCode(this)">Rescan Claude Code</button>
            </div>`;
        document.getElementById('costs-models-filter').innerHTML = '';
        document.getElementById('costs-model-table').innerHTML = '';
        document.getElementById('costs-sessions-table').innerHTML = '';
        ['daily', 'model', 'task', 'mode'].forEach(_costsDestroyChart);
        _costsRenderMeta(0, 0);
        return;
    }

    // Track shape on costsState so older filter helpers that referenced
    // ``costsState.shape`` keep working.
    costsState.shape = shape;

    costsRenderModelsFilter();
    costsRenderCards(shape, data, activity, sidecar);
    costsRenderDailyChart(shape, data);
    costsRenderModelChart(shape, data);

    // Source-aware secondary charts.
    const tt = document.getElementById('costs-task-title');
    const mt = document.getElementById('costs-mode-title');
    const taskCard = document.getElementById('costs-task-chart')?.closest('.costs-chart-card');
    const modeCard = document.getElementById('costs-mode-chart')?.closest('.costs-chart-card');

    const isWB = (costsState.project || '').toLowerCase() === 'work-buddy';
    const isAllProjects = !costsState.project;

    if (shape === 'claude_code') {
        // Top tools (always useful for Claude Code).
        if (taskCard) taskCard.style.display = '';
        if (tt) tt.textContent = 'Top tools (by turns)';
        costsRenderToolChart(data);

        // Top projects only makes sense when not already filtered to one project.
        if (modeCard) modeCard.style.display = isAllProjects ? '' : 'none';
        if (isAllProjects) {
            if (mt) mt.textContent = 'Top projects (by cost)';
            costsRenderProjectChart(data);
        } else {
            _costsDestroyChart('mode');
        }
    } else {
        // shape === 'internal' (work-buddy + activity = api/local/programmatic)
        // Top callers chart only meaningful for the work-buddy infra view.
        if (taskCard) taskCard.style.display = isWB ? '' : 'none';
        if (isWB) {
            if (tt) tt.textContent = 'Top callers (by cost)';
            costsRenderTaskChart(data, activity);
        } else {
            _costsDestroyChart('task');
        }
        // Cloud-vs-Local mix is meaningless when activity is already
        // restricted to one side — show only on Programmatic (= both).
        const showMode = isWB && activity === 'programmatic';
        if (modeCard) modeCard.style.display = showMode ? '' : 'none';
        if (showMode) {
            if (mt) mt.textContent = 'Cloud vs Local mix';
            costsRenderModeChart(data, activity);
        } else {
            _costsDestroyChart('mode');
        }
    }

    costsRenderModelTable(shape, data);
    costsRenderSessionsTable(shape, data);
}

// ---- Models filter (no All/None — chip toggling only) ----
function costsRenderModelsFilter() {
    const all = _costsCurrentModels();
    const el = document.getElementById('costs-models-filter');
    if (!el) return;
    if (all.length === 0) {
        el.innerHTML = '';
        return;
    }
    let html = '<span class="costs-filter-label">Models:</span>';
    for (const m of all) {
        const on = costsState.selectedModels.has(m);
        html += `<button class="costs-filter-pill${on ? ' active' : ''}"
                    onclick="costsModelToggle('${costsEsc(m)}')">${costsEsc(m)}</button>`;
    }
    el.innerHTML = html;
}

function costsModelToggle(m) {
    if (costsState.selectedModels.has(m)) costsState.selectedModels.delete(m);
    else costsState.selectedModels.add(m);
    costsRenderAll();
}

// ---- Aggregation helpers (range-filtered) ----
function _costsAggregateByDay(data) {
    const days = (data.by_day || []).filter(_costsFilterDay);
    const totals = {
        calls: 0, api_calls: 0, cache_hits: 0,
        cloud_calls: 0, local_calls: 0,
        turns: 0,
        input_tokens: 0, output_tokens: 0,
        cache_read_tokens: 0, cache_creation_tokens: 0,
        cost_usd: 0,
    };
    for (const d of days) {
        for (const k of Object.keys(totals)) {
            totals[k] += (d[k] || 0);
        }
    }
    return { days, totals };
}

// ---- Meta line ----
function _costsRenderMeta(filteredSessions, filteredCalls,
                          totalSessions, totalCalls) {
    const meta = document.getElementById('costs-meta');
    if (!meta) return;
    const sLabel = (totalSessions != null && filteredSessions !== totalSessions)
        ? `${filteredSessions} of ${totalSessions} sessions`
        : `${filteredSessions} session${filteredSessions === 1 ? '' : 's'}`;
    const cLabel = (totalCalls != null && filteredCalls !== totalCalls)
        ? `${filteredCalls} of ${totalCalls} ${costsState.shape === 'claude_code' ? 'turns' : 'calls'}`
        : `${filteredCalls} ${costsState.shape === 'claude_code' ? 'turn' : 'call'}${filteredCalls === 1 ? '' : 's'}`;
    meta.textContent = `${sLabel} · ${cLabel}`;
}

// ---- Cards ----
function costsRenderCards(shape, data, activity, sidecar) {
    const wrap = document.getElementById('costs-cards');
    if (!wrap) return;

    const sessions = (data.sessions || []).filter(_costsFilterSession);
    const totalSessions = (data.sessions || []).length;
    const { totals } = _costsAggregateByDay(data);

    // The backend already pre-filtered raw.internal by execution_mode for
    // activity = api / local. Totals are accurate as-is.
    let displayed = totals;

    const totalCalls = (data.totals || {}).calls || (data.totals || {}).turns || 0;
    const filteredCalls = (displayed.calls || displayed.turns || 0);
    _costsRenderMeta(sessions.length, filteredCalls,
                      totalSessions, totalCalls);

    const cards = [];
    cards.push({ label: 'Sessions', value: sessions.length.toString() });

    if (shape === 'claude_code') {
        cards.push({ label: 'Turns', value: costsFmtN(displayed.turns) });
    } else {
        // Internal log — split cloud/local in the Calls card sub.
        let sub;
        if (activity === 'api')   sub = 'cloud only';
        else if (activity === 'local') sub = 'local only';
        else sub = `${displayed.cloud_calls || 0} cloud · ${displayed.local_calls || 0} local`;
        cards.push({ label: 'Calls',
                     value: costsFmtN(displayed.calls), sub });
    }

    cards.push({ label: 'Input',  value: costsFmtN(displayed.input_tokens) });
    cards.push({ label: 'Output', value: costsFmtN(displayed.output_tokens) });

    const hasCache = (displayed.cache_read_tokens || 0) > 0
                  || (displayed.cache_creation_tokens || 0) > 0;
    if (hasCache) {
        cards.push({ label: 'Cache read',
                     value: costsFmtN(displayed.cache_read_tokens),
                     sub: '90% off input rate' });
        cards.push({ label: 'Cache write',
                     value: costsFmtN(displayed.cache_creation_tokens),
                     sub: '+25% premium' });
    }

    let costSub;
    if (shape === 'internal' && activity === 'local') costSub = 'local LLM — no cloud cost';
    else if (shape === 'internal' && activity === 'api') costSub = 'work-buddy API spend';
    else if (shape === 'internal' && activity === 'programmatic') costSub = 'work-buddy runner — cloud only billable';
    else if (shape === 'claude_code') costSub = 'Claude Code (Anthropic rates)';
    else costSub = 'cloud only; local logs $0';
    cards.push({ label: 'Est. cost',
                 value: costsFmtCost(displayed.cost_usd),
                 sub: costSub });

    // Sidecar: when project=work-buddy + activity=all, the primary view is
    // claude_code, but it's worth surfacing the small work-buddy infra
    // contribution as a separate card (so it stays visible).
    if (sidecar && (sidecar.totals || {}).calls) {
        const t = sidecar.totals;
        cards.push({ label: 'Plus work-buddy infra',
                     value: costsFmtN(t.calls),
                     sub: `${t.cloud_calls || 0} cloud · ${t.local_calls || 0} local · ${costsFmtCost(t.cost_usd)}`,
                     accent: true });
    }

    wrap.innerHTML = cards.map(c => `
        <div class="card${c.accent ? ' card-accent' : ''}">
            <div class="card-label">${costsEsc(c.label)}</div>
            <div class="card-value">${costsEsc(c.value)}</div>
            ${c.sub ? `<div class="card-sub">${costsEsc(c.sub)}</div>` : ''}
        </div>
    `).join('');
}

function _costsDestroyChart(id) {
    if (costsState.charts[id]) {
        try { costsState.charts[id].destroy(); } catch (_) {}
        delete costsState.charts[id];
    }
}

// ---- Daily chart (hidden when range = today) ----
function costsRenderDailyChart(shape, data) {
    const card = document.getElementById('costs-daily-chart')?.closest('.costs-chart-card');
    if (costsState.range === 'today') {
        if (card) card.style.display = 'none';
        _costsDestroyChart('daily');
        return;
    }
    if (card) card.style.display = '';

    const canvas = document.getElementById('costs-daily-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('daily');

    const { days } = _costsAggregateByDay(data);
    const labels = days.map(d => d.day);
    const inputs = days.map(d => d.input_tokens || 0);
    const outputs = days.map(d => d.output_tokens || 0);
    const cacheRead = days.map(d => d.cache_read_tokens || 0);
    const cacheCreate = days.map(d => d.cache_creation_tokens || 0);
    const costs = days.map(d => d.cost_usd || 0);

    const datasets = [
        { label: 'Input',  data: inputs,
          backgroundColor: COSTS_TOKEN_COLORS.input, stack: 'tokens', yAxisID: 'y' },
        { label: 'Output', data: outputs,
          backgroundColor: COSTS_TOKEN_COLORS.output, stack: 'tokens', yAxisID: 'y' },
    ];
    const hasCache = cacheRead.some(v => v > 0) || cacheCreate.some(v => v > 0);
    if (hasCache) {
        datasets.push(
            { label: 'Cache read',  data: cacheRead,
              backgroundColor: COSTS_TOKEN_COLORS.cache_read, stack: 'tokens', yAxisID: 'y' },
            { label: 'Cache write', data: cacheCreate,
              backgroundColor: COSTS_TOKEN_COLORS.cache_creation, stack: 'tokens', yAxisID: 'y' },
        );
    }
    datasets.push({
        label: 'Cost (USD)', data: costs,
        type: 'line', borderColor: '#D87857',
        backgroundColor: 'rgba(216,120,87,0.15)',
        yAxisID: 'y1', tension: 0.25, pointRadius: 2,
    });

    costsState.charts.daily = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: { labels, datasets },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: '#e6edf3' } },
                tooltip: { mode: 'index', intersect: false },
            },
            scales: {
                x: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' }, stacked: true },
                y: { ticks: { color: '#8b949e', callback: costsFmtN },
                     grid: { color: '#21262d' }, stacked: true,
                     title: { display: true, text: 'tokens', color: '#6e7681' } },
                y1: { position: 'right', ticks: { color: '#D87857', callback: costsFmtCost },
                      grid: { drawOnChartArea: false },
                      title: { display: true, text: 'cost', color: '#D87857' } },
            },
        },
    });
}

// ---- Model donut ----
function costsRenderModelChart(shape, data) {
    const canvas = document.getElementById('costs-model-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('model');
    const rows = (data.by_model || []).filter(_costsFilterModelRow);
    const labels = rows.map(r => r.model);
    const data_arr = rows.map(r => r.cost_usd || 0);
    const useTokens = labels.length === 0 || data_arr.every(v => v === 0);
    const tokens = rows.map(r => (r.input_tokens || 0) + (r.output_tokens || 0));
    costsState.charts.model = new Chart(canvas.getContext('2d'), {
        type: 'doughnut',
        data: { labels, datasets: [{
            data: useTokens ? tokens : data_arr,
            backgroundColor: labels.map((_, i) => COSTS_MODEL_PALETTE[i % COSTS_MODEL_PALETTE.length]),
        }]},
        options: { responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: 'right',
                labels: { color: '#e6edf3', boxWidth: 12 } },
                tooltip: { callbacks: { label: ctx => ctx.label + ': ' +
                    (useTokens ? costsFmtN(ctx.parsed) + ' tokens'
                                : costsFmtCost(ctx.parsed)) } } } },
    });
}

// ---- Task / Tool / Mode / Project charts ----
function costsRenderTaskChart(data, activity) {
    const canvas = document.getElementById('costs-task-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('task');
    const rows = (data.by_task || []).slice(0, 10);
    const labels = rows.map(r => r.task);
    const data_arr = rows.map(r => r.cost_usd || 0);
    const useTokens = data_arr.every(v => v === 0);
    const tokens = rows.map(r => (r.input_tokens || 0) + (r.output_tokens || 0));
    costsState.charts.task = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: { labels, datasets: [{
            label: useTokens ? 'tokens' : 'cost (USD)',
            data: useTokens ? tokens : data_arr,
            backgroundColor: '#D87857',
        }]},
        options: {
            indexAxis: 'y', responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false },
                tooltip: { callbacks: { label: ctx => useTokens
                    ? costsFmtN(ctx.parsed.x) + ' tokens'
                    : costsFmtCost(ctx.parsed.x) } } },
            scales: {
                x: { ticks: { color: '#8b949e',
                              callback: useTokens ? costsFmtN : costsFmtCost },
                     grid: { color: '#21262d' } },
                y: { ticks: { color: '#e6edf3' }, grid: { display: false } },
            },
        },
    });
}

function costsRenderToolChart(data) {
    const canvas = document.getElementById('costs-task-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('task');
    const rows = (data.by_tool || []).slice(0, 10);
    const labels = rows.map(r => r.tool);
    const data_arr = rows.map(r => r.turns || 0);
    costsState.charts.task = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: { labels, datasets: [{
            label: 'turns', data: data_arr, backgroundColor: '#4f8ef7',
        }]},
        options: {
            indexAxis: 'y', responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false },
                tooltip: { callbacks: { label: ctx => costsFmtN(ctx.parsed.x) + ' turns' } } },
            scales: {
                x: { ticks: { color: '#8b949e', callback: costsFmtN },
                     grid: { color: '#21262d' } },
                y: { ticks: { color: '#e6edf3' }, grid: { display: false } },
            },
        },
    });
}

function costsRenderModeChart(data, activity) {
    const canvas = document.getElementById('costs-mode-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('mode');
    let rows = data.by_execution_mode || [];
    if (activity === 'api')   rows = rows.filter(r => r.mode === 'cloud');
    if (activity === 'local') rows = rows.filter(r => r.mode === 'local');
    const labels = rows.map(r => r.mode || 'unknown');
    const calls = rows.map(r => r.calls || 0);
    const colors = labels.map(l => l === 'cloud' ? '#D87857'
                                  : l === 'local' ? '#3fb950' : '#6e7681');
    costsState.charts.mode = new Chart(canvas.getContext('2d'), {
        type: 'doughnut',
        data: { labels, datasets: [{ data: calls, backgroundColor: colors }] },
        options: { responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: 'right',
                labels: { color: '#e6edf3', boxWidth: 12 } },
                tooltip: { callbacks: { label: ctx => ctx.label + ': ' +
                    costsFmtN(ctx.parsed) + ' calls' } } } },
    });
}

function costsRenderProjectChart(data) {
    const canvas = document.getElementById('costs-mode-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('mode');
    const rows = (data.by_project || []).slice(0, 10);
    const labels = rows.map(r => r.project);
    const data_arr = rows.map(r => r.cost_usd || 0);
    costsState.charts.mode = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: { labels, datasets: [{
            label: 'cost', data: data_arr, backgroundColor: '#3fb950',
        }]},
        options: {
            indexAxis: 'y', responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false },
                tooltip: { callbacks: { label: ctx => costsFmtCost(ctx.parsed.x) } } },
            scales: {
                x: { ticks: { color: '#8b949e', callback: costsFmtCost },
                     grid: { color: '#21262d' } },
                y: { ticks: { color: '#e6edf3' }, grid: { display: false } },
            },
        },
    });
}

// ---- Tables ----
function costsRenderModelTable(shape, data) {
    const rows = (data.by_model || []).filter(_costsFilterModelRow);
    const wrap = document.getElementById('costs-model-table');
    if (!wrap) return;
    if (rows.length === 0) {
        wrap.innerHTML = '<div class="empty-state">No model data in this filter.</div>';
        return;
    }
    let html;
    if (shape === 'claude_code') {
        html = '<table class="data-table costs-table"><thead><tr>' +
            '<th>Model</th><th class="num">Turns</th><th class="num">Input</th>' +
            '<th class="num">Output</th><th class="num">Cache read</th>' +
            '<th class="num">Cache write</th><th class="num">Cost</th>' +
            '</tr></thead><tbody>';
        for (const r of rows) {
            html += `<tr>
                <td><code class="costs-model-name">${costsEsc(r.model)}</code></td>
                <td class="num">${costsFmtN(r.turns)}</td>
                <td class="num">${costsFmtN(r.input_tokens)}</td>
                <td class="num">${costsFmtN(r.output_tokens)}</td>
                <td class="num">${costsFmtN(r.cache_read_tokens)}</td>
                <td class="num">${costsFmtN(r.cache_creation_tokens)}</td>
                <td class="num">${costsFmtCost(r.cost_usd)}</td>
            </tr>`;
        }
    } else {
        // "Cache hits" here is the work-buddy-side LLM-cache count
        // (work_buddy.llm.cache, an in-process result cache). Distinct from
        // the Anthropic prompt-cache tokens displayed in the cards above.
        html = '<table class="data-table costs-table"><thead><tr>' +
            '<th>Model</th><th class="num">Calls</th><th class="num">API</th>' +
            '<th class="num">Cache hits</th><th class="num">Input</th>' +
            '<th class="num">Output</th><th class="num">Cost</th>' +
            '</tr></thead><tbody>';
        for (const r of rows) {
            html += `<tr>
                <td><code class="costs-model-name">${costsEsc(r.model)}</code></td>
                <td class="num">${costsFmtN(r.calls)}</td>
                <td class="num">${costsFmtN(r.api_calls)}</td>
                <td class="num">${costsFmtN(r.cache_hits)}</td>
                <td class="num">${costsFmtN(r.input_tokens)}</td>
                <td class="num">${costsFmtN(r.output_tokens)}</td>
                <td class="num">${costsFmtCost(r.cost_usd)}</td>
            </tr>`;
        }
    }
    html += '</tbody></table>';
    wrap.innerHTML = html;
}

// Column definitions per shape — drives both the header (with sort
// indicators) and the per-row rendering. Each column has:
//   key:   field name on a session row (or null for the action column)
//   label: visible header text
//   num:   true → right-aligned number column
//   sort:  the sort comparator key on the session record (null = unsortable)
//   render(s) → returns the <td>...</td> string for this column on row ``s``
function _costsSessionColumns(shape) {
    const ccCols = [
        { key: 'short_id',  label: 'Session',  sort: 'short_id',
          render: s => `<td><code title="${costsEsc(s.session_id)}">${costsEsc(s.short_id)}</code></td>` },
        { key: 'project',   label: 'Project',  sort: 'project',
          render: s => `<td title="${costsEsc(s.project)}">${costsEsc(s.project)}</td>` },
        { key: 'branch',    label: 'Branch',   sort: 'branch',
          render: s => `<td class="costs-branch-cell" title="${costsEsc(s.branch || '')}">${costsEsc(s.branch || '')}</td>` },
        { key: 'last',      label: 'Last',     sort: 'last',
          render: s => `<td>${costsFmtDate(s.last)}</td>` },
        { key: 'turns',     label: 'Turns', num: true, sort: 'turns',
          render: s => `<td class="num">${costsFmtN(s.turns)}</td>` },
        { key: 'input_tokens', label: 'In', num: true, sort: 'input_tokens',
          render: s => `<td class="num">${costsFmtN(s.input_tokens)}</td>` },
        { key: 'output_tokens', label: 'Out', num: true, sort: 'output_tokens',
          render: s => `<td class="num">${costsFmtN(s.output_tokens)}</td>` },
        { key: 'cache_read_tokens', label: 'Cache R', num: true, sort: 'cache_read_tokens',
          render: s => `<td class="num">${costsFmtN(s.cache_read_tokens)}</td>` },
        { key: 'cost_usd',  label: 'Cost', num: true, sort: 'cost_usd',
          render: s => `<td class="num">${costsFmtCost(s.cost_usd)}</td>` },
        { key: 'model',     label: 'Model',    sort: null,
          render: s => `<td class="costs-model-cell">${s.model
              ? `<span class="costs-model-chip">${costsEsc(s.model)}</span>` : ''}</td>` },
    ];
    const intCols = [
        { key: 'short_id',  label: 'Session',  sort: 'short_id',
          render: s => `<td><code title="${costsEsc(s.session_id)}">${costsEsc(s.short_id)}</code></td>` },
        { key: 'project',   label: 'Project',  sort: 'project',
          render: s => {
              const p = s.project ? s.project.replace(/^.*[\\\/]/, '') : '';
              return `<td title="${costsEsc(s.project)}">${costsEsc(p)}</td>`;
          } },
        { key: 'last',      label: 'Last',     sort: 'last',
          render: s => `<td>${costsFmtDate(s.last)}</td>` },
        { key: 'calls',     label: 'Calls', num: true, sort: 'calls',
          render: s => `<td class="num">${costsFmtN(s.calls)}</td>` },
        { key: 'input_tokens', label: 'In', num: true, sort: 'input_tokens',
          render: s => `<td class="num">${costsFmtN(s.input_tokens)}</td>` },
        { key: 'output_tokens', label: 'Out', num: true, sort: 'output_tokens',
          render: s => `<td class="num">${costsFmtN(s.output_tokens)}</td>` },
        { key: 'cost_usd',  label: 'Cost', num: true, sort: 'cost_usd',
          render: s => `<td class="num">${costsFmtCost(s.cost_usd)}</td>` },
        { key: 'models',    label: 'Models',   sort: null,
          render: s => `<td class="costs-model-cell">${(s.models || []).map(m =>
              `<span class="costs-model-chip">${costsEsc(m)}</span>`).join('')}</td>` },
    ];
    return shape === 'claude_code' ? ccCols : intCols;
}

function costsSortBy(key) {
    if (!key) return;
    const cur = costsState.sessionSort || { key: 'last', dir: 'desc' };
    if (cur.key === key) {
        costsState.sessionSort = { key, dir: cur.dir === 'asc' ? 'desc' : 'asc' };
    } else {
        // Numeric / date columns default to desc; text columns to asc.
        const textCols = new Set(['short_id', 'project', 'branch']);
        costsState.sessionSort = { key, dir: textCols.has(key) ? 'asc' : 'desc' };
    }
    costsRenderSessionsTable(
        costsState.shape || 'internal',
        _costsActiveData().data,
    );
}

function _costsSortSessions(rows) {
    const { key, dir } = costsState.sessionSort || { key: 'last', dir: 'desc' };
    const sign = dir === 'asc' ? 1 : -1;
    return rows.slice().sort((a, b) => {
        let va = a[key], vb = b[key];
        if (va == null) va = '';
        if (vb == null) vb = '';
        if (typeof va === 'number' && typeof vb === 'number') {
            return sign * (va - vb);
        }
        return sign * String(va).localeCompare(String(vb));
    });
}

// Reusable pager renderer.
//   ariaTotal — total rows.
//   onPage(n) — global function name (string) called with the new page number.
function _costsRenderPager(containerId, total, currentPage, pageSize, onPageFn) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (total <= pageSize) { el.innerHTML = ''; return; }
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const cur = Math.min(Math.max(currentPage, 1), totalPages);
    const startIdx = (cur - 1) * pageSize + 1;
    const endIdx = Math.min(cur * pageSize, total);

    function pageBtn(n, label, opts) {
        opts = opts || {};
        const classes = ['costs-pager-btn'];
        if (opts.current) classes.push('current');
        const disabled = opts.disabled ? ' disabled' : '';
        const onClick = opts.disabled ? '' : ` onclick="${onPageFn}(${n})"`;
        return `<button class="${classes.join(' ')}"${disabled}${onClick}>${costsEsc(label)}</button>`;
    }

    let html = '';
    html += pageBtn(cur - 1, '‹', { disabled: cur === 1 });

    // Show first/last and a sliding window of ±2 around current.
    const pages = new Set([1, totalPages, cur, cur - 1, cur + 1, cur - 2, cur + 2]);
    const visible = Array.from(pages)
        .filter(n => n >= 1 && n <= totalPages)
        .sort((a, b) => a - b);
    let prev = 0;
    for (const n of visible) {
        if (n - prev > 1) html += '<span class="costs-pager-ellipsis">…</span>';
        html += pageBtn(n, String(n), { current: n === cur });
        prev = n;
    }
    html += pageBtn(cur + 1, '›', { disabled: cur === totalPages });
    html += `<span class="costs-pager-info">${startIdx}–${endIdx} of ${total}</span>`;
    el.innerHTML = html;
}

function costsSessionsGoToPage(n) {
    costsState.sessionPage = n;
    costsRenderSessionsTable(
        costsState.shape || 'internal',
        _costsActiveData().data,
    );
}

function costsRenderSessionsTable(shape, data) {
    const all = (data.sessions || []).filter(_costsFilterSession);

    const countEl = document.getElementById('costs-sessions-count');
    if (countEl) countEl.textContent = all.length === 1
        ? '1 session'
        : `${all.length} sessions`;

    const wrap = document.getElementById('costs-sessions-table');
    if (!wrap) return;
    if (all.length === 0) {
        wrap.innerHTML = '<div class="empty-state">No sessions match the current filters.</div>';
        const pager = document.getElementById('costs-sessions-pager');
        if (pager) pager.innerHTML = '';
        return;
    }

    const sorted = _costsSortSessions(all);
    const pageSize = COSTS_PAGE_SIZE;
    const page = Math.min(Math.max(costsState.sessionPage || 1, 1),
                          Math.max(1, Math.ceil(sorted.length / pageSize)));
    const slice = sorted.slice((page - 1) * pageSize, page * pageSize);

    const cols = _costsSessionColumns(shape);
    const cur = costsState.sessionSort || { key: 'last', dir: 'desc' };

    // Header row with sort affordances.
    let html = '<table class="data-table costs-table"><thead><tr>';
    for (const c of cols) {
        const sortable = c.sort != null;
        const isActive = sortable && cur.key === c.sort;
        const arrow = isActive
            ? (cur.dir === 'asc' ? '▲' : '▼')
            : '↕';
        const cls = [
            sortable ? 'sortable' : '',
            isActive ? 'sort-active' : '',
            c.num ? 'num' : '',
        ].filter(Boolean).join(' ');
        const onClick = sortable ? ` onclick="costsSortBy('${costsEsc(c.sort)}')"` : '';
        const arrowSpan = sortable ? `<span class="sort-arrow">${arrow}</span>` : '';
        html += `<th class="${cls}"${onClick}>${costsEsc(c.label)}${arrowSpan}</th>`;
    }
    html += '</tr></thead><tbody>';

    for (const s of slice) {
        html += '<tr>';
        for (const c of cols) html += c.render(s);
        html += '</tr>';
    }

    html += '</tbody></table>';
    wrap.innerHTML = html;

    _costsRenderPager('costs-sessions-pager', sorted.length, page, pageSize,
                       'costsSessionsGoToPage');

    // Reset to page 1 if the current page is now beyond the data.
    costsState.sessionPage = page;
}

"""
