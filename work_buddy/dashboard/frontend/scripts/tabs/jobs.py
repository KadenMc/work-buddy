"""Dashboard Jobs tab JS — user + system scheduled jobs.

Owns the Jobs tab loader, the user-vs-system job tables, and the
Add-job form (parameter schema rendering, cron preview, registry
type-ahead, and submission). Publishes ``window.jobsSurface`` so the
SSE event bus can refresh on ``user_job.created`` / ``cron.hot_reload``
without polling.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Jobs ----
// Tracks job names that were just created by the form but haven't yet
// appeared in the daemon's view (it hot-reloads every ~30s). Each entry
// renders as a "pending" banner above the user-jobs table; entries
// auto-clear once the name appears in the loaded jobs list.
const _jobsPendingCreate = new Map();  // name -> {createdAt: ms, deadline: ms}

// Surface handle for the Jobs tab. SSE handlers in script_event_bus.py
// call refresh() on user_job.created (immediate) and cron.hot_reload
// (when the sidecar's scheduler picks the file up, ~30s later) so the
// table updates without polling.
window.jobsSurface = {
    refresh: function() { return loadJobs(); },
    isMounted: function() { return !!document.getElementById('jobs-user'); },
};

async function loadJobs() {
    const data = await fetchJSON('/api/state');
    if (!data) return;

    const jobs = data.jobs || [];
    const userJobs = jobs.filter(j => j.source === 'user');
    const systemJobs = jobs.filter(j => j.source !== 'user');

    // Drop any pending banners whose job has now appeared, or expired
    const now = Date.now();
    const userNames = new Set(userJobs.map(j => j.name));
    for (const [name, info] of _jobsPendingCreate.entries()) {
        if (userNames.has(name) || now > info.deadline) {
            _jobsPendingCreate.delete(name);
        }
    }

    let pendingHtml = '';
    if (_jobsPendingCreate.size > 0) {
        const rows = Array.from(_jobsPendingCreate.keys()).map(n =>
            `<li><code>${n}</code></li>`).join('');
        pendingHtml = `
            <div class="jobs-pending-banner">
                <strong>Created.</strong> Will appear in the table shortly.
                <ul>${rows}</ul>
            </div>`;
    }

    document.getElementById('jobs-user').innerHTML = pendingHtml + renderJobsTable(
        userJobs,
        `<div class="empty-state">
            No personal jobs yet. Click <strong>+ Add job</strong> above to create one.
         </div>`,
    );
    document.getElementById('jobs-system').innerHTML = renderJobsTable(
        systemJobs,
        '<div class="empty-state">No system jobs registered.</div>',
    );
}

function renderJobsTable(jobs, emptyHtml) {
    if (!jobs || jobs.length === 0) return emptyHtml;
    const rows = jobs.map(j => `
        <tr>
            <td>${j.name}</td>
            <td title="${j.schedule}">${j.schedule_desc || j.schedule}</td>
            <td>${j.last_result ? statusBadge(j.last_result, j.last_error) : '\u2014'}</td>
            <td>${timeAgo(j.last_run_at)}</td>
            <td>${timeUntil(j.next_at)}</td>
        </tr>
    `).join('');
    return `
        <table class="data-table">
            <thead><tr><th>Job</th><th>Schedule</th><th>Last Result</th><th>Last Run</th><th>Next Run</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

// ---- Add-job form ----
// Cached registry list (capabilities + workflows). The first call to
// /api/registry/list in the dashboard process triggers a full registry
// build (10-20s cold), so we kick the fetch at page-load time — by the
// time the user clicks Add Job it's almost always warm.
let _jobRegistry = null;
let _jobRegistryPromise = null;

function _loadJobRegistry() {
    if (_jobRegistryPromise) return _jobRegistryPromise;
    _jobRegistryPromise = fetch('/api/registry/list')
        .then(r => r.json())
        .then(j => { _jobRegistry = j; return j; })
        .catch(() => {
            _jobRegistry = {capabilities: [], workflows: []};
            return _jobRegistry;
        });
    return _jobRegistryPromise;
}

async function showAddJobForm() {
    document.getElementById('jobs-add-form').hidden = false;
    document.getElementById('jobs-add-btn').hidden = true;
    document.getElementById('job-form-name').focus();
    // Surface a loading hint immediately so the schema slot is not silent
    // if the registry is still cold-loading.
    const slot = document.getElementById('job-form-params-schema');
    if (slot && !_jobRegistry) {
        slot.innerHTML = '<em>Loading capabilities…</em>';
        slot.hidden = false;
    }
    await _loadJobRegistry();
    rebuildInvokeDatalist();
    // Re-render schema for whatever the user has typed in the meantime
    // (covers the cold-start case where the user typed before fetch landed).
    onInvokeNameInput();
}

function hideAddJobForm() {
    document.getElementById('jobs-add-form').hidden = true;
    document.getElementById('jobs-add-btn').hidden = false;
    // Clear inputs and any error so a re-open starts fresh
    ['job-form-name','job-form-schedule','job-form-prompt',
     'job-form-invoke-name','job-form-params']
        .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    document.getElementById('job-form-type').value = 'prompt';
    document.getElementById('job-form-invoke-kind').value = 'capability';
    document.getElementById('job-form-invoke-hint').textContent = '';
    const schema = document.getElementById('job-form-params-schema');
    if (schema) { schema.hidden = true; schema.innerHTML = ''; }
    onJobTypeChange();
    onInvokeKindChange();
    resetCronPreview();
    resetParamsValidity();
    const err = document.getElementById('job-form-error');
    err.hidden = true; err.textContent = '';
}

function onJobTypeChange() {
    const t = document.getElementById('job-form-type').value;
    document.getElementById('job-form-prompt-row').hidden = (t !== 'prompt');
    document.getElementById('job-form-invoke-row').hidden = (t !== 'invoke');
}

function onInvokeKindChange() {
    const kind = document.getElementById('job-form-invoke-kind').value;
    document.getElementById('job-form-invoke-name-label').textContent =
        kind === 'workflow' ? 'Workflow name' : 'Capability name';
    document.getElementById('job-form-invoke-name').placeholder =
        kind === 'workflow' ? 'morning-routine' : 'task_briefing';
    // Re-populate the datalist with the right slice + reset description hint
    rebuildInvokeDatalist();
    document.getElementById('job-form-invoke-hint').textContent = '';
    // Re-evaluate schema for the new kind (clears it if the current name
    // doesn't match an entry under the new kind). Params field visibility
    // is then driven by whether the picked entry declares parameters.
    onInvokeNameInput();
}

function rebuildInvokeDatalist() {
    const dl = document.getElementById('job-form-invoke-options');
    if (!dl) return;
    if (!_jobRegistry) { dl.innerHTML = ''; return; }
    const kind = document.getElementById('job-form-invoke-kind').value;
    const entries = (kind === 'workflow' ? _jobRegistry.workflows : _jobRegistry.capabilities) || [];
    dl.innerHTML = entries.map(e => {
        const desc = (e.description || '').replace(/"/g, '&quot;');
        return `<option value="${e.name}" label="${desc}">${desc}</option>`;
    }).join('');
}

function onInvokeNameInput() {
    if (!_jobRegistry) return;
    const kind = document.getElementById('job-form-invoke-kind').value;
    const entries = (kind === 'workflow' ? _jobRegistry.workflows : _jobRegistry.capabilities) || [];
    const value = document.getElementById('job-form-invoke-name').value.trim();
    const hit = entries.find(e => e.name === value);
    document.getElementById('job-form-invoke-hint').textContent =
        hit ? hit.description : '';
    renderParamsSchema(kind, hit);
    // The schema the params textarea is checked against just changed —
    // re-evaluate validity so the message stays truthful.
    onParamsInput();
}

// Render the parameter schema below the params textarea — works for
// both capabilities and workflows. The params textarea itself stays
// hidden for entries that declare no parameters (workflows without a
// declared schema, or capabilities with empty parameters).
function renderParamsSchema(kind, entry) {
    const slot = document.getElementById('job-form-params-schema');
    const wrap = document.getElementById('job-form-params-wrap');
    if (!slot) return;
    const params = entry && entry.parameters;
    const hasParams = !!(params && params.length > 0);
    // Show the params textarea only for entries that actually declare params.
    // Unknown / not-yet-resolved entries default to showing the textarea so
    // the user isn't blocked by a slow registry fetch.
    if (wrap) wrap.hidden = entry ? !hasParams : false;
    if (!hasParams) {
        if (entry) {
            slot.innerHTML = `<em>This ${kind} takes no parameters.</em>`;
            slot.hidden = false;
        } else {
            slot.hidden = true; slot.innerHTML = '';
        }
        return;
    }
    const escape = s => String(s ?? '').replace(/[&<>"']/g, c =>
        ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const rows = params.map(p => {
        const tag = p.required ? `<span class="schema-required">required</span>`
                                : `<span class="schema-optional">optional</span>`;
        const type = p.type ? `<code>${escape(p.type)}</code>` : '';
        return `<li>
            <span class="schema-name"><code>${escape(p.name)}</code></span>
            ${type} ${tag}
            <div class="schema-desc">${escape(p.description)}</div>
        </li>`;
    }).join('');
    slot.innerHTML = `<div class="schema-title">Parameters</div><ul>${rows}</ul>`;
    slot.hidden = false;
}

// Cron preview: debounced fetch to /api/cron/describe so the user sees
// "Every 15 minutes" rendered live as they type a valid expression.
let _cronPreviewTimer = null;
const _CRON_PREVIEW_DEFAULT = `5-field cron (MIN HOUR DOM MON DOW). ` +
    `Example: <code>*/15 * * * *</code> = every 15 min.`;
function resetCronPreview() {
    const el = document.getElementById('job-form-cron-preview');
    el.classList.remove('cron-preview-valid', 'cron-preview-invalid');
    el.classList.add('cron-preview-hint');
    el.innerHTML = _CRON_PREVIEW_DEFAULT;
}
// Validate the params textarea against the picked entry's parameters
// schema — same rules as the backend (no unknown keys, all required
// keys present). Empty input is fine. The indicator's message describes
// exactly what was checked so it doesn't lie about validity.
const _PARAMS_HINT_DEFAULT = 'Empty, or a JSON object matching the parameters above.';
function resetParamsValidity() {
    const el = document.getElementById('job-form-params-validity');
    if (!el) return;
    el.classList.remove('cron-preview-valid', 'cron-preview-invalid');
    el.classList.add('cron-preview-hint');
    el.textContent = _PARAMS_HINT_DEFAULT;
}
// Returns the parameters array for the entry currently selected in the
// invoke section, or null when nothing is selected / registry not loaded.
function _currentInvokeSchema() {
    if (!_jobRegistry) return null;
    const ui_type = document.getElementById('job-form-type')?.value;
    if (ui_type !== 'invoke') return null;
    const kind = document.getElementById('job-form-invoke-kind').value;
    const name = document.getElementById('job-form-invoke-name').value.trim();
    if (!name) return null;
    const entries = (kind === 'workflow' ? _jobRegistry.workflows : _jobRegistry.capabilities) || [];
    const hit = entries.find(e => e.name === name);
    return hit ? (hit.parameters || []) : null;
}
function onParamsInput() {
    const el = document.getElementById('job-form-params-validity');
    if (!el) return;
    const raw = document.getElementById('job-form-params').value.trim();
    if (!raw) { resetParamsValidity(); return; }

    // Parse + shape check
    let parsed;
    try { parsed = JSON.parse(raw); }
    catch (e) {
        el.classList.remove('cron-preview-hint', 'cron-preview-valid');
        el.classList.add('cron-preview-invalid');
        el.textContent = `✗ Invalid JSON: ${e.message}`;
        return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        el.classList.remove('cron-preview-hint', 'cron-preview-valid');
        el.classList.add('cron-preview-invalid');
        el.textContent = '✗ Must be a JSON object (e.g. {"key": "value"}).';
        return;
    }

    // Schema validation against the picked entry's parameters
    const schema = _currentInvokeSchema();
    const givenKeys = Object.keys(parsed);
    if (schema === null) {
        // No selected entry / registry pending — surface JSON-ok but be honest
        // that we can't yet check key names against any schema.
        el.classList.remove('cron-preview-hint', 'cron-preview-invalid');
        el.classList.add('cron-preview-valid');
        el.textContent = `✓ Valid JSON object (${givenKeys.length} ${givenKeys.length === 1 ? 'key' : 'keys'}). Pick a capability/workflow above to check key names.`;
        return;
    }

    if (schema.length === 0) {
        // Entry declares no parameters — backend will reject any non-empty params.
        el.classList.remove('cron-preview-hint', 'cron-preview-valid');
        el.classList.add('cron-preview-invalid');
        el.textContent = '✗ This entry takes no parameters; remove the JSON or pick a different one.';
        return;
    }

    const declared = new Set(schema.map(p => p.name));
    const required = schema.filter(p => p.required).map(p => p.name);
    const unknown = givenKeys.filter(k => !declared.has(k));
    const missing = required.filter(k => !(k in parsed));

    if (unknown.length || missing.length) {
        const parts = [];
        if (missing.length) parts.push(`missing required: ${missing.map(k => `\`${k}\``).join(', ')}`);
        if (unknown.length) parts.push(`unknown: ${unknown.map(k => `\`${k}\``).join(', ')}`);
        el.classList.remove('cron-preview-hint', 'cron-preview-valid');
        el.classList.add('cron-preview-invalid');
        el.innerHTML = `✗ ${parts.join('; ')}.`;
        return;
    }

    el.classList.remove('cron-preview-hint', 'cron-preview-invalid');
    el.classList.add('cron-preview-valid');
    el.textContent = `✓ Matches schema (${givenKeys.length} of ${schema.length} declared keys).`;
}

function onCronInput() {
    const expr = document.getElementById('job-form-schedule').value.trim();
    const el = document.getElementById('job-form-cron-preview');
    if (_cronPreviewTimer) { clearTimeout(_cronPreviewTimer); _cronPreviewTimer = null; }
    if (!expr) { resetCronPreview(); return; }
    _cronPreviewTimer = setTimeout(async () => {
        try {
            const resp = await fetch(`/api/cron/describe?expr=${encodeURIComponent(expr)}`);
            const data = await resp.json();
            el.classList.remove('cron-preview-hint');
            if (data.valid) {
                el.classList.remove('cron-preview-invalid');
                el.classList.add('cron-preview-valid');
                el.textContent = data.description;
            } else {
                el.classList.remove('cron-preview-valid');
                el.classList.add('cron-preview-invalid');
                el.textContent = "Doesn't parse as a 5-field cron expression.";
            }
        } catch (_) {
            // Network blip — leave the preview in its current state rather
            // than flashing a misleading "invalid" message.
        }
    }, 250);
}

async function submitAddJobForm() {
    const errEl = document.getElementById('job-form-error');
    errEl.hidden = true; errEl.textContent = '';

    const ui_type = document.getElementById('job-form-type').value;
    const payload = {
        name: document.getElementById('job-form-name').value.trim(),
        schedule: document.getElementById('job-form-schedule').value.trim(),
    };
    if (ui_type === 'prompt') {
        payload.job_type = 'prompt';
        payload.prompt = document.getElementById('job-form-prompt').value;
    } else {
        // ui_type === 'invoke' — backend job_type comes from the kind sub-select
        const kind = document.getElementById('job-form-invoke-kind').value;
        const invokeName = document.getElementById('job-form-invoke-name').value.trim();
        payload.job_type = kind;
        if (kind === 'capability') {
            payload.capability = invokeName;
        } else {
            payload.workflow = invokeName;
        }
        // Params apply to both kinds (workflows reach __params__ via input_map).
        // The form only shows the textarea when the entry declares params,
        // but we still parse defensively in case the user typed something.
        const raw = document.getElementById('job-form-params').value.trim();
        if (raw) {
            try {
                payload.params = JSON.parse(raw);
            } catch (e) {
                errEl.textContent = `Params must be valid JSON: ${e.message}`;
                errEl.hidden = false;
                return;
            }
        }
    }

    let resp;
    try {
        resp = await fetch('/api/user_jobs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
    } catch (e) {
        errEl.textContent = `Network error: ${e.message}`;
        errEl.hidden = false;
        return;
    }
    const result = await resp.json().catch(() => ({}));
    if (!resp.ok || !result.success) {
        errEl.textContent = result.error || `Request failed (HTTP ${resp.status})`;
        errEl.hidden = false;
        return;
    }
    // Stash a pending banner so the user gets immediate feedback even though
    // the daemon's reload (~30s) hasn't yet picked the file up. Banner clears
    // automatically once the job appears in /api/state, or after 60s.
    // The dashboard endpoint publishes a user_job.created event on the bus,
    // which jobsSurface subscribes to — so this loadJobs() is paired with
    // an SSE-driven refresh later when the sidecar's cron.hot_reload fires.
    if (result.name) {
        _jobsPendingCreate.set(result.name, {
            createdAt: Date.now(),
            deadline: Date.now() + 60_000,
        });
    }
    hideAddJobForm();
    await loadJobs();
}
"""
