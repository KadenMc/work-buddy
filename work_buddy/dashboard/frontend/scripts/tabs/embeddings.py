"""Dashboard Settings › Embeddings sub-view JS.

Two tables: **System** (work-buddy's own indexes — IR + knowledge — read-only) and
**Your vaults** (the user's roots — editable). Data via ``/api/embeddings``. The ⚙
opens an inline form editing a vault's id/path/include/exclude, POSTing to
``/api/embeddings/vault`` (the ``vault_config`` capability); plus add/remove. The form
**reuses the Jobs add-form styling** (``.jobs-add-form`` + ``.jobs-form-*``) so inputs,
buttons, and validation match the rest of the dashboard. All mutating controls are
hidden in read-only mode. Per-vault on-disk size is not separable (one shared DB) → the
total goes in the table footer.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Embeddings (Settings sub-view) ----
const _EMB_LABELS = { ir: 'IR', knowledge: 'Knowledge' };
let _embVaults = [];

function _embNum(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }
function _embSize(mb) { return (mb == null) ? '—' : mb + ' MB'; }
function _embDate(s) {
    if (!s) return '—';
    try { return new Date(s).toLocaleString([],
        {year:'numeric', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}); }
    catch (e) { return String(s).slice(0, 19); }
}
function _embHealth(h) {
    const cls = h === 'ok' ? 'idx-ok' : (h === 'unreachable' ? 'idx-warn' : 'idx-err');
    return `<span class="idx-badge ${cls}">${h || 'ok'}</span>`;
}
function _embBuilding(b) { return b ? ' <span class="idx-badge idx-building">building…</span>' : ''; }
function _embReadOnly() { return (typeof WB_READ_ONLY_MODE !== 'undefined' && WB_READ_ONLY_MODE); }

async function loadEmbeddings() {
    const container = document.getElementById('embeddings-content');
    if (!container) return;
    const data = await fetchJSON('/api/embeddings');
    if (!data) { container.innerHTML = '<div class="empty-state">Embeddings status unavailable.</div>'; return; }
    _embVaults = data.vaults || [];
    container.innerHTML = _embRenderSystem(data) + _embRenderUser(data) + _embFormHtml();
}

function _embRenderSystem(data) {
    if (data.system_error) return `<div class="empty-state">System index status failed: ${escapeHtml(data.system_error)}</div>`;
    const rows = data.system || [];
    const body = rows.length ? rows.map(r => `
        <tr>
            <td>${_EMB_LABELS[r.index] || escapeHtml(r.index)}</td>
            <td><strong${r.detail ? ` title="${escapeHtml(r.detail)}"` : ''}>${escapeHtml(r.source)}</strong></td>
            <td class="idx-r">${_embNum(r.items)}</td>
            <td class="idx-r">${_embNum(r.vectors)}</td>
            <td class="idx-r">${(r.pending > 0) ? `<span class="idx-pending">${_embNum(r.pending)}</span>` : '0'}</td>
            <td class="idx-r">${_embSize(r.size_mb)}</td>
            <td>${_embHealth(r.health)}</td>
            <td class="idx-muted">${_embDate(r.last_build)}</td>
        </tr>`).join('') : '<tr><td colspan="8" class="idx-muted">no system indexes</td></tr>';
    return `
        <div class="emb-section">
            <div class="emb-section-head">
                <span class="emb-section-title">System</span>
                <span class="idx-muted">work-buddy's own indexes — observe-only</span>
            </div>
            <table class="data-table">
                <thead><tr>
                    <th>Index</th><th>Source</th><th class="idx-r">Items</th><th class="idx-r">Vectors</th>
                    <th class="idx-r">Pending</th><th class="idx-r">Size</th><th>Health</th><th>Last build</th>
                </tr></thead>
                <tbody>${body}</tbody>
            </table>
        </div>`;
}

function _embRenderUser(data) {
    if (data.vaults_error) return `<div class="empty-state">Vault status failed: ${escapeHtml(data.vaults_error)}</div>`;
    const vaults = data.vaults || [];
    const ro = _embReadOnly();
    const body = vaults.length ? vaults.map(v => {
        const gear = ro ? '' : `<button class="emb-gear" title="Edit this vault" ` + wbActAttrs('showVaultForm', {vaultId: v.id}) + `>&#9881;</button>`;
        const defNote = v.is_default ? ` <span class="idx-tag" title="Default vault — not yet explicit in config. Edit to make it explicit.">default</span>` : '';
        const orphan = (!v.in_config && !v.is_default) ? ` <span class="idx-badge idx-warn" title="Removed from config; chunks remain until pruned">orphan</span>` : '';
        return `
        <tr>
            <td><strong>${escapeHtml(v.id)}</strong>${defNote}${orphan}${_embBuilding(v.building)}</td>
            <td class="idx-muted emb-path" title="${escapeHtml(v.path || '')}">${escapeHtml(v.path || '—')}</td>
            <td class="idx-r">${_embNum(v.file_count)}</td>
            <td class="idx-r">${_embNum(v.chunk_count)}</td>
            <td class="idx-r">${_embNum(v.vector_count)}</td>
            <td class="idx-r">${(v.pending > 0) ? `<span class="idx-pending">${_embNum(v.pending)}</span>` : '0'}</td>
            <td>${_embHealth(v.health)}</td>
            <td class="idx-muted">${_embDate(v.last_build)}</td>
            <td class="idx-r">${gear}</td>
        </tr>`;
    }).join('') : '<tr><td colspan="9" class="idx-muted">no vaults</td></tr>';
    const addBtn = ro ? '' : `<button class="jobs-form-submit" ` + wbActAttrs('showVaultFormNew', {}) + `>+ Add vault</button>`;
    return `
        <div class="emb-section">
            <div class="emb-section-head">
                <span class="emb-section-title">Your vaults</span>
                <span class="idx-muted">roots you index — edit path / globs</span>
                <span class="emb-section-actions">${addBtn}</span>
            </div>
            <table class="data-table">
                <thead><tr>
                    <th>Vault</th><th>Path</th><th class="idx-r">Files</th><th class="idx-r">Chunks</th>
                    <th class="idx-r">Vectors</th><th class="idx-r">Pending</th><th>Health</th><th>Last build</th><th></th>
                </tr></thead>
                <tbody>${body}</tbody>
                <tfoot><tr><td colspan="9" class="idx-muted">Size: ${_embSize(data.db_size_mb)} for all vaults</td></tr></tfoot>
            </table>
        </div>`;
}

// Inline editor — reuses the Jobs add-form styling so inputs/buttons/validation
// match the rest of the dashboard. No close "X"; Cancel/Save at the bottom (Jobs pattern).
function _embFormHtml() {
    return `
    <div id="vault-form" class="jobs-add-form" hidden>
        <div class="emb-form-title" id="vault-form-title">Add vault</div>
        <div class="job-form-row">
            <label>Vault id
                <input id="vault-form-id" type="text" autocomplete="off" placeholder="e.g. notes" />
                <small>Stable id; no '/' or '\\'. Namespaces chunk ids.</small>
            </label>
            <label>Path
                <input id="vault-form-path" type="text" autocomplete="off" placeholder="C:/path/to/root" />
                <small>Absolute path to the folder to index.</small>
            </label>
        </div>
        <div class="job-form-row">
            <label>Include globs (one per line)
                <textarea id="vault-form-include" rows="2" placeholder="**/*.md"></textarea>
            </label>
            <label>Exclude globs (one per line)
                <textarea id="vault-form-exclude" rows="2" placeholder="archive/**"></textarea>
            </label>
        </div>
        <div id="vault-form-error" class="job-form-error" hidden></div>
        <div class="job-form-actions">
            <button type="button" id="vault-form-remove" class="emb-remove-btn" ` + wbActAttrs('removeVaultFromForm', {}) + ` hidden>Remove vault</button>
            <button type="button" class="jobs-form-cancel" ` + wbActAttrs('hideVaultForm', {}) + `>Cancel</button>
            <button type="button" class="jobs-form-submit" ` + wbActAttrs('submitVaultForm', {}) + `>Save</button>
        </div>
    </div>`;
}

function showVaultForm(id) {
    const form = document.getElementById('vault-form');
    if (!form) return;
    const editing = !!id;
    const v = editing ? _embVaults.find(x => x.id === id) : null;
    document.getElementById('vault-form-title').textContent = editing ? `Edit vault "${id}"` : 'Add vault';
    const idEl = document.getElementById('vault-form-id');
    idEl.value = editing ? id : '';
    idEl.readOnly = editing;  // id is the stable key — don't rename in place (remove + re-add)
    document.getElementById('vault-form-path').value = v ? (v.path || '') : '';
    document.getElementById('vault-form-include').value = v ? (v.include || ['**/*.md']).join('\n') : '**/*.md';
    document.getElementById('vault-form-exclude').value = v ? (v.exclude || []).join('\n') : '';
    // The main vault (id "vault") is never removable — it's the user's primary
    // root (default mode, or promoted-to-explicit). Other vaults can be removed.
    document.getElementById('vault-form-remove').hidden =
        !editing || (v && (v.is_default || v.id === 'vault'));
    document.getElementById('vault-form-error').hidden = true;
    form.querySelectorAll('.jobs-form-field-invalid').forEach(e => e.classList.remove('jobs-form-field-invalid'));
    form.hidden = false;
    form.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
function hideVaultForm() { const f = document.getElementById('vault-form'); if (f) f.hidden = true; }

function _embLines(id) {
    return (document.getElementById(id).value || '').split('\n').map(s => s.trim()).filter(Boolean);
}

async function _embPost(url, payload) {
    try {
        const resp = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify(payload)});
        return await resp.json();
    } catch (e) {
        settingsToast(`Network error: ${e.message}`, 'error');
        return null;
    }
}

function _embShowFormError(result) {
    const err = document.getElementById('vault-form-error');
    if (result && result.errors_by_field) {
        const map = {id: 'vault-form-id', path: 'vault-form-path', globs: 'vault-form-include'};
        for (const k of Object.keys(result.errors_by_field)) {
            const el = document.getElementById(map[k]);
            if (el) el.classList.add('jobs-form-field-invalid');
        }
        err.textContent = Object.values(result.errors_by_field).join(' ');
    } else {
        err.textContent = (result && result.error) || 'Save failed.';
    }
    err.hidden = false;
}

async function submitVaultForm() {
    if (_embReadOnly()) return;
    document.getElementById('vault-form-error').hidden = true;
    document.querySelectorAll('#vault-form .jobs-form-field-invalid').forEach(e => e.classList.remove('jobs-form-field-invalid'));
    const result = await _embPost('/api/embeddings/vault', {
        action: 'set',
        id: document.getElementById('vault-form-id').value.trim(),
        path: document.getElementById('vault-form-path').value.trim(),
        include: _embLines('vault-form-include'),
        exclude: _embLines('vault-form-exclude'),
    });
    if (!result || !result.success) { _embShowFormError(result); return; }
    if (result.warning) settingsToast(result.warning, 'info');
    settingsToast(result.note || 'Saved.', 'success');
    hideVaultForm();
    await loadEmbeddings();
}

function removeVaultFromForm() {
    const id = document.getElementById('vault-form-id').value.trim();
    if (id) removeVault(id);
}

async function removeVault(id) {
    if (_embReadOnly()) return;
    if (!confirm(`Remove vault "${id}" from config?\n\nIts already-indexed chunks stay searchable until an explicit prune.`)) return;
    const result = await _embPost('/api/embeddings/vault', {action: 'remove', id: id});
    if (!result || !result.success) { settingsToast((result && result.error) || 'Remove failed', 'error'); return; }
    settingsToast(result.note || 'Removed.', 'success');
    hideVaultForm();
    await loadEmbeddings();
}

// Event delegation adapters
window.wbAction('showVaultForm', function (el) { showVaultForm(el.dataset.vaultId); });
window.wbAction('showVaultFormNew', function (el) { showVaultForm(); });
window.wbAction('hideVaultForm', function (el) { hideVaultForm(); });
window.wbAction('removeVaultFromForm', function (el) { removeVaultFromForm(); });
window.wbAction('submitVaultForm', function (el) { submitVaultForm(); });
"""


