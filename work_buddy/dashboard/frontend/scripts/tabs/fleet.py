"""Dashboard Settings › Inference — Local model fleet section.

A per-machine view of the local-inference fleet: one card per machine showing
reachability, the model(s) currently loaded (with live context utilization), and
hardware (GPU / VRAM / RAM). It answers "what's running on which box" — distinct
from the per-call provenance feed it sits above in the same Inference sub-view.

Sourced from ``GET /api/fleet`` (background-cached). The machine roster +
reachability + loaded models are discovered live from the local-inference
provider; the local machine reports its own hardware, while remote-peer hardware
comes from the ``inference.fleet`` config roster (a "config" provenance hint
distinguishes the two).

There is no internal event for external model loads/unloads, so this section is
load-on-open plus a manual refresh — it deliberately does NOT subscribe to the
provenance feed's SSE event (the one that keeps the per-call feed below it live).
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Local model fleet (Settings › Inference section) ----

function _fleetK(n) {
    if (n == null) return '—';
    if (n >= 1024) return Math.round(n / 1024) + 'k';
    return String(n);
}

function _fleetGb(n) {
    if (n == null) return null;
    // Roster values are already GB; live values arrive pre-converted server-side.
    return (Math.round(n * 10) / 10);
}

function _fleetReadOnly() { return (typeof WB_READ_ONLY_MODE !== 'undefined' && WB_READ_ONLY_MODE); }

async function loadFleet() {
    const container = document.getElementById('fleet-content');
    if (!container) return;
    window._fleetData = await fetchJSON('/api/fleet');
    _fleetRender();
}

function _fleetRender() {
    const container = document.getElementById('fleet-content');
    if (!container) return;
    window._wbMorphReplace(container, _fleetRenderInner(window._fleetData));
}

function _fleetReachBadge(m) {
    if (m.reachable) return `<span class="idx-badge idx-ok">online</span>`;
    return `<span class="idx-badge idx-muted-badge">offline</span>`;
}

function _fleetHardware(hw) {
    const gpus = (hw && hw.gpus) || [];
    const hasRam = hw && hw.ram_gb != null;
    if (!hw || (gpus.length === 0 && !hasRam)) {
        return `<div class="fleet-hw idx-muted">hardware unknown</div>`;
    }
    // Provenance hint: did the machine report this live, or is it from config?
    let src = '';
    if (hw.source === 'live') src = `<span class="fleet-hw-src" title="Read live from this machine">live</span>`;
    else if (hw.source === 'roster') src = `<span class="fleet-hw-src fleet-hw-src-cfg" title="From the inference.fleet config roster (peer hardware isn't readable remotely)">config</span>`;
    // One line per GPU; a total only when there's more than one to sum.
    const gpuLines = gpus.map(g => {
        const nm = g.name ? escapeHtml(g.name) : 'GPU';
        const vr = (g.vram_gb != null) ? ` · ${_fleetGb(g.vram_gb)} GB` : '';
        return `<div class="fleet-gpu">${nm}${vr}</div>`;
    }).join('');
    const total = (gpus.length > 1 && hw.total_vram_gb != null)
        ? ` · ${_fleetGb(hw.total_vram_gb)} GB total` : '';
    const head = gpus.length
        ? `${gpus.length} GPU${gpus.length === 1 ? '' : 's'}${total}`
        : 'hardware';
    const ram = hasRam ? `<div class="fleet-gpu idx-muted">${_fleetGb(hw.ram_gb)} GB RAM</div>` : '';
    return `<div class="fleet-hw">
        <div class="fleet-hw-head">${head} ${src}</div>
        ${gpuLines}${ram}
    </div>`;
}

function _fleetModel(lm) {
    const name = escapeHtml(lm.display_name || lm.model || '—');
    const kind = lm.kind ? `<span class="idx-badge fleet-kind">${escapeHtml(lm.kind)}</span>` : '';
    const quant = lm.quant ? `<span class="fleet-meta">${escapeHtml(lm.quant)}</span>` : '';
    const statusCls = lm.status === 'idle' ? 'idx-muted-badge' : 'inf-st-run';
    const status = lm.status ? `<span class="idx-badge ${statusCls}">${escapeHtml(lm.status)}</span>` : '';
    const queued = (lm.queued != null && lm.queued > 0)
        ? `<span class="fleet-meta">${lm.queued} queued</span>` : '';
    // Context: how much of the model's trained max window the loaded instance
    // uses. The numeric "loaded / max ctx" is always shown; the bar is only drawn
    // where it's informative — i.e. LLMs with real headroom (a 4k-of-262k sliver
    // signals lots of unused window / KV-cache room). For embedding models the
    // window is fixed and equal to max, so a permanently-full bar is just noise.
    let ctx = '';
    if (lm.context_length != null && lm.max_context_length) {
        const showBar = lm.kind !== 'embedding' && lm.context_length < lm.max_context_length;
        const pct = Math.max(2, Math.min(100,
            Math.round(lm.context_length / lm.max_context_length * 100)));
        const bar = showBar
            ? `<div class="fleet-ctx-bar"><div class="fleet-ctx-fill" style="width:${pct}%"></div></div>`
            : '';
        ctx = `
            <div class="fleet-ctx" title="loaded context ${lm.context_length} of the model's ${lm.max_context_length} max">
                ${bar}
                <span class="fleet-meta">${_fleetK(lm.context_length)} / ${_fleetK(lm.max_context_length)} ctx</span>
            </div>`;
    }
    return `
        <div class="fleet-model">
            <div class="fleet-model-row">
                <span class="fleet-model-name" title="${escapeHtml(lm.model || '')}">${name}</span>
                ${kind}${status}
            </div>
            <div class="fleet-model-row">${quant}${queued}${ctx}</div>
        </div>`;
}

function _fleetCard(m) {
    const local = m.is_local ? `<span class="idx-badge fleet-local">this machine</span>` : '';
    const ro = _fleetReadOnly();
    const editBtn = ro ? '' :
        `<button class="fleet-edit" title="Edit role / hardware" ` + wbActAttrs('showFleetForm', {deviceId: m.device_id}) + `>&#9881;</button>`;
    const role = m.role
        ? `<div class="fleet-role idx-muted">${escapeHtml(m.role)}</div>`
        : (ro ? '' : `<div class="fleet-role-empty" ` + wbActAttrs('showFleetForm', {deviceId: m.device_id}) + `>+ add a role label</div>`);
    const models = (m.loaded_models && m.loaded_models.length)
        ? m.loaded_models.map(_fleetModel).join('')
        : `<div class="fleet-empty idx-muted">no models loaded</div>`;
    const offlineCls = m.reachable ? '' : ' fleet-card-offline';
    return `
        <div class="fleet-card${offlineCls}">
            <div class="fleet-card-head">
                <span class="fleet-name">${escapeHtml(m.name || '—')}</span>
                ${local}${_fleetReachBadge(m)}${editBtn}
            </div>
            ${role}
            ${_fleetHardware(m.hardware)}
            <div class="fleet-models">${models}</div>
        </div>`;
}

// Inline roster editor — reuses the Jobs add-form styling (.jobs-add-form /
// .job-form-*) so inputs/buttons match the rest of Settings. Role is primary;
// hardware specs are optional and hidden for the local machine (detected live).
function _fleetFormHtml() {
    if (_fleetReadOnly()) return '';
    return `
    <div id="fleet-form" class="jobs-add-form" hidden>
        <div class="fleet-form-title" id="fleet-form-title">Edit machine</div>
        <div class="job-form-row">
            <label>Role label
                <input id="fleet-form-role" type="text" autocomplete="off" placeholder="e.g. Remote compute node" />
                <small>A human label for this machine. The only field you usually need.</small>
            </label>
        </div>
        <div id="fleet-form-specs">
            <div class="fleet-form-note idx-muted">Remote-peer hardware can't be auto-detected — set it here and it shows on the card. No config file to edit.</div>
            <div class="job-form-row">
                <label>GPUs (one per line: name | VRAM in GB)
                    <textarea id="fleet-form-gpus" rows="2" placeholder="NVIDIA RTX 4090 | 24"></textarea>
                    <small>One line per GPU — a dual-GPU box gets two lines. VRAM is optional.</small>
                </label>
            </div>
            <div class="job-form-row">
                <label>RAM (GB)
                    <input id="fleet-form-ram" type="number" step="any" min="0" autocomplete="off" />
                </label>
            </div>
        </div>
        <div id="fleet-form-error" class="job-form-error" hidden></div>
        <div class="job-form-actions">
            <button type="button" id="fleet-form-clear" class="fleet-clear-btn" ` + wbActAttrs('clearFleetFromForm', {}) + ` hidden>Clear roster entry</button>
            <button type="button" class="jobs-form-cancel" ` + wbActAttrs('hideFleetForm', {}) + `>Cancel</button>
            <button type="button" class="jobs-form-submit" ` + wbActAttrs('submitFleetForm', {}) + `>Save</button>
        </div>
    </div>`;
}

let _fleetEditingId = null;

function showFleetForm(deviceId) {
    if (_fleetReadOnly()) return;
    const form = document.getElementById('fleet-form');
    if (!form) return;
    const m = ((window._fleetData && window._fleetData.machines) || []).find(x => x.device_id === deviceId);
    if (!m) return;
    _fleetEditingId = deviceId;
    document.getElementById('fleet-form-title').textContent = `Edit ${m.name || deviceId}`;
    document.getElementById('fleet-form-role').value = m.role || '';
    // The local machine's hardware is detected live — hide the spec fields for it.
    const specs = document.getElementById('fleet-form-specs');
    specs.hidden = !!m.is_local;
    const hw = m.hardware || {};
    const rostered = hw.source === 'roster';
    const gpus = (rostered && Array.isArray(hw.gpus)) ? hw.gpus : [];
    document.getElementById('fleet-form-gpus').value = gpus.map(g =>
        (g.vram_gb != null) ? `${g.name || ''} | ${g.vram_gb}` : (g.name || '')).join('\n');
    document.getElementById('fleet-form-ram').value = (rostered && hw.ram_gb != null) ? hw.ram_gb : '';
    document.getElementById('fleet-form-clear').hidden = !m.in_roster;
    document.getElementById('fleet-form-error').hidden = true;
    form.hidden = false;
    form.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function hideFleetForm() { const f = document.getElementById('fleet-form'); if (f) f.hidden = true; _fleetEditingId = null; }

// Parse the "name | VRAM" textarea (one GPU per line) into [{name, vram_gb}].
// VRAM stays a string here; the server validates/coerces it.
function _fleetParseGpus(text) {
    return (text || '').split('\n').map(s => s.trim()).filter(Boolean).map(line => {
        const i = line.indexOf('|');
        const name = (i >= 0 ? line.slice(0, i) : line).trim();
        const vram = (i >= 0 ? line.slice(i + 1) : '').trim();
        const g = {};
        if (name) g.name = name;
        if (vram) g.vram_gb = vram;
        return g;
    }).filter(g => g.name || g.vram_gb);
}

async function _fleetPost(payload) {
    try {
        const resp = await fetch('/api/fleet/roster', {method: 'POST',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
        return await resp.json();
    } catch (e) { settingsToast(`Network error: ${e.message}`, 'error'); return null; }
}

async function submitFleetForm() {
    if (_fleetReadOnly() || !_fleetEditingId) return;
    const errEl = document.getElementById('fleet-form-error');
    errEl.hidden = true;
    const payload = {action: 'set', device_id: _fleetEditingId,
                     role: document.getElementById('fleet-form-role').value.trim()};
    if (!document.getElementById('fleet-form-specs').hidden) {
        payload.gpus = _fleetParseGpus(document.getElementById('fleet-form-gpus').value);
        payload.ram_gb = document.getElementById('fleet-form-ram').value.trim();
    }
    const result = await _fleetPost(payload);
    if (!result || !result.success) {
        errEl.textContent = (result && (result.error
            || (result.errors_by_field && Object.values(result.errors_by_field).join(' ')))) || 'Save failed.';
        errEl.hidden = false;
        return;
    }
    settingsToast(result.note || 'Saved.', 'success');
    hideFleetForm();
    await loadFleet();
}

async function clearFleetFromForm() {
    if (_fleetReadOnly() || !_fleetEditingId) return;
    if (!confirm("Clear this machine's roster entry (role + hardware)? Discovered info stays.")) return;
    const result = await _fleetPost({action: 'remove', device_id: _fleetEditingId});
    if (!result || !result.success) { settingsToast((result && result.error) || 'Clear failed', 'error'); return; }
    settingsToast(result.note || 'Cleared.', 'success');
    hideFleetForm();
    await loadFleet();
}

function _fleetRenderInner(data) {
    if (!data) return `<div class="loading">Loading fleet...</div>`;
    const header = `
        <div class="fleet-header">
            <div class="fleet-title-row">
                <span class="emb-section-title">Local model fleet</span>
                <button class="inf-help-btn" title="Refresh fleet" ` + wbActAttrs('loadFleet', {}) + `>↻</button>
            </div>
            <div class="idx-muted inf-intro">What's loaded on which machine right now — reachability, models, and hardware.</div>
        </div>`;
    let banner = '';
    if (data.lms_available === false) {
        banner = `<div class="fleet-banner">${escapeHtml(data.error || 'Local-inference provider not reachable.')} Showing configured machines as offline.</div>`;
    }
    const machines = data.machines || [];
    const body = machines.length
        ? `<div class="fleet-grid">${machines.map(_fleetCard).join('')}</div>`
        : `<div class="empty-state">No machines found — configure a local-inference provider (e.g. LM Studio) and an inference.fleet roster.</div>`;
    return `<div class="fleet-section">${header}${banner}${body}${_fleetFormHtml()}</div>`;
}

// Surface handle (manual refresh only — NOT wired to the provenance feed's SSE
// event, since external model loads/unloads have no internal event to listen to).
window.fleetSurface = {
    refresh: function() { return loadFleet(); },
    isMounted: function() {
        return !!document.getElementById('fleet-content')
            && (typeof WB_SETTINGS_SUBTAB === 'undefined' || WB_SETTINGS_SUBTAB === 'inference');
    },
};

// Event delegation adapters
window.wbAction('showFleetForm', function (el) { showFleetForm(el.dataset.deviceId); });
window.wbAction('hideFleetForm', function (el) { hideFleetForm(); });
window.wbAction('clearFleetFromForm', function (el) { clearFleetFromForm(); });
window.wbAction('submitFleetForm', function (el) { submitFleetForm(); });
window.wbAction('loadFleet', function (el) { loadFleet(); });
"""


