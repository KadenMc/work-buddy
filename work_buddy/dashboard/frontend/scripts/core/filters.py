"""Shared chip-filter renderer — one configurable widget covering every
filter behavior the dashboard tabs exhibit.

JS API::

    wbRenderFilters(containerId, config)

Mounts into ``#<containerId>`` (a ``<div class="wb-filters" id="...">`` the
caller owns in its tab HTML), in the mould of ``wbRenderPager``. Three modes:

- ``single``  — exactly one selected value per group (radiogroup; the
  ``segmented`` variant renders a capsule of mutually-exclusive pills).
- ``multi``   — a flat ``Set`` of selected values per group (toggle chips).
- ``grouped`` — family-grouped tristate multi-select: each family pill
  derives all / none / indeterminate from its members; Alt/Shift-click solos.

State is **caller-owned**. The widget keeps no selection state of its own: it
reads the current selection through ``config.getSelected`` and reports the
intended next selection through ``config.onChange``. Both are passed by
*string name* and resolved to globals at click time (the ``wbRenderPager``
``onPageFnName`` trick), so the rendered ``onclick=`` attributes stay
serializable and morphdom-stable — no closures captured on DOM nodes.

Because selection lives in caller (tab-module) state, never in the DOM, an
SSE-driven ``_wbMorphReplace`` that re-runs ``wbRenderFilters`` re-derives
identical ``is-active`` classes from ``getSelected`` — selection cannot be
lost to a DOM diff. The widget is idempotent and performs no fetches; it is a
pure view control.

Accessor contracts by mode::

    single : getSelected(groupKey) -> value          ; onChange(groupKey, value, ev)
    multi  : getSelected(groupKey) -> Set<value>     ; onChange(groupKey, nextSet, ev)
    grouped: getSelected()         -> Set<value>     ; onChange(nextSet, ev)

The config registry (``window._wbFilterConfigs``) is keyed by ``config.id``;
the inline dispatchers look the config up by id at click time, so nothing but
a stable string id and small integer indices ever live in the markup.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ===========================================================================
// Shared filter rail — wbRenderFilters
// ===========================================================================
// Configs are registered by id so the inline onclick dispatchers can resolve
// (config, selection) at click time without storing closures on DOM nodes.
window._wbFilterConfigs = window._wbFilterConfigs || {};

window.wbRenderFilters = function (containerId, config) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (!config || !config.id) return;
    window._wbFilterConfigs[config.id] = config;
    const variant = config.variant || 'chips';
    const size = config.size || 'sm';
    el.classList.add('wb-filters');
    el.classList.toggle('is-segmented', variant === 'segmented');
    el.classList.toggle('is-grouped', variant === 'grouped');
    el.classList.toggle('is-md', size === 'md');
    el.innerHTML = _wbFiltersMarkup(config);
};

// Resolve a string-named global (or tolerate a direct function reference).
function _wbFilterResolveFn(name) {
    if (typeof name === 'function') return name;
    if (typeof name === 'string' && typeof window[name] === 'function') return window[name];
    return null;
}

// Family tristate: all members selected -> 'all', none -> 'none', else 'partial'.
function _wbFilterFamilyState(selSet, memberValues) {
    let on = 0;
    for (const v of memberValues) if (selSet && selSet.has && selSet.has(v)) on++;
    if (on === 0) return 'none';
    if (on === memberValues.length) return 'all';
    return 'partial';
}

function _wbFilterChip(domId, label, active, opts) {
    opts = opts || {};
    const cls = ['wb-filter-chip'];
    if (active) cls.push('is-active');
    const role = opts.role || 'button';
    const aria = (role === 'radio')
        ? ` aria-checked="${active ? 'true' : 'false'}"`
        : ` aria-pressed="${active ? 'true' : 'false'}"`;
    const tabindex = (opts.tabindex == null) ? 0 : opts.tabindex;
    const disabled = opts.disabled ? ' disabled' : '';
    const title = opts.title ? ` title="${escapeHtml(opts.title)}"` : '';
    const click = opts.disabled ? '' : ` onclick="${opts.onclick}"`;
    const keys = (opts.disabled || !opts.onkeydown) ? '' : ` onkeydown="${opts.onkeydown}"`;
    return `<button id="${domId}" type="button" class="${cls.join(' ')}" role="${role}"`
        + `${aria} tabindex="${tabindex}"${title}${disabled}${click}${keys}>`
        + `${escapeHtml(label)}</button>`;
}

function _wbFilterFamilyPill(domId, label, active, opts) {
    opts = opts || {};
    const cls = ['wb-filter-family-pill'];
    if (active) cls.push('is-active');
    if (opts.indeterminate) cls.push('is-indeterminate');
    // Tristate maps to aria-pressed mixed/true/false.
    const aria = ` aria-pressed="${opts.mixed ? 'mixed' : (active ? 'true' : 'false')}"`;
    const title = opts.title ? ` title="${escapeHtml(opts.title)}"` : '';
    const keys = opts.onkeydown ? ` onkeydown="${opts.onkeydown}"` : '';
    return `<button id="${domId}" type="button" class="${cls.join(' ')}" role="button"`
        + `${aria} tabindex="0"${title} onclick="${opts.onclick}"${keys}>`
        + `${escapeHtml(label)}</button>`;
}

function _wbFilterChipTitle(config, opt) {
    if (opt.title) return opt.title;
    if (config.solo && config.mode !== 'single') return 'Click to toggle · Alt-click to solo';
    return '';
}

function _wbFiltersMarkup(config) {
    if ((config.mode || 'multi') === 'grouped') return _wbFiltersGroupedMarkup(config);
    return _wbFiltersFlatMarkup(config);
}

// single / multi: one or more named groups of chips.
function _wbFiltersFlatMarkup(config) {
    const id = config.id;
    const mode = config.mode || 'multi';
    const variant = config.variant || 'chips';
    const getSel = _wbFilterResolveFn(config.getSelected);
    const groups = config.groups || [];
    let html = '';
    groups.forEach(function (group, gi) {
        const label = group.label
            ? `<span class="wb-filter-label">${escapeHtml(group.label)}</span>` : '';
        const sel = getSel ? getSel(group.key) : null;
        const chips = (group.options || []).map(function (opt, oi) {
            const active = (mode === 'single')
                ? (sel === opt.value)
                : !!(sel && sel.has && sel.has(opt.value));
            const domId = `wbf-${id}-${gi}-${oi}`;
            const role = (mode === 'single') ? 'radio' : 'button';
            // Roving tabindex for single (radiogroup); every toggle reachable for multi.
            const tabindex = (mode === 'single') ? (active ? 0 : -1) : 0;
            return _wbFilterChip(domId, (opt.label != null ? opt.label : opt.value), active, {
                role: role,
                tabindex: tabindex,
                disabled: !!opt.disabled,
                title: _wbFilterChipTitle(config, opt),
                onclick: `_wbFilterClick('${id}',${gi},${oi},event)`,
                onkeydown: `_wbFilterKey('${id}',${gi},${oi},event)`,
            });
        }).join('');
        const groupRole = (mode === 'single') ? 'radiogroup' : 'group';
        const groupAria = group.label ? ` aria-label="${escapeHtml(group.label)}"` : '';
        const wrapCls = (variant === 'segmented') ? 'wb-filter-capsule' : 'wb-filter-group';
        html += label + `<span class="${wrapCls}" role="${groupRole}"${groupAria}>${chips}</span>`;
    });
    html += _wbFilterResetHtml(config, mode);
    return html;
}

// grouped: family containers with a derived-tristate family pill + member chips.
function _wbFiltersGroupedMarkup(config) {
    const id = config.id;
    const getSel = _wbFilterResolveFn(config.getSelected);
    const sel = getSel ? getSel() : null;
    const families = config.families || [];
    let html = config.label
        ? `<span class="wb-filter-label">${escapeHtml(config.label)}</span>` : '';
    families.forEach(function (fam, fi) {
        const memberValues = (fam.members || []).map(function (m) { return m.value; });
        const state = _wbFilterFamilyState(sel, memberValues);
        const famActive = state === 'all';
        const famIndet = state === 'partial';
        const famTitle = config.solo
            ? 'Click to toggle family · Alt-click to solo' : 'Click to toggle family';
        let inner = _wbFilterFamilyPill(`wbf-${id}-fp${fi}`, fam.family, famActive, {
            indeterminate: famIndet,
            mixed: famIndet,
            title: famTitle,
            onclick: `_wbFilterFamilyClick('${id}',${fi},event)`,
            onkeydown: `_wbFilterFamilyKey('${id}',${fi},event)`,
        });
        inner += (fam.members || []).map(function (m, mi) {
            const active = !!(sel && sel.has && sel.has(m.value));
            return _wbFilterChip(`wbf-${id}-f${fi}-${mi}`, (m.label != null ? m.label : m.value), active, {
                role: 'button',
                tabindex: 0,
                title: config.solo ? 'Click to toggle · Alt-click to solo' : 'Click to toggle',
                onclick: `_wbFilterMemberClick('${id}',${fi},${mi},event)`,
                onkeydown: `_wbFilterMemberKey('${id}',${fi},${mi},event)`,
            });
        }).join('');
        html += `<span class="wb-filter-family" role="group" aria-label="${escapeHtml(fam.family)}">${inner}</span>`;
    });
    html += _wbFilterResetHtml(config, 'grouped');
    return html;
}

// Reset appears only when the selection is narrowed. single is never narrowed;
// grouped auto-derives narrowed (selected != all leaves) unless the caller
// supplies isNarrowed; multi requires an explicit isNarrowed accessor.
function _wbFilterResetHtml(config, mode) {
    if (!config.reset || mode === 'single') return '';
    let narrowed = false;
    const isNarrowed = _wbFilterResolveFn(config.isNarrowed);
    if (isNarrowed) {
        narrowed = !!isNarrowed();
    } else if (mode === 'grouped') {
        const getSel = _wbFilterResolveFn(config.getSelected);
        const sel = getSel ? getSel() : null;
        let total = 0;
        (config.families || []).forEach(function (f) { total += (f.members || []).length; });
        narrowed = !!(sel && typeof sel.size === 'number' && sel.size !== total);
    }
    if (!narrowed) return '';
    const label = config.resetLabel || 'Reset';
    return `<button class="wb-filter-reset" type="button" title="Clear filter"`
        + ` onclick="_wbFilterReset('${config.id}',event)"`
        + ` onkeydown="_wbFilterResetKey('${config.id}',event)">${escapeHtml(label)}</button>`;
}

// ---- Click dispatchers (resolve config + selection by id at click time) ----

function _wbFilterClick(widgetId, gi, oi, ev) {
    const cfg = window._wbFilterConfigs[widgetId];
    if (!cfg) return;
    const group = (cfg.groups || [])[gi];
    if (!group) return;
    const opt = (group.options || [])[oi];
    if (!opt || opt.disabled) return;
    const onChange = _wbFilterResolveFn(cfg.onChange);
    if (!onChange) return;
    if (cfg.mode === 'single') { onChange(group.key, opt.value, ev); return; }
    const getSel = _wbFilterResolveFn(cfg.getSelected);
    const cur = (getSel && getSel(group.key)) || new Set();
    let next;
    if (cfg.solo && ev && (ev.altKey || ev.shiftKey)) {
        next = new Set([opt.value]);
    } else {
        next = new Set(cur);
        if (next.has(opt.value)) next.delete(opt.value); else next.add(opt.value);
    }
    onChange(group.key, next, ev);
}

function _wbFilterMemberClick(widgetId, fi, mi, ev) {
    const cfg = window._wbFilterConfigs[widgetId];
    if (!cfg) return;
    const fam = (cfg.families || [])[fi];
    if (!fam) return;
    const m = (fam.members || [])[mi];
    if (!m) return;
    const onChange = _wbFilterResolveFn(cfg.onChange);
    if (!onChange) return;
    const getSel = _wbFilterResolveFn(cfg.getSelected);
    const cur = (getSel && getSel()) || new Set();
    let next;
    if (cfg.solo && ev && (ev.altKey || ev.shiftKey)) {
        next = new Set([m.value]);
    } else {
        next = new Set(cur);
        if (next.has(m.value)) next.delete(m.value); else next.add(m.value);
    }
    onChange(next, ev);
}

function _wbFilterFamilyClick(widgetId, fi, ev) {
    const cfg = window._wbFilterConfigs[widgetId];
    if (!cfg) return;
    const fam = (cfg.families || [])[fi];
    if (!fam) return;
    const memberValues = (fam.members || []).map(function (m) { return m.value; });
    const onChange = _wbFilterResolveFn(cfg.onChange);
    if (!onChange) return;
    const getSel = _wbFilterResolveFn(cfg.getSelected);
    const cur = (getSel && getSel()) || new Set();
    let next;
    if (cfg.solo && ev && (ev.altKey || ev.shiftKey)) {
        next = new Set(memberValues);
    } else {
        next = new Set(cur);
        // 'all' on -> clear the family; partial/none -> fill it.
        if (_wbFilterFamilyState(cur, memberValues) === 'all') {
            memberValues.forEach(function (v) { next.delete(v); });
        } else {
            memberValues.forEach(function (v) { next.add(v); });
        }
    }
    onChange(next, ev);
}

function _wbFilterReset(widgetId, ev) {
    const cfg = window._wbFilterConfigs[widgetId];
    if (!cfg) return;
    const onReset = _wbFilterResolveFn(cfg.onReset);
    if (onReset) { onReset(ev); return; }
    // Default reset for grouped: re-select every leaf.
    if (cfg.mode === 'grouped') {
        const all = [];
        (cfg.families || []).forEach(function (f) {
            (f.members || []).forEach(function (m) { all.push(m.value); });
        });
        const onChange = _wbFilterResolveFn(cfg.onChange);
        if (onChange) onChange(new Set(all), ev);
    }
}

// ---- Keyboard support (additive — the tabs were click-only) ----

function _wbFilterIsActivate(ev) {
    return !!ev && (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar');
}

function _wbFilterKey(widgetId, gi, oi, ev) {
    if (_wbFilterIsActivate(ev)) { ev.preventDefault(); _wbFilterClick(widgetId, gi, oi, ev); return; }
    const cfg = window._wbFilterConfigs[widgetId];
    if (!cfg || cfg.mode !== 'single') return;
    const group = (cfg.groups || [])[gi];
    if (!group) return;
    const opts = group.options || [];
    const n = opts.length;
    let delta = 0;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') delta = 1;
    else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') delta = -1;
    else return;
    ev.preventDefault();
    let idx = oi;
    for (let s = 0; s < n; s++) { idx = (idx + delta + n) % n; if (!opts[idx].disabled) break; }
    const onChange = _wbFilterResolveFn(cfg.onChange);
    if (onChange) onChange(group.key, opts[idx].value, ev);
    // Caller's onChange repaints the rail; focus the now-active chip by stable id.
    const focusEl = document.getElementById(`wbf-${widgetId}-${gi}-${idx}`);
    if (focusEl) focusEl.focus();
}

function _wbFilterMemberKey(widgetId, fi, mi, ev) {
    if (_wbFilterIsActivate(ev)) { ev.preventDefault(); _wbFilterMemberClick(widgetId, fi, mi, ev); }
}

function _wbFilterFamilyKey(widgetId, fi, ev) {
    if (_wbFilterIsActivate(ev)) { ev.preventDefault(); _wbFilterFamilyClick(widgetId, fi, ev); }
}

function _wbFilterResetKey(widgetId, ev) {
    if (_wbFilterIsActivate(ev)) { ev.preventDefault(); _wbFilterReset(widgetId, ev); }
}
"""