def styles() -> str:
    return """
/* shared index-table primitives (theme-variable names match the dashboard) */
.idx-r { text-align:right; font-variant-numeric: tabular-nums; }
.idx-muted { color: var(--text-muted); font-size: 0.85em; }
.idx-pending { color: var(--yellow); font-weight:600; }
.idx-tag { color: var(--text-muted); font-size: 0.8em; }
.idx-badge { font-size:0.75em; padding:1px 7px; border-radius:10px; font-weight:600; display:inline-block; }
.idx-ok { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
.idx-warn { background: color-mix(in srgb, var(--yellow) 18%, transparent); color: var(--yellow); }
.idx-err { background: color-mix(in srgb, var(--red) 18%, transparent); color: var(--red); }
.idx-building { background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent); }

/* embeddings sub-view */
.emb-section { margin-bottom: 22px; }
.emb-section-head { display:flex; align-items:baseline; gap:10px; margin-bottom:6px; flex-wrap:wrap; }
.emb-section-title { font-weight:600; font-size:1.05em; color: var(--text-primary); }
.emb-section-actions { margin-left:auto; display:flex; gap:8px; }
.emb-path { max-width: 360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.emb-gear { background:none; border:none; cursor:pointer; font-size:1.05em; opacity:0.7; padding:2px 4px; color: var(--text-secondary); }
.emb-gear:hover { opacity:1; }
/* form title sits above the reused .jobs-add-form fields */
.emb-form-title { font-weight:600; font-size:13px; margin-bottom:10px; color: var(--text-primary); }
/* destructive action, left-aligned within .job-form-actions (Cancel/Save stay right) */
.emb-remove-btn { margin-right:auto; border:1px solid var(--red); border-radius:4px;
    padding:6px 14px; font-size:12px; font-weight:600; cursor:pointer;
    background: var(--bg-tertiary); color: var(--red); }
.emb-remove-btn:hover { background: color-mix(in srgb, var(--red) 14%, var(--bg-tertiary)); }
"""
