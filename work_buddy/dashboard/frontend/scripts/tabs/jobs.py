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
// Tracks job names that were just changed via the dashboard but haven't
// yet been picked up by the sidecar's filesystem watcher (~50ms in
// practice; 30s polling fallback for non-NTFS edge cases). Each map
// renders a colored banner above the user-jobs table:
//   * Create   — green, clears when the name APPEARS in the list.
//   * Delete   — red,   clears when the name DISAPPEARS from the list.
//   * Edit     — neutral (blue), clears on a hard timeout (overwrite
//                replaces the file in place — no append/remove signal
//                we can react to, so we time out).
// All three deadlines default to 60s so the banner doesn't linger
// forever if the watcher is slow / off.
const _jobsPendingCreate = new Map();  // name -> {createdAt: ms, deadline: ms}
const _jobsPendingDelete = new Map();  // name -> {createdAt: ms, deadline: ms}
const _jobsPendingEdit   = new Map();  // name -> {createdAt: ms, deadline: ms}

// Helper used by all three flows. Stashes a name in the matching map,
// repaints the banner slot **immediately** (no /api/state fetch — the
// banner is purely local UI state about something the user just did).
// The banner's lifetime is driven by the *change actually landing*:
//
//   * Create → name appears in /api/state's user-job list.
//   * Delete → name disappears from that list.
//   * Edit   → ``cron.hot_reload`` event fires after the banner went up
//              (the sidecar's scheduler has reloaded with the new file).
//
// A 60-second safety-net timer expires the banner if none of the
// natural signals ever arrive (e.g. sidecar dead). A short minimum-
// visibility floor guarantees the user actually sees the banner even
// on a fast happy path — without it, a sub-100ms hot-reload would
// flash too quickly to register.
const _JOBS_BANNER_MIN_VISIBLE_MS = 1200;
const _JOBS_BANNER_SAFETY_TTL_MS = 60_000;

function _jobsMarkPending(map, name) {
    if (!name) return;
    const now = Date.now();
    map.set(name, {
        createdAt: now,
        minVisibleUntil: now + _JOBS_BANNER_MIN_VISIBLE_MS,
        deadline: now + _JOBS_BANNER_SAFETY_TTL_MS,
    });
    _renderJobsBanners();
    setTimeout(() => {
        // Safety net only — natural signals usually clear earlier.
        if (map.has(name)) {
            map.delete(name);
            _renderJobsBanners();
        }
    }, _JOBS_BANNER_SAFETY_TTL_MS + 50);
}

// Try to clear an entry; respects the minimum-visible floor so a
// sub-second sidecar reload doesn't flash the banner too quickly.
// Schedules a deferred retry if the floor hasn't elapsed yet.
function _jobsTryClearPending(map, name) {
    const info = map.get(name);
    if (!info) return false;
    const remaining = info.minVisibleUntil - Date.now();
    if (remaining > 0) {
        setTimeout(() => _jobsTryClearPending(map, name), remaining + 20);
        return false;
    }
    map.delete(name);
    _renderJobsBanners();
    return true;
}

// cron.hot_reload signals that the sidecar's *scheduler* has picked
// up the filesystem change — but the dashboard's table doesn't reflect
// the new state until the next ``loadJobs`` completes its
// ``/api/state`` round-trip. Clearing the edit banner directly on
// hot_reload disconnects it from the visible table update (banner
// vanishes at the 1.2s floor while the table is still stale on a
// slow /api/state). Instead, MARK the entry as eligible-for-clear
// here; ``loadJobs`` does the actual clear *after* it renders the
// fresh table, keeping the two transitions visually in sync.
if (window.eventBus && typeof window.eventBus.on === 'function') {
    window.eventBus.on('cron.hot_reload', () => {
        for (const info of _jobsPendingEdit.values()) {
            info.hotReloadSeen = true;
        }
    });
}

