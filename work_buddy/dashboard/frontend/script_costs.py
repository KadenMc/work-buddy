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
let costsState = {
    raw: null,                    // last /api/costs?source=all response
    project: '',                  // '' = all projects, else exact project name
    activity: 'all',              // 'all' | 'claude_code' | 'api' | 'local'
                                  // (only meaningful when project === 'work-buddy')
    range: '30',                  // 'today' | '7' | '30' | '90' | 'all'
    selectedModels: null,         // null = all selected; Set of strings otherwise
    sessionFilter: '',
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
    costsState.activity = v;
    document.querySelectorAll('#costs-activity-pills .costs-pill').forEach(b => {
        b.classList.toggle('active', b.dataset.activity === v);
    });
    costsRenderAll();
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
//       'all'         → claude_code (Claude Code dominates; internal stats
//                       still surface as sub-text on cards)
//       'claude_code' → claude_code
//       'api'         → internal, with cards filtered to cloud
//       'local'       → internal, with cards filtered to local
//   - project=other          → claude_code
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
    // api / local — internal source.
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
        // shape === 'internal' (work-buddy + activity = api or local)
        // Top callers chart only meaningful for the work-buddy infra view.
        if (taskCard) taskCard.style.display = isWB ? '' : 'none';
        if (modeCard) modeCard.style.display = isWB ? '' : 'none';
        if (isWB) {
            if (tt) tt.textContent = 'Top callers (by cost)';
            costsRenderTaskChart(data, activity);
            if (mt) mt.textContent = 'Cloud vs Local mix';
            costsRenderModeChart(data, activity);
        } else {
            _costsDestroyChart('task');
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

    // Apply activity-mode filtering for the internal shape.
    let displayed = totals;
    if (shape === 'internal' && activity === 'api') {
        displayed = _costsFilterTotalsByMode(totals, 'cloud');
    } else if (shape === 'internal' && activity === 'local') {
        displayed = _costsFilterTotalsByMode(totals, 'local');
    }

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
        if (activity === 'api')   sub = `${displayed.cloud_calls || 0} cloud`;
        else if (activity === 'local') sub = `${displayed.local_calls || 0} local`;
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

function _costsFilterTotalsByMode(totals, mode) {
    // For internal shape: rough activity filter over already-summed
    // totals. We only know cloud_calls / local_calls; tokens are
    // mixed. The cards still render, but the displayed numbers are
    // approximate when activity≠all (the underlying log doesn't track
    // per-call mode-tagged tokens). Caveat surfaced via the cost-card
    // sub-text.
    if (mode === 'cloud') {
        return {
            ...totals,
            calls: totals.cloud_calls || 0,
            local_calls: 0,
        };
    }
    if (mode === 'local') {
        return {
            ...totals,
            calls: totals.local_calls || 0,
            cloud_calls: 0,
            cost_usd: 0,
        };
    }
    return totals;
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

function costsRenderSessionsTable(shape, data) {
    const all = (data.sessions || []).filter(_costsFilterSession);
    const filterStr = (costsState.sessionFilter || '').toLowerCase();
    const filtered = filterStr ? all.filter(s => {
        return (s.short_id || '').toLowerCase().includes(filterStr)
            || (s.session_id || '').toLowerCase().includes(filterStr)
            || (s.project || '').toLowerCase().includes(filterStr)
            || (s.directory || '').toLowerCase().includes(filterStr)
            || (s.branch || '').toLowerCase().includes(filterStr);
    }) : all;

    const countEl = document.getElementById('costs-sessions-count');
    if (countEl) countEl.textContent = `${filtered.length} of ${all.length} sessions`;

    const wrap = document.getElementById('costs-sessions-table');
    if (!wrap) return;
    if (filtered.length === 0) {
        wrap.innerHTML = '<div class="empty-state">No sessions match.</div>';
        return;
    }

    let html;
    if (shape === 'claude_code') {
        html = '<table class="data-table costs-table"><thead><tr>' +
            '<th>Session</th><th>Project</th><th>Branch</th><th>Last</th>' +
            '<th class="num">Turns</th><th class="num">In</th>' +
            '<th class="num">Out</th><th class="num">Cache R</th>' +
            '<th class="num">Cost</th><th>Model</th>' +
            '</tr></thead><tbody>';
        for (const s of filtered.slice(0, 200)) {
            html += `<tr>
                <td><code title="${costsEsc(s.session_id)}">${costsEsc(s.short_id)}</code></td>
                <td title="${costsEsc(s.project)}">${costsEsc(s.project)}</td>
                <td>${costsEsc(s.branch || '')}</td>
                <td>${costsFmtDate(s.last)}</td>
                <td class="num">${costsFmtN(s.turns)}</td>
                <td class="num">${costsFmtN(s.input_tokens)}</td>
                <td class="num">${costsFmtN(s.output_tokens)}</td>
                <td class="num">${costsFmtN(s.cache_read_tokens)}</td>
                <td class="num">${costsFmtCost(s.cost_usd)}</td>
                <td class="costs-model-cell">${s.model ?
                    `<span class="costs-model-chip">${costsEsc(s.model)}</span>` : ''}</td>
            </tr>`;
        }
        if (filtered.length > 200) {
            html += `<tr><td colspan="10" class="empty-state">Showing first 200 of ${filtered.length}</td></tr>`;
        }
    } else {
        html = '<table class="data-table costs-table"><thead><tr>' +
            '<th>Session</th><th>Project</th><th>Last</th>' +
            '<th class="num">Calls</th><th class="num">In</th>' +
            '<th class="num">Out</th><th class="num">Cost</th><th>Models</th>' +
            '</tr></thead><tbody>';
        for (const s of filtered.slice(0, 200)) {
            const proj = s.project ? s.project.replace(/^.*[\\\/]/, '') : '';
            html += `<tr>
                <td><code title="${costsEsc(s.session_id)}">${costsEsc(s.short_id)}</code></td>
                <td title="${costsEsc(s.project)}">${costsEsc(proj)}</td>
                <td>${costsFmtDate(s.last)}</td>
                <td class="num">${costsFmtN(s.calls)}</td>
                <td class="num">${costsFmtN(s.input_tokens)}</td>
                <td class="num">${costsFmtN(s.output_tokens)}</td>
                <td class="num">${costsFmtCost(s.cost_usd)}</td>
                <td class="costs-model-cell">${(s.models || []).map(m =>
                    `<span class="costs-model-chip">${costsEsc(m)}</span>`).join('')}</td>
            </tr>`;
        }
        if (filtered.length > 200) {
            html += `<tr><td colspan="8" class="empty-state">Showing first 200 of ${filtered.length}</td></tr>`;
        }
    }
    html += '</tbody></table>';
    wrap.innerHTML = html;
}

// ---- Wire-up ----
document.addEventListener('DOMContentLoaded', function() {
    const f = document.getElementById('costs-session-filter');
    if (f) {
        f.addEventListener('input', function() {
            costsState.sessionFilter = f.value || '';
            costsRenderSessionsTable(
                costsState.shape || 'internal',
                _costsActiveData().data,
            );
        });
    }
});
"""
