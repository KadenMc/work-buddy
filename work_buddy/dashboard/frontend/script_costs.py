"""Dashboard Costs tab JS — fetches /api/costs and renders charts/tables.

Charting via Chart.js v4.4.0, vendored at
``work_buddy/dashboard/frontend/vendor/chart.umd.min.js`` and served by
the Flask ``/static/<path>`` route in ``service.py``.

UI structure follows ``claude-usage`` (MIT, Pawel Huryn) but is fully
re-expressed in work-buddy's existing CSS variables — no franken-styling
across the dashboard. Token-color encoding intentionally matches
``claude-usage`` so any future cross-comparison stays visually coherent.
"""

from __future__ import annotations


def _costs_script() -> str:
    return r"""
// ---- Costs tab state ----
let costsState = {
    raw: null,                    // last /api/costs response
    selectedModels: null,         // null = all
    range: '30',                  // '7' | '30' | '90' | 'all'
    mode: 'all',                  // 'all' | 'cloud' | 'local'
    source: 'internal',           // 'internal' | 'claude_code' | 'all'
    sessionFilter: '',
    sessionSort: 'last',
    charts: {},                   // Chart.js instances keyed by canvas id
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

// ---- Fetch ----
async function loadCosts(force) {
    const meta = document.getElementById('costs-meta');
    if (meta) meta.textContent = 'loading...';
    const url = '/api/costs?source=' + encodeURIComponent(costsState.source);
    const data = await fetchJSON(url);
    if (!data || data.error) {
        document.getElementById('costs-cards').innerHTML =
            '<div class="empty-state">Failed to load cost data: ' +
            costsEsc((data && data.error) || 'unknown error') + '</div>';
        if (meta) meta.textContent = '';
        return;
    }

    // Two response shapes:
    //   source=internal     → the internal shape directly
    //   source=claude_code  → the transcripts shape directly
    //   source=all          → {internal, claude_code, source:'all'}
    let primary = data;
    let secondary = null;
    if (data.source === 'all') {
        primary = data.internal || {};
        secondary = data.claude_code || null;
    }
    // Detect which shape we got; claude_code shape carries
    // ``source: 'claude_code'`` and may have ``available: false``.
    costsState.raw = primary;
    costsState.claudeCode = (data.source === 'all') ? secondary :
        (primary && primary.source === 'claude_code') ? primary : null;
    costsState.shape = (primary && primary.source === 'claude_code')
        ? 'claude_code' : 'internal';

    const allModels = primary && primary.all_models ? primary.all_models : [];
    if (costsState.selectedModels === null) {
        costsState.selectedModels = new Set(allModels);
    } else {
        for (const m of Array.from(costsState.selectedModels)) {
            if (!allModels.includes(m)) costsState.selectedModels.delete(m);
        }
    }
    costsRenderAll();

    if (meta) {
        const t = primary.totals || {};
        const sCount = primary.session_count || 0;
        const callsLabel = costsState.shape === 'claude_code'
            ? (t.turns || 0) + ' turns'
            : (t.calls || 0) + ' calls';
        meta.textContent = `${sCount} sessions · ${callsLabel} · ${costsFmtDate(primary.generated_at)}`;
    }
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

function costsSourceChanged(v) { costsState.source = v; loadCosts(true); }
function costsRangeChanged(v)  { costsState.range = v; costsRenderAll(); }
function costsModeChanged(v)   { costsState.mode = v; costsRenderAll(); }

// ---- Filter helpers ----
function _costsRangeStartIso() {
    if (costsState.range === 'all') return null;
    const days = parseInt(costsState.range, 10);
    if (!isFinite(days) || days <= 0) return null;
    const d = new Date();
    d.setDate(d.getDate() - days + 1);
    d.setHours(0, 0, 0, 0);
    return d.toISOString().slice(0, 10);
}

function _costsFilterSession(s) {
    // Range
    const start = _costsRangeStartIso();
    if (start && (s.last || '').slice(0, 10) < start) return false;
    // Model selection — internal sessions carry a `models` list; transcript
    // sessions carry a single `model`. Handle both.
    if (costsState.selectedModels && costsState.selectedModels.size > 0) {
        const sm = s.models || (s.model ? [s.model] : []);
        const overlap = sm.some(m => costsState.selectedModels.has(m));
        if (!overlap) return false;
    }
    return true;
}

function _costsFilterDay(d) {
    const start = _costsRangeStartIso();
    if (start && d.day < start) return false;
    return true;
}

function _costsFilterModelRow(r) {
    if (costsState.selectedModels && costsState.selectedModels.size > 0) {
        return costsState.selectedModels.has(r.model);
    }
    return true;
}

function _costsModeMatches(entry) {
    if (costsState.mode === 'all') return true;
    // We don't have a per-day per-mode shape in the read model, but
    // the by_execution_mode list lets us compute totals. Mode filtering
    // on aggregate slices (by_day, by_model) is best-effort: when a
    // user picks 'cloud only' we'll display the full daily series but
    // re-color the cards using the matching mode bucket. UX expectation
    // is documented in DECISIONS.md.
    return true;
}

// ---- Rendering ----
function costsRenderAll() {
    if (!costsState.raw) return;

    // If the user picked the claude_code source but no scan has run yet,
    // surface an explicit "Scan now" prompt instead of empty charts.
    if (costsState.shape === 'claude_code'
            && costsState.raw.available === false) {
        document.getElementById('costs-cards').innerHTML =
            `<div class="empty-state" style="padding:24px;text-align:center;">
                <div style="margin-bottom:12px;">${costsEsc(costsState.raw.message || 'No Claude Code usage cached yet.')}</div>
                <button class="chats-accent-btn"
                        onclick="costsRescanClaudeCode(this)">Rescan Claude Code</button>
            </div>`;
        document.getElementById('costs-models-filter').innerHTML = '';
        document.getElementById('costs-model-table').innerHTML = '';
        document.getElementById('costs-sessions-table').innerHTML = '';
        ['daily', 'model', 'task', 'mode'].forEach(_costsDestroyChart);
        return;
    }

    costsRenderModelsFilter();
    costsRenderCards();
    costsRenderDailyChart();
    costsRenderModelChart();
    const tt = document.getElementById('costs-task-title');
    const mt = document.getElementById('costs-mode-title');
    if (costsState.shape === 'claude_code') {
        if (tt) tt.textContent = 'Top tools (by turns)';
        if (mt) mt.textContent = 'Top projects (by cost)';
        costsRenderToolChart();
        costsRenderProjectChart();
    } else {
        if (tt) tt.textContent = 'Top callers (by cost)';
        if (mt) mt.textContent = 'Cloud vs Local mix';
        costsRenderTaskChart();
        costsRenderModeChart();
    }
    costsRenderModelTable();
    costsRenderSessionsTable();
}

function costsRenderModelsFilter() {
    const all = costsState.raw.all_models || [];
    const el = document.getElementById('costs-models-filter');
    if (!el) return;
    if (all.length === 0) {
        el.innerHTML = '<div class="empty-state" style="padding:8px 0;">No model data yet.</div>';
        return;
    }
    let html = '<span class="costs-filter-label">Models:</span>';
    html += '<button class="costs-filter-pill costs-filter-action" onclick="costsModelsAll()">all</button>';
    html += '<button class="costs-filter-pill costs-filter-action" onclick="costsModelsNone()">none</button>';
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
function costsModelsAll()  { costsState.selectedModels = new Set(costsState.raw.all_models || []); costsRenderAll(); }
function costsModelsNone() { costsState.selectedModels = new Set(); costsRenderAll(); }

function _costsAggregateByDayFiltered() {
    // Sum filtered by_day rows into a single totals dict, plus return
    // the filtered list itself for charting.
    const days = (costsState.raw.by_day || []).filter(_costsFilterDay);
    const totals = { calls: 0, api_calls: 0, cache_hits: 0,
                     cloud_calls: 0, local_calls: 0,
                     input_tokens: 0, output_tokens: 0, cost_usd: 0 };
    for (const d of days) {
        totals.calls          += d.calls || 0;
        totals.api_calls      += d.api_calls || 0;
        totals.cache_hits     += d.cache_hits || 0;
        totals.cloud_calls    += d.cloud_calls || 0;
        totals.local_calls    += d.local_calls || 0;
        totals.input_tokens   += d.input_tokens || 0;
        totals.output_tokens  += d.output_tokens || 0;
        totals.cost_usd       += d.cost_usd || 0;
    }
    return { days, totals };
}

function _costsAggregateByDayTranscripts() {
    const days = (costsState.raw.by_day || []).filter(_costsFilterDay);
    const totals = { turns: 0, input_tokens: 0, output_tokens: 0,
                     cache_read_tokens: 0, cache_creation_tokens: 0,
                     cost_usd: 0 };
    for (const d of days) {
        totals.turns               += d.turns || 0;
        totals.input_tokens        += d.input_tokens || 0;
        totals.output_tokens       += d.output_tokens || 0;
        totals.cache_read_tokens   += d.cache_read_tokens || 0;
        totals.cache_creation_tokens += d.cache_creation_tokens || 0;
        totals.cost_usd            += d.cost_usd || 0;
    }
    return { days, totals };
}

function costsRenderCards() {
    const wrap = document.getElementById('costs-cards');
    if (!wrap) return;

    if (costsState.shape === 'claude_code') {
        const { totals } = _costsAggregateByDayTranscripts();
        const sessionCount = (costsState.raw.sessions || []).filter(_costsFilterSession).length;
        const cards = [
            { label: 'Sessions',     value: sessionCount.toString() },
            { label: 'Turns',        value: costsFmtN(totals.turns) },
            { label: 'Input',        value: costsFmtN(totals.input_tokens) },
            { label: 'Output',       value: costsFmtN(totals.output_tokens) },
            { label: 'Cache read',   value: costsFmtN(totals.cache_read_tokens),
                sub: '90% off input rate' },
            { label: 'Cache write',  value: costsFmtN(totals.cache_creation_tokens),
                sub: '+25% premium' },
            { label: 'Est. cost',    value: costsFmtCost(totals.cost_usd),
                sub: 'Anthropic Apr 2026 rates' },
        ];
        wrap.innerHTML = cards.map(c => `
            <div class="card">
                <div class="card-label">${costsEsc(c.label)}</div>
                <div class="card-value">${costsEsc(c.value)}</div>
                ${c.sub ? `<div class="card-sub">${costsEsc(c.sub)}</div>` : ''}
            </div>
        `).join('');
        return;
    }

    const { totals } = _costsAggregateByDayFiltered();
    const sessionCount = (costsState.raw.sessions || []).filter(_costsFilterSession).length;
    const cards = [
        { label: 'Sessions',     value: sessionCount.toString() },
        { label: 'Calls',        value: costsFmtN(totals.calls),
            sub: `${totals.cloud_calls} cloud · ${totals.local_calls} local` },
        { label: 'Input tokens', value: costsFmtN(totals.input_tokens) },
        { label: 'Output tokens',value: costsFmtN(totals.output_tokens) },
    ];
    // Show cache cards only when the data is present (rows post-2026-04-25
    // carry these; legacy rows have them as 0).
    if ((totals.cache_read_tokens || 0) > 0 || (totals.cache_creation_tokens || 0) > 0) {
        cards.push(
            { label: 'Cache read',  value: costsFmtN(totals.cache_read_tokens),
                sub: '90% off input rate' },
            { label: 'Cache write', value: costsFmtN(totals.cache_creation_tokens),
                sub: '+25% premium' },
        );
    }
    cards.push({ label: 'Est. cost',    value: costsFmtCost(totals.cost_usd),
        sub: 'cloud only; local logs $0' });
    wrap.innerHTML = cards.map(c => `
        <div class="card">
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

function costsRenderDailyChart() {
    const canvas = document.getElementById('costs-daily-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('daily');

    const isTr = costsState.shape === 'claude_code';
    const { days } = isTr ? _costsAggregateByDayTranscripts()
                          : _costsAggregateByDayFiltered();
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
    // Cache datasets render whenever the data is present, not based on source.
    // Internal-log rows after 2026-04-25 carry cache token splits;
    // transcript rows always have them.
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

// Transcript-only: top 10 tools by turn count.
function costsRenderToolChart() {
    const canvas = document.getElementById('costs-task-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('task');
    const rows = (costsState.raw.by_tool || []).slice(0, 10);
    const labels = rows.map(r => r.tool);
    const data = rows.map(r => r.turns || 0);
    costsState.charts.task = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: { labels, datasets: [{
            label: 'turns', data, backgroundColor: '#4f8ef7',
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

// Transcript-only: top 10 projects by cost.
function costsRenderProjectChart() {
    const canvas = document.getElementById('costs-mode-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('mode');
    const rows = (costsState.raw.by_project || []).slice(0, 10);
    const labels = rows.map(r => r.project);
    const data = rows.map(r => r.cost_usd || 0);
    costsState.charts.mode = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: { labels, datasets: [{
            label: 'cost', data, backgroundColor: '#3fb950',
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

function costsRenderModelChart() {
    const canvas = document.getElementById('costs-model-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('model');
    const rows = (costsState.raw.by_model || []).filter(_costsFilterModelRow);
    const labels = rows.map(r => r.model);
    const data = rows.map(r => r.cost_usd || 0);
    if (labels.length === 0 || data.every(v => v === 0)) {
        // Fall back to token volume if no cloud cost recorded.
        const tokens = rows.map(r => (r.input_tokens || 0) + (r.output_tokens || 0));
        canvas.parentElement.dataset.fallback = 'tokens';
        costsState.charts.model = new Chart(canvas.getContext('2d'), {
            type: 'doughnut',
            data: { labels, datasets: [{ data: tokens,
                backgroundColor: labels.map((_, i) => COSTS_MODEL_PALETTE[i % COSTS_MODEL_PALETTE.length]) }] },
            options: { responsive: true, maintainAspectRatio: false,
                plugins: { legend: { position: 'right',
                    labels: { color: '#e6edf3', boxWidth: 12 } },
                    tooltip: { callbacks: { label: ctx => ctx.label + ': ' +
                        costsFmtN(ctx.parsed) + ' tokens (no cloud cost)' } } } },
        });
        return;
    }
    canvas.parentElement.dataset.fallback = '';
    costsState.charts.model = new Chart(canvas.getContext('2d'), {
        type: 'doughnut',
        data: { labels, datasets: [{ data,
            backgroundColor: labels.map((_, i) => COSTS_MODEL_PALETTE[i % COSTS_MODEL_PALETTE.length]) }] },
        options: { responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: 'right',
                labels: { color: '#e6edf3', boxWidth: 12 } },
                tooltip: { callbacks: { label: ctx => ctx.label + ': ' +
                    costsFmtCost(ctx.parsed) } } } },
    });
}

function costsRenderTaskChart() {
    const canvas = document.getElementById('costs-task-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('task');
    const rows = (costsState.raw.by_task || []).slice(0, 10);
    const labels = rows.map(r => r.task);
    const data = rows.map(r => r.cost_usd || 0);
    const tokens = rows.map(r => (r.input_tokens || 0) + (r.output_tokens || 0));
    const useTokens = data.every(v => v === 0);
    costsState.charts.task = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: useTokens ? 'tokens' : 'cost (USD)',
                data: useTokens ? tokens : data,
                backgroundColor: '#D87857',
            }],
        },
        options: {
            indexAxis: 'y', responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false },
                tooltip: { callbacks: { label: ctx => useTokens ?
                    costsFmtN(ctx.parsed.x) + ' tokens' :
                    costsFmtCost(ctx.parsed.x) } } },
            scales: {
                x: { ticks: { color: '#8b949e',
                    callback: useTokens ? costsFmtN : costsFmtCost },
                    grid: { color: '#21262d' } },
                y: { ticks: { color: '#e6edf3' }, grid: { display: false } },
            },
        },
    });
}

function costsRenderModeChart() {
    const canvas = document.getElementById('costs-mode-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('mode');
    const rows = costsState.raw.by_execution_mode || [];
    const labels = rows.map(r => r.mode || 'unknown');
    const calls = rows.map(r => r.calls || 0);
    const colors = labels.map(l => l === 'cloud' ? '#D87857' :
                                    l === 'local' ? '#3fb950' : '#6e7681');
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

function costsRenderModelTable() {
    const rows = (costsState.raw.by_model || []).filter(_costsFilterModelRow);
    const wrap = document.getElementById('costs-model-table');
    if (!wrap) return;
    if (rows.length === 0) {
        wrap.innerHTML = '<div class="empty-state">No model data in this filter.</div>';
        return;
    }
    const isTr = costsState.shape === 'claude_code';
    let html;
    if (isTr) {
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
        html += '</tbody></table>';
        wrap.innerHTML = html;
        return;
    }
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
    html += '</tbody></table>';
    wrap.innerHTML = html;
}

function costsRenderSessionsTable() {
    const all = (costsState.raw.sessions || []).filter(_costsFilterSession);
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

    const isTr = costsState.shape === 'claude_code';
    let html;
    if (isTr) {
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
        html += '</tbody></table>';
        wrap.innerHTML = html;
        return;
    }

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
    html += '</tbody></table>';
    wrap.innerHTML = html;
}

// ---- Wire-up ----
document.addEventListener('DOMContentLoaded', function() {
    const f = document.getElementById('costs-session-filter');
    if (f) {
        f.addEventListener('input', function() {
            costsState.sessionFilter = f.value || '';
            costsRenderSessionsTable();
        });
    }
});
"""