// Paint the dedicated banner slot from the three pending Maps. Cheap;
// no network. Safe to call as often as we want.
function _renderJobsBanners() {
    const slot = document.getElementById('jobs-pending-banners');
    if (!slot) return;
    function _renderOne(map, cssVariant, headline, body) {
        if (map.size === 0) return '';
        const rows = Array.from(map.keys()).map(n =>
            `<li><code>${n}</code></li>`).join('');
        return `
            <div class="jobs-pending-banner ${cssVariant}">
                <strong>${headline}</strong> ${body}
                <ul>${rows}</ul>
            </div>`;
    }
    slot.innerHTML =
        _renderOne(_jobsPendingCreate, '',
            'Created.', 'Will appear in the table shortly.')
        + _renderOne(_jobsPendingEdit, 'jobs-pending-banner-neutral',
            'Updated.', 'The change will be picked up by the scheduler shortly.')
        + _renderOne(_jobsPendingDelete, 'jobs-pending-banner-destructive',
            'Deleted.', 'Will disappear from the table shortly.');
}

// Surface handle for the Jobs tab. SSE handlers in core/event_bus.py
// call refresh() on user_job.created (immediate) and cron.hot_reload
// (when the sidecar's scheduler picks the file up, ~30s later) so the
// table updates without polling.
window.jobsSurface = {
    refresh: function() { return loadJobs(); },
    isMounted: function() { return !!document.getElementById('jobs-user'); },
};

// ---- Form-bridge registration ----
//
// Schema-driven agent ↔ form integration. The dashboard_interact MCP
// capability addresses this form via form_id="jobs-add-job"; the
// bridge dispatches each event to the matching handler below.
//
// Knowledge of which DOM input each schema field maps to lives ONLY
// here — the brief generator (interact_brief.render_form_section)
// describes fields by their canonical names, never by ui_id.
function _wbJobsSetInput(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    if (document.activeElement === el) return;  // don't clobber typing
    if (el.value !== value) el.value = value;
}

function _wbJobsSetTypeAndKind(jobType) {
    const selectEl = document.getElementById('job-form-type');
    const kindEl = document.getElementById('job-form-invoke-kind');
    if (!selectEl) return;
    if (jobType === 'prompt') {
        selectEl.value = 'prompt';
    } else if (jobType === 'capability' || jobType === 'workflow') {
        selectEl.value = 'invoke';
        if (kindEl) kindEl.value = jobType;
    }
    if (typeof onJobTypeChange === 'function') onJobTypeChange();
    if ((jobType === 'capability' || jobType === 'workflow')
        && typeof onInvokeKindChange === 'function') {
        onInvokeKindChange();
    }
}

function _wbJobsReadFormState() {
    const get = (id) => (document.getElementById(id) || {}).value || '';
    const typeSel = get('job-form-type');
    const kindSel = get('job-form-invoke-kind');
    const out = {
        name: get('job-form-name'),
        schedule: get('job-form-schedule'),
        prompt: get('job-form-prompt'),
        params: get('job-form-params'),
    };
    if (typeSel === 'prompt') {
        out.job_type = 'prompt';
    } else if (typeSel === 'invoke') {
        out.job_type = (kindSel === 'workflow') ? 'workflow' : 'capability';
        if (out.job_type === 'capability') out.capability = get('job-form-invoke-name');
        else out.workflow = get('job-form-invoke-name');
    }
    return out;
}

