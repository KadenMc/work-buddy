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


def script() -> str:
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
    knownModels: [],              // full model list captured on the most recent
                                  // unfiltered fetch — survives refetches that
                                  // include a ``models`` filter (which would
                                  // otherwise shrink ``data.all_models``).
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
    if (c < 100)  return '$' + c.toFixed(2);
    // Past $100, cents are noise; show whole dollars with thousands separator.
    return '$' + Math.round(c).toLocaleString();
}
// Parse an ISO timestamp into a Date.
//
// Pre-2026-04-26 cost-log rows from work-buddy used ``datetime.now()``
// which produces TZ-naive ISO strings ("2026-04-25T10:00:00.123456").
// JavaScript's ``new Date(string)`` treats TZ-less ISO as **local
// time**, but the writer intended UTC — so any reload on a non-UTC
// machine would show stale "Last activity" times. From 2026-04-26
// onward we write UTC with explicit offset; for legacy rows we append
// "Z" defensively so they parse as UTC.
function _costsParseTs(s) {
    if (!s) return null;
    const hasTz = /([Zz]|[+-]\d{2}:?\d{2})$/.test(s);
    const d = new Date(hasTz ? s : s + 'Z');
    return isFinite(d.getTime()) ? d : null;
}

// Human-readable timestamp formatter — adopted from the Chats tab's
// ``formatTimestamp`` (core/page.py) so the two views agree on
// "Today HH:MM / Yesterday HH:MM / Mon DD HH:MM" output.
function costsFmtDate(s) {
    if (!s) return '\u2014';
    const d = _costsParseTs(s);
    if (!d) return s;
    const now = new Date();
    const time = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    if (d.toDateString() === now.toDateString()) return 'Today ' + time;
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (d.toDateString() === yesterday.toDateString()) return 'Yesterday ' + time;
    const month = d.toLocaleString('default', {month: 'short'});
    return month + ' ' + d.getDate() + ' ' + time;
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
    _costsSyncToolbar();
}

// Mirror costsState back into the toolbar widgets (range select, activity
// pills row + active pill). Needed when state was set programmatically —
// most notably the URL-hash restore on initial page load.
function _costsSyncToolbar() {
    const rangeSel = document.getElementById('costs-range');
    if (rangeSel && costsState.range) rangeSel.value = costsState.range;
    _costsSyncActivityVisibility();  // pill visibility + .active class
}

// ---- Fetch ----
async function loadCosts(force) {
    await costsLoadProjects();
    // Refresh the rate-limit chip alongside the cost data — they're
    // both "what's happening with my Claude usage right now" indicators.
    _costsLoadRateLimits();

    const meta = document.getElementById('costs-meta');
    if (meta) meta.textContent = 'loading...';

    // Initial load uses no models filter — we need the full model list
    // back so the chip rail can be built.
    const params = _costsBuildParams({includeModels: false});

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

    // Capture the full unfiltered model list so chip rendering survives
    // future refetches that include a ``models`` filter (which would
    // shrink ``data.all_models`` to the narrowed subset).
    const all = _costsCurrentModelsFromActiveData();
    costsState.knownModels = all.slice();

    // Reset selectedModels to all-known when refetching (new project may
    // expose a different model set).
    costsState.selectedModels = new Set(all);

    costsRenderAll();
}

// ---- Toolbar handlers ----
function costsProjectChanged(v) {
    costsState.project = v || '';
    // Reset activity to "all" whenever the project changes.
    costsState.activity = 'all';
    _costsSyncActivityVisibility();
    if (typeof _persistHash === 'function') _persistHash();
    loadCosts(true);
}
function costsRangeChanged(v) {
    costsState.range = v;
    if (typeof _persistHash === 'function') _persistHash();
    // Range now applies at the backend so every aggregate (by_task,
    // by_model, sessions, totals, ...) reflects the same window.
    // Refetching is the right call.
    loadCosts(true);
}
function costsActivityChanged(v) {
    const prev = costsState.activity;
    costsState.activity = v;
    _costsRenderActivityFilter();
    if (typeof _persistHash === 'function') _persistHash();
    // api / local require a backend refetch (execution_mode filter
    // applies at row level — by_model / sessions need to be re-aggregated).
    // Switching back to claude_code / all / programmatic doesn't need a
    // refetch as long as we already have the all-source response.
    const needsRefetch = (v === 'api' || v === 'local')
                       || (prev === 'api' || prev === 'local');
    if (needsRefetch) loadCosts(true);
    else costsRenderAll();
}