def styles() -> str:
    return """
/* Local model fleet section — reuses .idx-badge/.idx-ok/.idx-muted-badge/.inf-st-run
   and .emb-section-title/.inf-intro/.inf-help-btn from the Embeddings/Inference views. */
.fleet-section { margin-bottom: 26px; }
.fleet-header { margin-bottom: 14px; }
.fleet-title-row { display:flex; align-items:center; gap:8px; }

.fleet-banner { margin: 8px 0 14px; padding: 8px 12px; border-radius: 8px;
    background: color-mix(in srgb, var(--yellow, #d8a657) 16%, transparent);
    color: var(--text-secondary); font-size: 0.85em; }

.fleet-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }

.fleet-card { border: 1px solid var(--border, var(--bg-tertiary)); border-radius: 10px;
    background: var(--bg-secondary); padding: 14px 16px; display:flex; flex-direction:column; gap:8px; }
.fleet-card-offline { opacity: 0.62; }

.fleet-card-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.fleet-name { font-weight: 700; color: var(--text-primary); }
.fleet-local { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }
.fleet-role { font-size: 0.78em; }

.fleet-hw { font-size: 0.82em; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.fleet-hw-head { font-size: 0.78em; color: var(--text-muted); margin-bottom: 2px; }
.fleet-gpu { color: var(--text-secondary); }
.fleet-hw-src { margin-left: 6px; font-size: 0.82em; padding: 1px 6px; border-radius: 6px;
    background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
.fleet-hw-src-cfg { background: color-mix(in srgb, var(--text-muted) 22%, transparent); color: var(--text-muted); }

.fleet-models { display:flex; flex-direction:column; gap:8px; margin-top:4px; }
.fleet-empty { font-size: 0.82em; font-style: italic; }
.fleet-model { border-top: 1px solid var(--border, var(--bg-tertiary)); padding-top: 6px; }
.fleet-model:first-child { border-top: none; padding-top: 0; }
.fleet-model-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.fleet-model-name { font-weight: 600; color: var(--text-primary); font-size: 0.88em;
    max-width: 100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fleet-kind { background: color-mix(in srgb, var(--text-muted) 20%, transparent); color: var(--text-muted); }
.fleet-meta { font-size: 0.78em; color: var(--text-muted); font-variant-numeric: tabular-nums; }

.fleet-ctx { display:flex; align-items:center; gap:6px; flex: 1 1 auto; min-width: 120px; }
.fleet-ctx-bar { flex: 1 1 auto; height: 5px; border-radius: 3px; background: var(--bg-tertiary); overflow: hidden; }
.fleet-ctx-fill { height: 100%; background: var(--accent); }

/* Inline editor affordances (form itself reuses .jobs-add-form / .job-form-*) */
.fleet-edit { margin-left: auto; background: none; border: none; cursor: pointer;
    font-size: 1em; opacity: 0.6; padding: 2px 4px; color: var(--text-secondary); }
.fleet-edit:hover { opacity: 1; color: var(--text-primary); }
.fleet-role-empty { font-size: 0.78em; color: var(--text-muted); cursor: pointer;
    border: 1px dashed var(--border, var(--bg-tertiary)); border-radius: 6px;
    padding: 2px 8px; display: inline-block; }
.fleet-role-empty:hover { color: var(--text-secondary); border-color: var(--text-muted); }
.fleet-form-title { font-weight: 600; font-size: 13px; margin-bottom: 10px; color: var(--text-primary); }
.fleet-form-note { font-size: 0.8em; margin: 2px 0 8px; }
.fleet-clear-btn { margin-right: auto; border: 1px solid var(--red); border-radius: 4px;
    padding: 6px 14px; font-size: 12px; font-weight: 600; cursor: pointer;
    background: var(--bg-tertiary); color: var(--red); }
.fleet-clear-btn:hover { background: color-mix(in srgb, var(--red) 14%, var(--bg-tertiary)); }
"""