if (window.wbFormBridge && typeof window.wbFormBridge.register === 'function') {
    window.wbFormBridge.register('jobs-add-job', {
        fieldHandlers: {
            name:       v => _wbJobsSetInput('job-form-name', v),
            schedule:   v => {
                _wbJobsSetInput('job-form-schedule', v);
                if (typeof onCronInput === 'function') onCronInput();
            },
            job_type:   v => _wbJobsSetTypeAndKind(v),
            capability: v => {
                _wbJobsSetInput('job-form-invoke-name', v);
                if (typeof onInvokeNameInput === 'function') onInvokeNameInput();
            },
            workflow:   v => {
                _wbJobsSetInput('job-form-invoke-name', v);
                if (typeof onInvokeNameInput === 'function') onInvokeNameInput();
            },
            prompt:     v => _wbJobsSetInput('job-form-prompt', v),
            params:     v => {
                try {
                    const json = JSON.stringify(v || {}, null, 2);
                    _wbJobsSetInput('job-form-params', json);
                    if (typeof onParamsInput === 'function') onParamsInput();
                } catch (e) { /* non-serializable — skip */ }
            },
        },
        // submitHandler / getStateHandler arrive in step 4 of the bridge
        // build (rendezvous-backed). Until then, the brief still uses the
        // bespoke /api/user_jobs/help/form_submit endpoint.
        openHandler: () => {
            if (typeof showAddJobForm === 'function') {
                showAddJobForm();
                // showAddJobForm focuses the name input as a UX nicety;
                // blur it so subsequent agent-driven field_set calls
                // are not skipped by the focus-guard in _wbJobsSetInput.
                if (document.activeElement && document.activeElement.blur) {
                    document.activeElement.blur();
                }
            }
        },
        cancelHandler: () => {
            // Same path the user takes by clicking the form's "Cancel"
            // button — clears the inputs and hides the form. Used by
            // the chat agent when the user explicitly opts out.
            if (typeof hideAddJobForm === 'function') hideAddJobForm();
        },
        getStateHandler: () => _wbJobsReadFormState(),
        // Reuses the user's manual-flow submit. Going through
        // submitAddJobForm keeps the agent path identical to the
        // user clicking "Create job" — same validation, same payload
        // shape, same destination endpoint. If we ever change any of
        // those, the agent flow inherits the change for free.
        submitHandler: async () => {
            if (typeof submitAddJobForm !== 'function') {
                return { ok: false, error: 'submitAddJobForm not loaded' };
            }
            try {
                const r = await submitAddJobForm();
                if (r && r.success) {
                    return { ok: true, name: r.name };
                }
                return {
                    ok: false,
                    error: (r && r.error) || 'submission failed',
                    raw: r,
                };
            } catch (err) {
                return { ok: false, error: 'submitAddJobForm threw: ' + err };
            }
        },
    });
}