def styles() -> str:
    return """
/* Shared filter rail — rendered by ``wbRenderFilters`` in
   ``core/filters.py``. One canonical chip style (the ``wb-filter-*`` family)
   covering single-select (segmented), flat multi-select, and family-grouped
   tristate multi-select. Class names are tab-agnostic: a tab mounts a
   ``<div class="wb-filters" id="..."></div>`` and calls the renderer. Themes
   via the existing design tokens, so no new variables are introduced. */
.wb-filters {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
}
.wb-filter-label {
    font-size: 12px; color: var(--text-muted); margin-right: 2px;
}
.wb-filter-group, .wb-filter-capsule {
    display: inline-flex; align-items: center; gap: 4px; flex-wrap: wrap;
}

/* Canonical chip — multi-select / chips variant. */
.wb-filter-chip {
    background: var(--bg-tertiary); border: 1px solid var(--border);
    color: var(--text-secondary); border-radius: 12px; padding: 3px 10px;
    font-size: 11px; cursor: pointer; font-family: inherit;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.wb-filter-chip:hover {
    background: var(--bg-secondary); color: var(--text-primary);
    border-color: var(--accent);
}
.wb-filter-chip.is-active {
    background: var(--accent-subtle); border-color: var(--accent); color: var(--accent);
}
.wb-filter-chip:disabled { opacity: 0.45; cursor: not-allowed; }
.wb-filter-chip:disabled:hover {
    background: var(--bg-tertiary); color: var(--text-secondary); border-color: var(--border);
}

/* Segmented variant — capsule of mutually-exclusive pills (single-select). */
.wb-filters.is-segmented .wb-filter-capsule {
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: 16px; padding: 3px; gap: 4px; flex-wrap: nowrap;
}
.wb-filters.is-segmented .wb-filter-chip {
    background: transparent; border: 1px solid transparent; border-radius: 12px;
    color: var(--text-secondary); padding: 4px 14px; font-size: 12px;
}
.wb-filters.is-segmented .wb-filter-chip:hover {
    background: transparent; color: var(--text-primary); border-color: transparent;
}
.wb-filters.is-segmented .wb-filter-chip.is-active {
    background: var(--accent-subtle); color: var(--accent);
    border-color: transparent; font-weight: 500;
}
/* size:md — solid-fill active for capsule single-select (reads heavier). */
.wb-filters.is-segmented.is-md .wb-filter-chip.is-active {
    background: var(--accent); color: #fff;
}

/* Family-grouped (tristate) — bordered family container + heavier family pill. */
.wb-filter-family {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 3px 6px; margin-right: 4px;
    background: rgba(255, 255, 255, 0.015);
    border: 1px solid var(--border); border-radius: 14px;
}
.wb-filter-family-pill {
    background: var(--bg-tertiary); border: 1px solid var(--border);
    color: var(--text-primary); border-radius: 12px; padding: 3px 10px;
    font-size: 11px; font-weight: 600; cursor: pointer; font-family: inherit;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.wb-filter-family-pill:hover {
    background: var(--accent-subtle); color: var(--accent); border-color: var(--accent);
}
.wb-filter-family-pill.is-active {
    background: var(--accent); color: #fff; border-color: var(--accent);
}
/* indeterminate = some members on. Striped fill reads as "in between". */
.wb-filter-family-pill.is-indeterminate {
    background: var(--accent-subtle); color: var(--accent); border-color: var(--accent);
    background-image: repeating-linear-gradient(
        45deg, transparent 0, transparent 3px,
        rgba(216, 120, 87, 0.15) 3px, rgba(216, 120, 87, 0.15) 6px);
}

/* Reset — dashed action, appears only when the selection is narrowed. */
.wb-filter-reset {
    background: transparent; border: 1px dashed var(--border);
    color: var(--text-muted); border-radius: 12px; padding: 3px 10px;
    font-size: 11px; cursor: pointer; font-family: inherit; margin-left: 4px;
}
.wb-filter-reset:hover { color: var(--accent); border-color: var(--accent); }

/* Keyboard focus ring — keyboard nav is part of the canonical behavior. */
.wb-filter-chip:focus-visible,
.wb-filter-family-pill:focus-visible,
.wb-filter-reset:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 1px;
}
"""