// Activity selector via the shared wbRenderFilters widget (single-select
// segmented). costsState.activity is the source of truth.
function _costsRenderActivityFilter() {
    wbRenderFilters('costs-activity-filter', {
        id: 'costs-activity-filter',
        mode: 'single',
        variant: 'segmented',
        groups: [{ key: 'activity', label: 'Activity', options: [
            { value: 'all', label: 'All' },
            { value: 'claude_code', label: 'Claude Code' },
            { value: 'programmatic', label: 'Programmatic',
              title: "work-buddy's runner activity — API + Local combined" },
            { value: 'api', label: 'API' },
            { value: 'local', label: 'Local' },
        ] }],
        getSelected: '_costsGetActivity',
        onChange: '_costsOnActivityChange',
    });
}
function _costsGetActivity(key) { return costsState.activity; }
function _costsOnActivityChange(key, value) { costsActivityChanged(value); }

function _costsSyncActivityVisibility() {
    const row = document.getElementById('costs-activity-row');
    if (!row) return;
    const isWB = (costsState.project || '').toLowerCase() === 'work-buddy';
    row.style.display = isWB ? '' : 'none';
    // Repaint the rail — keeps the active pill in sync when state was set
    // programmatically (e.g. URL hash restore) without a pill click.
    _costsRenderActivityFilter();
}