async function loadJobs() {
    const data = await fetchJSON('/api/state');
    if (!data) return;

    const jobs = data.jobs || [];
    const userJobs = jobs.filter(j => j.source === 'user');
    const systemJobs = jobs.filter(j => j.source !== 'user');

    // Banners are owned by ``_renderJobsBanners`` (painted into a
    // separate DOM slot, decoupled from this fetch). loadJobs is the
    // place where the table-driven banner-clear signals fire:
    //   * Create banner clears once the row APPEARS in the list.
    //   * Delete banner clears once the row DISAPPEARS from it.
    //   * Edit banner clears once a cron.hot_reload event has been
    //     observed AND this loadJobs has rendered the fresh table.
    //     Doing the clear here (instead of in the hot_reload handler
    //     directly) keeps the banner removal in sync with the
    //     table's actual data update — on a slow /api/state, the
    //     hot_reload arrives long before /api/state's response, and
    //     clearing the banner at hot_reload time would leave the
    //     stale table visible without the "Updated." indicator.
    const userNames = new Set(userJobs.map(j => j.name));
    for (const name of Array.from(_jobsPendingCreate.keys())) {
        if (userNames.has(name)) _jobsTryClearPending(_jobsPendingCreate, name);
    }
    for (const name of Array.from(_jobsPendingDelete.keys())) {
        if (!userNames.has(name)) _jobsTryClearPending(_jobsPendingDelete, name);
    }
    for (const [name, info] of Array.from(_jobsPendingEdit.entries())) {
        if (info.hotReloadSeen) _jobsTryClearPending(_jobsPendingEdit, name);
    }

    document.getElementById('jobs-user').innerHTML = renderJobsTable(
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

// Feather-style 16x16 SVG icons. Stroke-only so they inherit the
// surrounding text color and theme cleanly (used by the row-action
// buttons below). The pencil + trash paths are lifted from
// scripts/tabs/threads/card.py so the visual language matches.
function _wbJobIcon(name) {
    const paths = {
        edit: '<path d="M12 20h9"></path>'
            + '<path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path>',
        trash: '<polyline points="3 6 5 6 21 6"></polyline>'
             + '<path d="M19 6l-1 14a2 2 0 0 1 -2 2H8a2 2 0 0 1-2-2L5 6"></path>',
    };
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
         + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
         + 'stroke-linejoin="round">' + (paths[name] || '') + '</svg>';
}

function renderJobsTable(jobs, emptyHtml) {
    if (!jobs || jobs.length === 0) return emptyHtml;
    const rows = jobs.map(j => {
        // Only user-authored jobs get edit/delete affordances. System
        // jobs ship with the repo and editing them from the dashboard
        // would silently shadow the source-controlled file.
        const actions = (j.source === 'user') ? `
            <td class="jobs-row-actions">
                <button class="jobs-row-icon-btn" type="button"
                        title="Edit this job"
                        onclick="onEditJobClick(${JSON.stringify(j.name).replace(/"/g, '&quot;')})">
                    ${_wbJobIcon('edit')}
                </button>
                <button class="jobs-row-icon-btn jobs-row-icon-destructive" type="button"
                        title="Delete this job"
                        onclick="onDeleteJobClick(${JSON.stringify(j.name).replace(/"/g, '&quot;')})">
                    ${_wbJobIcon('trash')}
                </button>
            </td>` : '<td class="jobs-row-actions"></td>';
        // Prefer effective_at (next_at + jitter offset, or queued
        // pending due time) when present, falling back to next_at for
        // back-compat with older sidecar_state.json shapes.
        const fireAt = j.effective_at || j.next_at;
        const jitter = j.jitter_seconds || 0;
        const nextCell = jitter > 0
            ? `${timeUntil(fireAt)}<span class="jobs-jitter-tag" title="`
                + `Schedule has jitter_seconds=${jitter}; deterministic offset spreads firing.`
                + `">+${jitter}s jit</span>`
            : timeUntil(fireAt);
        return `
            <tr>
                <td>${j.name}</td>
                <td title="${j.schedule}">${j.schedule_desc || j.schedule}</td>
                <td>${j.last_result ? statusBadge(j.last_result, j.last_error) : '\u2014'}</td>
                <td>${timeAgo(j.last_run_at)}</td>
                <td>${nextCell}</td>
                ${actions}
            </tr>
        `;
    }).join('');
    return `
        <table class="data-table">
            <thead><tr>
                <th>Job</th><th>Schedule</th><th>Last Result</th>
                <th>Last Run</th><th>Next Run</th>
                <th class="jobs-row-actions"></th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

// ---- Edit / delete handlers ----
//
// Track whether the form is currently open in edit mode (and for which
// job name). Submission needs to know to set ``overwrite: true`` and
// keep the same name; cancel needs to reset the flag.
let _editingJobName = null;

function _setEditMode(name) {
    _editingJobName = name || null;
    const submitBtn = document.querySelector('.jobs-form-submit');
    if (submitBtn) {
        submitBtn.textContent = name ? 'Save changes' : 'Create job';
    }
    const nameEl = document.getElementById('job-form-name');
    if (nameEl) {
        nameEl.disabled = !!name;
        nameEl.title = name ? 'Name cannot be changed for an existing job.' : '';
    }
}

async function onEditJobClick(name) {
    if (!name) return;
    let resp;
    try {
        resp = await fetch('/api/user_jobs/' + encodeURIComponent(name));
    } catch (e) {
        alert('Network error loading job: ' + e.message);
        return;
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
        alert(data.error || 'Failed to load job for editing.');
        return;
    }

    if (typeof showAddJobForm === 'function') await showAddJobForm();

    document.getElementById('job-form-name').value = data.name || '';
    document.getElementById('job-form-schedule').value = data.schedule || '';
    if (typeof onCronInput === 'function') onCronInput();
    if (data.job_type === 'prompt') {
        document.getElementById('job-form-type').value = 'prompt';
        if (typeof onJobTypeChange === 'function') onJobTypeChange();
        document.getElementById('job-form-prompt').value = data.prompt || '';
    } else {
        document.getElementById('job-form-type').value = 'invoke';
        if (typeof onJobTypeChange === 'function') onJobTypeChange();
        document.getElementById('job-form-invoke-kind').value = data.job_type || 'capability';
        if (typeof onInvokeKindChange === 'function') onInvokeKindChange();
        const invokeName = data.job_type === 'workflow' ? data.workflow : data.capability;
        document.getElementById('job-form-invoke-name').value = invokeName || '';
        if (data.params && Object.keys(data.params).length) {
            document.getElementById('job-form-params').value = JSON.stringify(data.params, null, 2);
            if (typeof onParamsInput === 'function') onParamsInput();
        }
        if (typeof onInvokeNameInput === 'function') onInvokeNameInput();
    }
    _setEditMode(name);
}

async function onDeleteJobClick(name) {
    if (!name) return;
    if (!confirm(`Delete the job "${name}"? This removes its file under .data/user_jobs/ and cannot be undone.`)) {
        return;
    }
    let resp;
    try {
        resp = await fetch('/api/user_jobs/' + encodeURIComponent(name), { method: 'DELETE' });
    } catch (e) {
        alert('Network error: ' + e.message);
        return;
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
        alert(data.error || 'Failed to delete job.');
        return;
    }
    // Stash a "Deleted" banner — clears once the row vanishes from
    // /api/state (sidecar's filesystem watcher catches up in ~50ms),
    // honoring the minimum-visibility floor.
    _jobsMarkPending(_jobsPendingDelete, data.name || name);
    // The dashboard publishes user_job.deleted on success; jobsSurface
    // refresh hooks fire from event_bus.py. Belt-and-suspenders direct
    // refresh covers the case where the SSE stream is briefly idle.
    if (typeof loadJobs === 'function') loadJobs();
}

// ---- Add-job form ----
// Cached registry list (capabilities + workflows). The first call to
// /api/registry/list in the dashboard process triggers a full registry
// build (10-20s cold), so we kick the fetch at page-load time — by the
// time the user clicks Add Job it's almost always warm.
let _jobRegistry = null;
let _jobRegistryPromise = null;

// Clear the per-field validation highlight as soon as the user edits a
// flagged input — the highlight should reflect the *current* state,
// not the state at last failed submit.
document.addEventListener('input', (ev) => {
    const t = ev.target;
    if (t && t.classList && t.classList.contains('jobs-form-field-invalid')) {
        t.classList.remove('jobs-form-field-invalid');
    }
}, true);

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
    // Always exit edit mode on hide — Cancel out of an edit reverts
    // the form to "create new" semantics.
    _setEditMode(null);
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
    // ONE <option> per registry entry. The ``value`` is the canonical
    // name (what gets inserted on selection); the label embeds both
    // the slash-command alias (if any) and the description so the
    // dropdown shows them on a single row, AND so the browser's
    // built-in datalist filtering matches user input against either
    // name (modern browsers match against both value and label).
    const options = entries.map(e => {
        const desc = (e.description || '').replace(/"/g, '&quot;');
        const slash = e.slash_command || '';
        // Display label: leading slash-command alias (when present),
        // then description. Looks like:
        //     morning-routine | /wb-morning · 11-phase configurable routine…
        const label = (slash ? `/${slash} · ` : '') + desc;
        return `<option value="${e.name}" label="${label.replace(/"/g, '&quot;')}"></option>`;
    });
    dl.innerHTML = options.join('');
}