// Manual refresh: kick the Claude Code transcript scanner first, then
// reload. Shared by the toolbar Refresh button and the empty-state CTA.
// Without the rescan, ``loadCosts`` only re-fetches ``/api/costs`` and
// renders the same cached numbers — clicking Refresh would be a no-op
// against a stale cache.
//
// Read-only mode 403s the rescan endpoint — fall through to a plain
// reload rather than surfacing an error; the user can still see whatever
// is already cached.
async function costsRefresh(btn) {
    const originalLabel = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing...'; }
    try {
        const r = await fetch('/api/costs/rescan', { method: 'POST' });
        if (r.status !== 403) await r.json();
    } catch (e) {
        console.error('rescan failed', e);
    }
    if (btn) {
        btn.disabled = false;
        if (originalLabel !== null) btn.textContent = originalLabel;
    }
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

// Active-data view of all_models — used by loadCosts to seed
// ``knownModels`` from the response and as a fallback when no snapshot
// has been taken yet.
function _costsCurrentModelsFromActiveData() {
    const { data } = _costsActiveData();
    return data.all_models || [];
}

// Stable list for chip rendering / family grouping. Prefers the
// snapshot taken on the last unfiltered fetch (so chips don't shrink
// when we refetch with a ``models`` filter). Falls back to the active
// response's all_models when no snapshot exists yet.
function _costsCurrentModels() {
    if (costsState.knownModels && costsState.knownModels.length > 0) {
        return costsState.knownModels;
    }
    return _costsCurrentModelsFromActiveData();
}

// Shared URL-params builder for /api/costs fetches.
//
// ``includeModels``: when true and the user's chip selection narrows
// the known set, attach a comma-separated ``models`` query param so
// the backend filters every aggregate (cards, charts, top-callers,
// sessions). When false (initial load), we want the full all_models
// list back, so we omit the filter.
function _costsBuildParams(opts) {
    const params = new URLSearchParams();
    params.set('source', 'all');
    if (costsState.project) params.set('project', costsState.project);
    // For api / local activity, ask the backend to filter rows by
    // execution_mode so by_model / sessions / etc. are properly sliced.
    if (costsState.activity === 'api')   params.set('execution_mode', 'cloud');
    if (costsState.activity === 'local') params.set('execution_mode', 'local');
    const _startIso = _costsRangeStartIso();
    if (_startIso) params.set('start_date', _startIso);
    if (opts && opts.includeModels && costsState.selectedModels) {
        const known = costsState.knownModels || [];
        const sel = costsState.selectedModels;
        // Attach when narrowed — including the all-deselected case
        // (``models=`` with no value), which the backend reads as
        // "match nothing." Skipping it would silently revert to all-time.
        if (known.length > 0 && sel.size < known.length) {
            params.set('models', [...sel].join(','));
        }
    }
    return params;
}

// ---- Data-only refresh (Decision 1(b)) ----
//
// Auto-refresh hook for the Costs tab. Re-fetches /api/costs and re-renders
// data regions, but skips the models-filter chip rebuild (which would drop
// any in-progress hover/click on a chip) and preserves the user's
// selectedModels selection. Toolbar widgets — project select, range select,
// activity pills — are untouched here; they only mutate via their own
// onchange handlers.
async function refreshCostsData() {
    const meta = document.getElementById('costs-meta');
    if (meta) meta.textContent = 'loading...';
    // Refresh rate-limit chip in lockstep — same "what's happening now"
    // theme; lets the chip pick up observations from background runner
    // calls between dashboard interactions.
    _costsLoadRateLimits();

    // includeModels=true so an active chip narrowing propagates to
    // every backend aggregate (cards, charts, top-callers, sessions).
    const params = _costsBuildParams({includeModels: true});

    const data = await fetchJSON('/api/costs?' + params.toString());
    if (!data || data.error) {
        if (meta) meta.textContent = '';
        return;
    }
    costsState.raw = data;
    _costsSyncActivityVisibility();
    // Note: do NOT reset costsState.selectedModels — the user's chip
    // selection is sticky across auto-refreshes. Don't update
    // ``knownModels`` either: when the request had a ``models`` filter,
    // the response's all_models is narrowed.
    costsRenderAll({skipModelsFilter: true});
}

// ---- Rate-limit chip + popover ----
//
// Reads the on-disk observations (one per model) populated by the
// runner from anthropic-ratelimit-* response headers. Renders a single
// compact chip in the toolbar showing the most-restrictive headroom
// across recently-observed models. Click expands a per-model popover
// with a help section (everyone reading this for the first time gets
// the full explainer).

let _rlObservations = {};
let _rlPopoverOpen = false;
const _RL_STALE_MS = 5 * 60 * 1000;  // observations older than 5 min = idle
const _RL_PRESSURE_PCT = 30;          // amber threshold
const _RL_HOT_PCT = 10;               // red threshold

async function _costsLoadRateLimits() {
    const data = await fetchJSON('/api/costs/rate-limits');
    _rlObservations = (data && data.observations) || {};
    _costsRenderRateChip();
    if (_rlPopoverOpen) _costsRenderRatePopover();
}

function _rlAge(obs) {
    if (!obs || !obs.observed_at) return Infinity;
    const t = new Date(obs.observed_at).getTime();
    return isFinite(t) ? Date.now() - t : Infinity;
}

function _rlAgeLabel(ms) {
    if (!isFinite(ms)) return 'never';
    const s = Math.round(ms / 1000);
    if (s < 60)   return s + 's ago';
    if (s < 3600) return Math.round(s / 60) + 'm ago';
    return Math.round(s / 3600) + 'h ago';
}

function _rlLowestPct(obs) {
    // Returns lowest "remaining/limit" percentage across requests/input/
    // output dims (skip combined; usually redundant). null if no data.
    let lowest = null;
    for (const dim of ['requests', 'input_tokens', 'output_tokens']) {
        const d = obs && obs[dim];
        if (d && d.limit && d.remaining != null) {
            const pct = (d.remaining / d.limit) * 100;
            if (lowest === null || pct < lowest) lowest = pct;
        }
    }
    return lowest;
}

function _costsRenderRateChip() {
    const chip = document.getElementById('costs-rate-chip');
    const pctEl = document.getElementById('costs-rate-pct');
    if (!chip || !pctEl) return;

    const models = Object.keys(_rlObservations);
    if (models.length === 0) {
        chip.style.display = 'none';
        return;
    }
    chip.style.display = '';

    let lowest = null;
    let allStale = true;
    for (const model of models) {
        const obs = _rlObservations[model];
        if (_rlAge(obs) > _RL_STALE_MS) continue;
        allStale = false;
        const p = _rlLowestPct(obs);
        if (p !== null && (lowest === null || p < lowest)) lowest = p;
    }

    chip.classList.remove('warn', 'hot', 'stale');
    if (allStale || lowest === null) {
        chip.classList.add('stale');
        pctEl.textContent = '—';
        chip.title = 'No recent rate-limit observations. Click for details.';
    } else {
        if (lowest < _RL_HOT_PCT) chip.classList.add('hot');
        else if (lowest < _RL_PRESSURE_PCT) chip.classList.add('warn');
        pctEl.textContent = Math.round(lowest) + '%';
        chip.title = 'Most-restrictive headroom across recent calls. Click for details.';
    }
}

function costsToggleRateLimitPopover(ev) {
    if (ev) ev.stopPropagation();
    _rlPopoverOpen = !_rlPopoverOpen;
    const pop = document.getElementById('costs-rate-popover');
    if (!pop) return;
    pop.style.display = _rlPopoverOpen ? '' : 'none';
    if (_rlPopoverOpen) _costsRenderRatePopover();
}

function _costsRenderRatePopover() {
    const el = document.getElementById('costs-rate-popover');
    if (!el) return;
    const models = Object.keys(_rlObservations).sort();

    let html = `<div class="costs-rate-pop-header">
        <span>Anthropic rate-limit headroom</span>
        <span class="costs-rate-pop-sub">last observed values</span>
    </div>`;

    if (models.length === 0) {
        html += '<div class="empty-state" style="padding: 12px;">No observations yet — make a Claude API call through work-buddy to populate.</div>';
    } else {
        for (const model of models) {
            const obs = _rlObservations[model];
            const ageMs = _rlAge(obs);
            const stale = ageMs > _RL_STALE_MS;
            const lowest = _rlLowestPct(obs);
            let stateLabel, stateClass;
            if (stale) {
                stateLabel = '— idle'; stateClass = 'stale';
            } else if (lowest !== null && lowest < _RL_HOT_PCT) {
                stateLabel = '⚠ hot'; stateClass = 'hot';
            } else if (lowest !== null && lowest < _RL_PRESSURE_PCT) {
                stateLabel = '⚠ pressured'; stateClass = 'warn';
            } else {
                stateLabel = '✓ healthy'; stateClass = 'healthy';
            }

            html += `<div class="costs-rate-model state-${stateClass}">`;
            html += `<div class="costs-rate-model-row">
                        <code>${costsEsc(model)}</code>
                        <span class="costs-rate-state">${stateLabel}</span>
                     </div>`;
            for (const [dim, label] of [
                ['requests', 'Requests/min'],
                ['input_tokens', 'Input tokens/min'],
                ['output_tokens', 'Output tokens/min'],
            ]) {
                const d = obs[dim];
                if (!d || d.limit == null || d.remaining == null) continue;
                const pct = Math.max(0, Math.min(100, (d.remaining / d.limit) * 100));
                html += `<div class="costs-rate-bar-row">
                            <span class="costs-rate-bar-label">${label}</span>
                            <div class="costs-rate-bar"><div class="costs-rate-bar-fill" style="width: ${pct}%"></div></div>
                            <span class="costs-rate-bar-value">${costsFmtN(d.remaining)} / ${costsFmtN(d.limit)}</span>
                         </div>`;
            }
            const ageNote = stale ? ' \u2014 bucket has likely reset since' : '';
            html += `<div class="costs-rate-meta">Last observed: ${_rlAgeLabel(ageMs)}${costsEsc(ageNote)}</div>`;
            html += `</div>`;
        }
    }

    // Help section — collapsed by default. Explains everything for a
    // first-time reader: what RPM/ITPM/OTPM mean, how the bucket model
    // works, why values may be stale, and the coverage gap.
    html += `<div class="costs-rate-help">
        <button class="costs-rate-help-toggle" ${wbActAttrs('costsRateLimitToggleHelp', {})}>
            <span class="costs-rate-help-icon">\u2139</span> What is this?
        </button>
        <div id="costs-rate-help-body" class="costs-rate-help-body" style="display: none;">
            <p>Anthropic's API enforces three per-minute rate limits per model family. The strictest one bites first:</p>
            <ul>
                <li><strong>Requests/min (RPM)</strong> \u2014 how many API calls in a 60s window. Each call counts as 1 regardless of size.</li>
                <li><strong>Input tokens/min (ITPM)</strong> \u2014 total input tokens (your prompts, system messages, cache reads, cache writes) sent in a 60s window.</li>
                <li><strong>Output tokens/min (OTPM)</strong> \u2014 total output tokens Claude generates back in a 60s window.</li>
            </ul>
            <p>Each is a "token bucket" that refills continuously over the window. When a bucket hits zero, calls return HTTP 429 and you have to wait briefly.</p>
            <p><strong>Caveats about this view:</strong></p>
            <ul>
                <li>Values come from Anthropic's response headers, captured on each work-buddy API call. Calls Claude Code makes directly drain the same buckets but aren't visible here.</li>
                <li>Observations older than 5 minutes are marked <em>idle</em> \u2014 the bucket has very likely reset on Anthropic's side since.</li>
                <li>Total monthly spend, plan tier, and prepaid balance aren't in these headers \u2014 they require Anthropic's Admin API (separate key class).</li>
            </ul>
        </div>
    </div>`;

    el.innerHTML = html;
}

function costsRateLimitToggleHelp() {
    const body = document.getElementById('costs-rate-help-body');
    if (body) body.style.display = body.style.display === 'none' ? '' : 'none';
}

// Click outside the popover closes it.
document.addEventListener('click', function(ev) {
    if (!_rlPopoverOpen) return;
    const pop = document.getElementById('costs-rate-popover');
    const chip = document.getElementById('costs-rate-chip');
    if (pop && chip && !pop.contains(ev.target) && !chip.contains(ev.target)) {
        _rlPopoverOpen = false;
        pop.style.display = 'none';
    }
});

// ---- Rendering ----
function costsRenderAll(opts) {
    if (!costsState.raw) return;
    const skipModelsFilter = !!(opts && opts.skipModelsFilter);

    const { data, shape, activity, sidecar } = _costsActiveData();

    // Empty-state for the Claude Code source when nothing is cached yet.
    if (shape === 'claude_code' && data && data.available === false) {
        document.getElementById('costs-cards').innerHTML =
            `<div class="empty-state" style="padding:24px;text-align:center;">
                <div style="margin-bottom:12px;">${costsEsc(data.message || 'No Claude Code usage cached yet.')}</div>
                <button class="chats-accent-btn"
                        ${wbActAttrs('costsRefresh', {})}>Rescan Claude Code</button>
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

    if (!skipModelsFilter) costsRenderModelsFilter();
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
        // Top-tools-by-turns was a leftover from claude-usage's design;
        // not informative in a cost view. Slot is empty for claude_code.
        if (taskCard) taskCard.style.display = 'none';
        _costsDestroyChart('task');

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

    // Hide the secondary chart row entirely when both cards are hidden,
    // so we don't leave an empty row of margin behind.
    const secondRow = (taskCard || modeCard)?.parentElement;
    if (secondRow) {
        const taskShown = taskCard && taskCard.style.display !== 'none';
        const modeShown = modeCard && modeCard.style.display !== 'none';
        secondRow.style.display = (taskShown || modeShown) ? '' : 'none';
    }

    costsRenderModelTable(shape, data);
    costsRenderSessionsTable(shape, data);
}

// ---- Models filter ----
//
// Model chips are grouped by family (Anthropic Sonnet / Haiku / Opus,
// Qwen, Google, Other) so the user can bulk-toggle a whole family in
// one click. Within a family, click a chip to toggle one model.
// Alt/Shift-click on either a family pill or a chip = solo (deselect
// everything else, select only this). A small "Reset" link appears at
// the right of the row when the filter is narrowed.
//
// Family extraction:
//   * ``claude-(opus|sonnet|haiku)-...`` → "Anthropic Opus" etc.
//   * ``vendor/model``                   → vendor (capitalized)
//   * else                                → "Other"

function _costsModelFamily(model) {
    if (!model) return 'Other';
    // All Anthropic models collapse to a single "Claude" family — clicking
    // the family pill toggles every claude-* model in one go. The chip
    // labels carry the tier name so you can still pick out opus / sonnet /
    // haiku at a glance. (Earlier design grouped per-tier; reverted because
    // the per-tier bulk only saved one click anyway, and the user wanted
    // a one-click "all of Claude" toggle.)
    if (/^claude-/i.test(model)) return 'Claude';
    const vendor = model.match(/^([^/]+)\//);
    if (vendor) {
        const v = vendor[1];
        return v.charAt(0).toUpperCase() + v.slice(1);
    }
    return 'Other';
}

function _costsModelShortLabel(model, family) {
    // Strip the family-derived prefix so chips read tighter when the
    // family pill is right next to them.
    if (family === 'Claude') {
        // claude-sonnet-4-6 → "sonnet 4-6", claude-haiku-4-5-20251001 → "haiku 4-5-20251001"
        const m = model.match(/^claude-(opus|sonnet|haiku)-(.+)$/i);
        if (m) return m[1].toLowerCase() + ' ' + m[2];
        return model.replace(/^claude-/i, '');
    } else if (family !== 'Other') {
        const m = model.match(/^[^/]+\/(.+)$/);
        if (m) return m[1];
    }
    return model;
}

// Anthropic-tier sort weight for chip ordering inside the Claude family.
// Opus first (most capable), then Sonnet, then Haiku.
const _COSTS_CLAUDE_TIER_RANK = { opus: 0, sonnet: 1, haiku: 2 };

function _costsClaudeChipSortKey(model) {
    const m = model.match(/^claude-(opus|sonnet|haiku)-(.+)$/i);
    if (!m) return [99, model];
    const tier = _COSTS_CLAUDE_TIER_RANK[m[1].toLowerCase()] ?? 99;
    return [tier, m[2]];
}

function _costsGroupModelsByFamily(models) {
    const map = new Map();
    for (const m of models) {
        const fam = _costsModelFamily(m);
        if (!map.has(fam)) map.set(fam, []);
        map.get(fam).push(m);
    }
    // Claude family first (with members re-sorted by tier), then vendor
    // families A→Z; "Other" last.
    const ordered = [];
    if (map.has('Claude')) {
        const claudeModels = map.get('Claude').slice().sort((a, b) => {
            const ka = _costsClaudeChipSortKey(a);
            const kb = _costsClaudeChipSortKey(b);
            if (ka[0] !== kb[0]) return ka[0] - kb[0];
            return String(ka[1]).localeCompare(String(kb[1]));
        });
        ordered.push({ family: 'Claude', models: claudeModels });
        map.delete('Claude');
    }
    const rest = [...map.entries()]
        .map(([family, models]) => ({ family, models }))
        .sort((a, b) => {
            if (a.family === 'Other') return 1;
            if (b.family === 'Other') return -1;
            return a.family.localeCompare(b.family);
        });
    return ordered.concat(rest);
}

// Model-family rail via the shared wbRenderFilters widget (grouped tristate
// multi-select + solo + appears-when-narrowed reset). costsState.selectedModels
// (a Set; null = all selected) is the source of truth. The widget derives each
// family's all/none/indeterminate state from the leaf set and centralizes the
// modifier-solo dispatch, so the bespoke costsModel* handlers are gone.
function costsRenderModelsFilter() {
    const all = _costsCurrentModels();
    const el = document.getElementById('costs-models-filter');
    if (!el) return;
    if (all.length === 0) { el.innerHTML = ''; return; }
    const groups = _costsGroupModelsByFamily(all);
    wbRenderFilters('costs-models-filter', {
        id: 'costs-models-filter',
        mode: 'grouped',
        variant: 'grouped',
        label: 'Models',
        families: groups.map(g => ({
            family: g.family,
            members: g.models.map(m => ({ value: m, label: _costsModelShortLabel(m, g.family) })),
        })),
        getSelected: '_costsGetSelectedModels',
        onChange: '_costsOnModelChange',
        solo: true,
        reset: true,
    });
}

// null selectedModels means "all" — materialize it so the widget's tristate
// derivation and narrowed-reset check see a concrete full set.
function _costsGetSelectedModels() {
    return costsState.selectedModels || new Set(_costsCurrentModels());
}
function _costsOnModelChange(nextSet) {
    costsState.selectedModels = nextSet;
    _costsAfterModelChipChange();
}

// After mutating costsState.selectedModels, re-render the chip rail (so the
// active classes flip and the Reset appears) and refetch /api/costs so cards /
// charts / top-callers / sessions all re-aggregate against the narrowed set.
function _costsAfterModelChipChange() {
    costsRenderModelsFilter();
    refreshCostsData();
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
//
// Just the in-range counts. The "X of Y" framing was noise; if the user
// wants the unfiltered total they switch the range to "All time".
// Signature still accepts the totals (callers pass them) but they're
// ignored — keeping the param shape avoids touching every call site.
function _costsRenderMeta(filteredSessions, filteredCalls,
                          totalSessions, totalCalls) {
    const meta = document.getElementById('costs-meta');
    if (!meta) return;
    const noun = costsState.shape === 'claude_code' ? 'turn' : 'call';
    const sLabel = `${filteredSessions} session${filteredSessions === 1 ? '' : 's'}`;
    const cLabel = `${filteredCalls} ${noun}${filteredCalls === 1 ? '' : 's'}`;
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
//
// Default measure: cost. For shape='internal' we switch to call counts —
// local-LLM rows cost $0 by design, and a cost donut would invisibly
// drop them and misrepresent the work distribution. The title flips
// to match.
function costsRenderModelChart(shape, data) {
    const canvas = document.getElementById('costs-model-chart');
    const titleEl = document.getElementById('costs-model-chart-title');
    if (!canvas || typeof Chart === 'undefined') return;
    _costsDestroyChart('model');
    const rows = (data.by_model || []).filter(_costsFilterModelRow);
    const labels = rows.map(r => r.model);

    // Pick the measure for this view.
    //   shape='claude_code' (Claude Code transcripts) → cost (default)
    //   shape='internal'    (work-buddy runner activity) → calls
    //                       (local LLMs cost $0; cost would drop them)
    //   Fallback when every value is 0 → token volume, so an empty
    //   donut isn't shown for filtered windows that all happen to be
    //   $0 / 0 calls.
    const isInternal = shape === 'internal';
    const callsArr = rows.map(r => r.calls || r.turns || 0);
    const costArr = rows.map(r => r.cost_usd || 0);
    const tokensArr = rows.map(r => (r.input_tokens || 0) + (r.output_tokens || 0));

    let measure, values, fmt, title;
    if (isInternal) {
        measure = 'calls';
        values = callsArr;
        fmt = v => costsFmtN(v) + ' calls';
        title = 'Calls by model';
    } else {
        measure = 'cost';
        values = costArr;
        fmt = v => costsFmtCost(v);
        title = 'Cost by model';
    }
    if (values.every(v => v === 0)) {
        measure = 'tokens';
        values = tokensArr;
        fmt = v => costsFmtN(v) + ' tokens';
        // Don't change the title — keep the user's mental model anchored;
        // the tooltip shows tokens which makes the empty-cost case obvious.
    }
    if (titleEl) titleEl.textContent = title;

    costsState.charts.model = new Chart(canvas.getContext('2d'), {
        type: 'doughnut',
        data: { labels, datasets: [{
            data: values,
            backgroundColor: labels.map((_, i) => COSTS_MODEL_PALETTE[i % COSTS_MODEL_PALETTE.length]),
        }]},
        options: { responsive: true, maintainAspectRatio: false,
            plugins: { legend: { position: 'right',
                labels: { color: '#e6edf3', boxWidth: 12 } },
                tooltip: { callbacks: { label: ctx => ctx.label + ': ' + fmt(ctx.parsed) } } } },
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
// 60 minutes — the user's chosen threshold for "Active" sessions.
const COSTS_ACTIVE_WINDOW_MS = 60 * 60 * 1000;

function _costsActiveDot(s) {
    if (!s || !s.last) return '';
    const d = _costsParseTs(s.last);
    if (!d) return '';
    if (Date.now() - d.getTime() > COSTS_ACTIVE_WINDOW_MS) return '';
    return '<span class="wb-active-dot" title="Active in the last hour"></span>';
}

function _costsSessionColumns(shape) {
    const ccCols = [
        { key: 'short_id',  label: 'Session',  sort: 'short_id',
          render: s => `<td>${_costsActiveDot(s)}<code title="${costsEsc(s.session_id)}">${costsEsc(s.short_id)}</code></td>` },
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
          render: s => `<td>${_costsActiveDot(s)}<code title="${costsEsc(s.session_id)}">${costsEsc(s.short_id)}</code></td>` },
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
            // Per-key column class (e.g. col-branch) lets CSS pin widths
            // for columns whose contents vary widely page-to-page.
            c.key ? ('col-' + c.key.replace(/_/g, '-')) : '',
        ].filter(Boolean).join(' ');
        const attrs = sortable ? wbActAttrs('costsSortBy', {sortKey: c.sort}) : '';
        const arrowSpan = sortable ? `<span class="sort-arrow">${arrow}</span>` : '';
        html += `<th class="${cls}" ${attrs}>${costsEsc(c.label)}${arrowSpan}</th>`;
    }
    html += '</tr></thead><tbody>';

    for (const s of slice) {
        html += '<tr>';
        for (const c of cols) html += c.render(s);
        html += '</tr>';
    }

    html += '</tbody></table>';
    wrap.innerHTML = html;

    wbRenderPager('costs-sessions-pager', sorted.length, page, pageSize,
                  'costsSessionsGoToPage');

    // Reset to page 1 if the current page is now beyond the data.
    costsState.sessionPage = page;
}

// Surface handle for the Costs tab. SSE handlers in
// core/event_bus.py call refresh() on llm.call_logged. Uses the
// existing data-only ``refreshCostsData`` path which preserves chip
// rail / model filter / project selection by design — internally now
// morphdom-merges via window._wbMorphReplace where it touches user
// state. See architecture/event-bus.
window.costsSurface = {
    refresh: function() {
        if (typeof refreshCostsData === 'function') return refreshCostsData();
    },
    isMounted: function() {
        return !!document.getElementById('costs-cards');
    },
};

// ---- Event-delegation adapters ----
window.wbAction('costsRateLimitToggleHelp', function (el, e) {
    costsRateLimitToggleHelp(e);
});
window.wbAction('costsRefresh', function (el) {
    costsRefresh(el);
});
window.wbAction('costsSortBy', function (el) {
    costsSortBy(el.dataset.sortKey);
});

"""