// Find the canonical registry entry given the user's typed value, which
// might be either the registry name OR a slash-command alias (with or
// without the ``wb-`` prefix, and with or without a leading ``/``).
// Returns ``null`` if no match.
function _resolveInvokeEntry(entries, value) {
    if (!value) return null;
    // Strip a leading slash so ``/wb-morning`` (the form people
    // type in chat or paste from docs) resolves identically to
    // ``wb-morning``.
    const v = value.replace(/^\//, '').trim();
    if (!v) return null;
    let hit = entries.find(e => e.name === v);
    if (hit) return { entry: hit, was_slash: false };
    hit = entries.find(e => e.slash_command && e.slash_command === v);
    if (hit) return { entry: hit, was_slash: true };
    const wbPrefixed = v.startsWith('wb-') ? v : `wb-${v}`;
    hit = entries.find(e => e.slash_command && e.slash_command === wbPrefixed);
    if (hit) return { entry: hit, was_slash: true };
    return null;
}

function onInvokeNameInput() {
    if (!_jobRegistry) return;
    const kind = document.getElementById('job-form-invoke-kind').value;
    const entries = (kind === 'workflow' ? _jobRegistry.workflows : _jobRegistry.capabilities) || [];
    const inputEl = document.getElementById('job-form-invoke-name');
    const value = inputEl.value.trim();
    const resolved = _resolveInvokeEntry(entries, value);
    const hintEl = document.getElementById('job-form-invoke-hint');
    if (!resolved) {
        hintEl.textContent = '';
        renderParamsSchema(kind, null);
        onParamsInput();
        return;
    }
    if (resolved.was_slash) {
        // User typed a slash-command name; show the canonical name
        // and rewrite the input so submit goes through with the right
        // value. Don't fight the user mid-typing — only rewrite when
        // the input isn't currently focused (i.e., user pasted or the
        // datalist supplied the value and moved focus elsewhere).
        if (document.activeElement !== inputEl) {
            inputEl.value = resolved.entry.name;
        }
        hintEl.textContent =
            `${resolved.entry.description}  ·  saved as “${resolved.entry.name}”`;
    } else {
        hintEl.textContent = resolved.entry.description;
    }
    renderParamsSchema(kind, resolved.entry);
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
    // In edit mode (entered via the row's pencil button), the form's
    // primary action is "Save changes" — same backend path as create
    // but with overwrite=true so the existing file is replaced rather
    // than rejected by the no-clobber guard in create_user_job_file.
    if (_editingJobName) {
        payload.overwrite = true;
    }
    if (ui_type === 'prompt') {
        payload.job_type = 'prompt';
        payload.prompt = document.getElementById('job-form-prompt').value;
    } else {
        // ui_type === 'invoke' — backend job_type comes from the kind sub-select
        const kind = document.getElementById('job-form-invoke-kind').value;
        const rawName = document.getElementById('job-form-invoke-name').value.trim();
        // Resolve slash-command aliases (``wb-morning``) to the
        // underlying registry name (``morning-routine``) before submit.
        // The hint already showed the canonical name to the user, so
        // this is transparent rewriting, not behind-their-back.
        const entries = _jobRegistry
            ? (kind === 'workflow' ? _jobRegistry.workflows : _jobRegistry.capabilities) || []
            : [];
        const resolved = _resolveInvokeEntry(entries, rawName);
        const invokeName = resolved ? resolved.entry.name : rawName;
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
        return { success: false, error: `Network error: ${e.message}` };
    }
    const result = await resp.json().catch(() => ({}));
    if (!resp.ok || !result.success) {
        const errMsg = result.error || `Request failed (HTTP ${resp.status})`;
        errEl.textContent = errMsg;
        errEl.hidden = false;
        // Highlight the offending input(s) so the user can see at a
        // glance which field needs attention. Backend's typed
        // ``errors_by_field`` maps field-key → message; the keys
        // mirror create_user_job_file's parameter names.
        document.querySelectorAll('.jobs-form-field-invalid')
            .forEach(el => el.classList.remove('jobs-form-field-invalid'));
        const fieldErrors = result.errors_by_field || {};
        const fieldToInputId = {
            name: 'job-form-name',
            schedule: 'job-form-schedule',
            capability: 'job-form-invoke-name',
            workflow: 'job-form-invoke-name',
            prompt: 'job-form-prompt',
            params: 'job-form-params',
        };
        for (const fieldKey of Object.keys(fieldErrors)) {
            const inputId = fieldToInputId[fieldKey];
            if (!inputId) continue;
            const el = document.getElementById(inputId);
            if (el) el.classList.add('jobs-form-field-invalid');
        }
        return { success: false, error: errMsg, http_status: resp.status, raw: result };
    }
    // Clear any prior invalid-field highlights on success.
    document.querySelectorAll('.jobs-form-field-invalid')
        .forEach(el => el.classList.remove('jobs-form-field-invalid'));
    // Stash a pending banner so the user gets immediate feedback even though
    // the daemon's reload (~30s) hasn't yet picked the file up. The banner
    // clears once the change is visible in /api/state (or after 60s).
    // The dashboard endpoint publishes a user_job.created event on the bus,
    // which jobsSurface subscribes to — so this loadJobs() is paired with
    // an SSE-driven refresh later when the sidecar's cron.hot_reload fires.
    //
    // Snapshot the edit-mode flag BEFORE hideAddJobForm clears it, so we
    // route the banner to the right map (Created vs Updated).
    const wasEditing = !!_editingJobName;
    if (result.name) {
        _jobsMarkPending(
            wasEditing ? _jobsPendingEdit : _jobsPendingCreate,
            result.name,
        );
    }
    hideAddJobForm();
    // Fire-and-forget the table refresh so this function returns its
    // result immediately. The SSE-driven ``user_job.created`` handler
    // also fires ``jobsSurface.refresh()`` from the event bus — this
    // direct call is belt-and-suspenders for the case where the
    // caller (e.g. the chat-walkthrough form bridge) is awaiting our
    // return and would otherwise sit through the loadJobs round-trip.
    loadJobs();
    return { success: true, name: result.name, raw: result };
}

// ---- "Help me create a job" — chat-sidebar walkthrough ----
//
// Posts to /api/user_jobs/help, which silently creates a conversation,
// fire-and-forgets a Claude session bound to it, and returns the
// conversation_id. We open the chat sidebar tied to that conversation.
// bound_tab='jobs' keeps the sidebar visible only while the Jobs tab
// is active — switching tabs hides it but leaves the chat instance
// running (state preserved on return).
//
// Local _toast helper: window.showToast resolves to the notifications.py
// multi-arg version (title/body/tabName/viewId/view/...) which crashes
// on undefined `view`. Inline this small (msg, kind) variant to avoid
// the collision. Same pattern other tabs would use.
function _jobsHelpToast(msg, kind) {
    const container = document.getElementById('toast-container');
    if (!container) { console.log('[jobs-help]', kind, msg); return; }
    const el = document.createElement('div');
    el.className = 'toast toast-' + (kind || 'info');
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => el.remove(), 4000);
}
async function onJobsHelpClick() {
    const btn = document.getElementById('jobs-help-btn');
    if (!btn) return;
    if (window.wbChatSidebar && window.wbChatSidebar.isOpen()) {
        _jobsHelpToast('A chat is already open. Close it before starting another.', 'info');
        return;
    }
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Starting…';
    try {
        const resp = await fetch('/api/user_jobs/help', { method: 'POST' });
        const data = await resp.json();
        if (data && data.ok) {
            window.wbChatSidebar.open({
                conversation_id: data.conversation_id,
                title: data.title || 'Help me create a job',
                bound_tab: 'jobs',
            });
        } else {
            _jobsHelpToast((data && data.error) || 'Could not start the chat.', 'error');
        }
    } catch (exc) {
        _jobsHelpToast('Help request failed: ' + exc, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = orig;
    }
}
"""
