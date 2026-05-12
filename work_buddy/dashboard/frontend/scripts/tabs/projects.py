"""Dashboard Projects tab JS — registry, observations, edit form.

Owns the project list rendering, per-project detail view, observation
log loader, and the inline save / add-observation editors.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Projects ----
let _projectsCache = [];
let _selectedProjectSlug = null;
let _projectSchema = null;  // {statuses_display_order, origins, authors}

// Hardcoded fallback only used if the schema endpoint is unreachable.
// The source of truth lives in work_buddy/projects/store.py
// (STATUS_DISPLAY_ORDER). If you find yourself updating this list,
// you're patching the wrong layer.
const _PROJECT_STATUS_FALLBACK = ['active', 'paused', 'future', 'past'];

function _statusOrder() {
    if (_projectSchema && Array.isArray(_projectSchema.statuses_display_order)) {
        return _projectSchema.statuses_display_order;
    }
    return _PROJECT_STATUS_FALLBACK;
}

async function loadProjects() {
    // Fetch schema once per page session.
    if (!_projectSchema) {
        const schema = await fetchJSON('/api/projects/_schema');
        if (schema && !schema.error) _projectSchema = schema;
    }
    const data = await fetchJSON('/api/projects');
    if (!data) return;
    _projectsCache = data.projects || [];
    renderProjectList(_projectsCache);

    // First-load hash hydration: if the URL carries ``p=<id>`` but
    // no project is currently selected, resolve that integer id to a
    // slug (via _projectsCache) and select it. Using the integer id
    // (not the slug) means the URL survives slug renames.
    if (!_selectedProjectSlug
        && window._urlState
        && Object.prototype.hasOwnProperty.call(window._urlState, 'p')) {
        const pid = parseInt(window._urlState.p, 10);
        // One-shot consumption — the hash itself is the durable record.
        delete window._urlState.p;
        if (!Number.isNaN(pid)) {
            const proj = _projectsCache.find(p => p.id === pid);
            if (proj) selectProject(proj.slug);
        }
    }
}

function renderProjectList(projects) {
    const container = document.getElementById('projects-list');
    if (projects.length === 0) {
        container.innerHTML = '<div class="empty-state">No projects found</div>';
        return;
    }

    // Build groups in the order the server prescribes.
    const order = _statusOrder();
    const groups = {};
    order.forEach(s => { groups[s] = []; });

    projects.forEach(p => {
        // 'deleted' rows shouldn't reach the dashboard (the API filters
        // them by default), but guard explicitly in case include_deleted
        // ever flips on.
        if (p.status === 'deleted') return;
        const g = groups[p.status] || groups[order[0]];
        g.push(p);
    });

    let html = '';
    for (const status of order) {
        const items = groups[status];
        if (!items) continue;
        if (items.length === 0) continue;
        html += '<div style="margin-bottom:16px;">';
        html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px; padding-left:4px;">' + status + ' (' + items.length + ')</div>';
        items.forEach(p => {
            html += '<div class="proj-card" data-slug="' + p.slug + '" style="padding:10px 12px; margin-bottom:4px; border-radius:6px; cursor:pointer; border:1px solid var(--border); background:var(--bg-secondary); transition:background 0.15s;">';
            html += '<div style="display:flex; justify-content:space-between; align-items:center;">';
            html += '<strong style="font-size:14px;">' + (p.name || p.slug) + '</strong>';
            html += statusBadge(p.status);
            html += '</div>';
            if (p.description) {
                html += '<div style="font-size:12px; color:var(--text-muted); margin-top:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + p.description + '</div>';
            }
            html += '</div>';
        });
        html += '</div>';
    }
    container.innerHTML = html;

    // Attach click handlers via event delegation (avoids inline onclick quoting issues)
    container.querySelectorAll('.proj-card').forEach(card => {
        card.addEventListener('click', () => selectProject(card.dataset.slug));
    });

    // Re-apply highlight after re-render so auto-refresh doesn't visually
    // unselect the user's active project (the right-hand detail pane is
    // never re-rendered by loadProjects, only this left list is).
    if (_selectedProjectSlug) {
        const activeCard = container.querySelector(
            '.proj-card[data-slug="' + _selectedProjectSlug + '"]');
        if (activeCard) {
            activeCard.style.background = 'var(--bg-tertiary)';
            activeCard.style.borderColor = 'var(--accent)';
        }
    }
}

// ---- Folder + alias row renderers ----
// Folders and aliases each render as a row with the value (click to
// edit inline), action buttons on the right (archive toggle for
// folders, remove for both), and an existence ✓/✗ for folders.

function renderFolderRow(f) {
    const archivedPill = f.archived
        ? '<span style="font-size:10px; padding:1px 6px; border-radius:8px; background:var(--bg-secondary); color:var(--text-muted); margin-left:6px; user-select:none;">archived</span>'
        : '';
    // Existence marker. user-select:none + pointer-events:auto keeps
    // the symbol unselectable while still firing the `title` tooltip.
    const existsMark = f.exists
        ? '<span title="Path exists on disk" style="color:var(--green); font-size:13px; margin-right:6px; user-select:none; cursor:default;">✓</span>'
        : '<span title="Path does NOT exist on disk" style="color:var(--red); font-size:13px; margin-right:6px; user-select:none; cursor:default;">✗</span>';
    const archiveBtnLabel = f.archived ? 'Unarchive' : 'Archive';
    const archiveTooltip = f.archived
        ? 'Restore this folder to active — it will re-appear in the default project surfaces and count toward activity signals.'
        : 'Mark this folder as no longer active. It stays attached to the project for historical reference but is visually de-emphasized and excluded from default activity signals. Use this for dormant repos or stale notes folders.';
    const pathEsc = escapeHtml(f.path);
    const pathAttr = (f.path || '').replace(/"/g, '&quot;');
    return (
        '<div class="proj-folder-row" data-path="' + pathAttr + '" data-archived="' + (f.archived ? 1 : 0) + '" ' +
            'style="display:flex; align-items:center; gap:6px; padding:6px 8px; background:var(--bg-tertiary); border-radius:4px;">' +
            existsMark +
            '<span class="proj-folder-path" style="flex:1; font-size:12px; font-family:var(--font-mono,monospace); color:var(--text-secondary); word-break:break-all; cursor:text;" title="Click to edit the folder path">' + pathEsc + '</span>' +
            archivedPill +
            '<button class="proj-folder-archive nb-btn-tiny" title="' + escapeHtml(archiveTooltip) + '" style="font-size:11px; padding:2px 8px; background:transparent; border:1px solid var(--border); color:var(--text-muted); border-radius:3px; cursor:pointer;">' + archiveBtnLabel + '</button>' +
            '<button class="proj-folder-remove nb-btn-tiny" title="Detach this folder from the project (the folder on disk is not touched)" style="font-size:11px; padding:2px 8px; background:transparent; border:1px solid var(--border); color:var(--text-muted); border-radius:3px; cursor:pointer;">✕</button>' +
        '</div>'
    );
}

function renderAliasPill(a) {
    const aliasEsc = escapeHtml(a.alias || a.alias_norm || '?');
    const aliasAttr = (a.alias || '').replace(/"/g, '&quot;');
    const normTooltip = a.alias_norm ? ('normalized: ' + a.alias_norm) : '';
    return (
        '<span class="proj-alias-pill" data-alias="' + aliasAttr + '" title="' + escapeHtml(normTooltip) + '" ' +
            'style="display:inline-flex; align-items:center; gap:4px; font-size:12px; padding:3px 6px 3px 10px; border-radius:12px; background:var(--bg-tertiary); border:1px solid var(--border); color:var(--text-secondary);">' +
            '<span class="proj-alias-text" style="cursor:text;" title="Click to edit this alias">' + aliasEsc + '</span>' +
            '<button class="proj-alias-remove nb-btn-tiny" title="Detach this alias (the canonical project is not deleted)" style="font-size:11px; padding:0 4px; background:transparent; border:none; color:var(--text-muted); cursor:pointer; line-height:1;">✕</button>' +
        '</span>'
    );
}

// Wire up event handlers on folder + alias rows (delegated). Called
// after the detail pane is rendered AND after any in-place patch.
function wireFolderEvents(slug) {
    const list = document.getElementById('proj-folders-list');
    if (!list) return;
    list.querySelectorAll('.proj-folder-row').forEach(row => {
        const path = row.dataset.path;
        const archived = row.dataset.archived === '1';
        const removeBtn = row.querySelector('.proj-folder-remove');
        const archBtn = row.querySelector('.proj-folder-archive');
        const pathEl = row.querySelector('.proj-folder-path');
        removeBtn.addEventListener('click', () => removeFolder(slug, path));
        archBtn.addEventListener('click', () => setFolderArchived(slug, path, !archived));
        pathEl.addEventListener('click', () => beginEditFolder(slug, row, path));
    });
    const addBtn = document.getElementById('proj-folder-add-btn');
    if (addBtn && !addBtn._wired) {
        addBtn._wired = true;
        addBtn.addEventListener('click', () => addFolderFromForm(slug));
    }
}

function wireAliasEvents(slug) {
    const list = document.getElementById('proj-aliases-list');
    if (!list) return;
    list.querySelectorAll('.proj-alias-pill').forEach(pill => {
        const alias = pill.dataset.alias;
        const removeBtn = pill.querySelector('.proj-alias-remove');
        const textEl = pill.querySelector('.proj-alias-text');
        removeBtn.addEventListener('click', () => removeAlias(slug, alias));
        textEl.addEventListener('click', () => beginEditAlias(slug, pill, alias));
    });
    const addBtn = document.getElementById('proj-alias-add-btn');
    if (addBtn && !addBtn._wired) {
        addBtn._wired = true;
        addBtn.addEventListener('click', () => addAliasFromForm(slug));
    }
}

async function refreshFoldersOnly(slug) {
    const data = await fetchJSON('/api/projects/' + slug);
    if (_selectedProjectSlug !== slug) return;
    if (!data || data.error) return;
    const folders = Array.isArray(data.folders) ? data.folders : [];
    const list = document.getElementById('proj-folders-list');
    if (!list) return;
    if (folders.length === 0) {
        list.innerHTML = '<div style="color:var(--text-muted); font-size:12px; font-style:italic;">No folders attached.</div>';
    } else {
        list.innerHTML = folders.map(renderFolderRow).join('');
    }
    // Update count label
    const label = list.parentElement.querySelector('label');
    if (label) label.textContent = 'Folders (' + folders.length + ')';
    wireFolderEvents(slug);
}

async function refreshAliasesOnly(slug) {
    const data = await fetchJSON('/api/projects/' + slug);
    if (_selectedProjectSlug !== slug) return;
    if (!data || data.error) return;
    const aliases = Array.isArray(data.aliases) ? data.aliases : [];
    const list = document.getElementById('proj-aliases-list');
    if (!list) return;
    if (aliases.length === 0) {
        list.innerHTML = '<div style="color:var(--text-muted); font-size:12px; font-style:italic;">No aliases.</div>';
    } else {
        list.innerHTML = aliases.map(renderAliasPill).join('');
    }
    const label = list.parentElement.querySelector('label');
    if (label) label.textContent = 'Aliases (' + aliases.length + ')';
    wireAliasEvents(slug);
}

function _setStatus(elId, text, color) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.textContent = text;
    el.style.color = color || 'var(--text-muted)';
    if (text) setTimeout(() => { if (el.textContent === text) el.textContent = ''; }, 3000);
}

async function addFolderFromForm(slug) {
    const input = document.getElementById('proj-folder-add-path');
    const path = (input.value || '').trim();
    if (!path) return;
    _setStatus('proj-folder-status', 'Adding…');
    const resp = await fetch('/api/projects/' + slug + '/folders', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path, archived: false}),
    });
    const data = await resp.json();
    if (data.error) {
        _setStatus('proj-folder-status', 'Error: ' + data.error, 'var(--red)');
    } else {
        input.value = '';
        _setStatus('proj-folder-status', 'Added.', 'var(--green)');
        refreshFoldersOnly(slug);
    }
}

async function removeFolder(slug, path) {
    _setStatus('proj-folder-status', 'Removing…');
    const resp = await fetch('/api/projects/' + slug + '/folders', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path}),
    });
    const data = await resp.json();
    if (data.error) {
        _setStatus('proj-folder-status', 'Error: ' + data.error, 'var(--red)');
    } else {
        _setStatus('proj-folder-status', 'Removed.', 'var(--green)');
        refreshFoldersOnly(slug);
    }
}

async function setFolderArchived(slug, path, archived) {
    _setStatus('proj-folder-status', archived ? 'Archiving…' : 'Unarchiving…');
    const resp = await fetch('/api/projects/' + slug + '/folders/archived', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path, archived}),
    });
    const data = await resp.json();
    if (data.error) {
        _setStatus('proj-folder-status', 'Error: ' + data.error, 'var(--red)');
    } else {
        _setStatus('proj-folder-status', archived ? 'Archived.' : 'Unarchived.', 'var(--green)');
        refreshFoldersOnly(slug);
    }
}

function beginEditFolder(slug, row, oldPath) {
    const pathEl = row.querySelector('.proj-folder-path');
    if (!pathEl || pathEl.tagName === 'INPUT') return;  // already editing
    const input = document.createElement('input');
    input.type = 'text';
    input.value = oldPath;
    input.style.cssText = pathEl.style.cssText + '; padding:2px 6px; background:var(--bg-primary); border:1px solid var(--accent); border-radius:3px; color:var(--text-primary);';
    pathEl.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const commit = async () => {
        if (done) return;
        done = true;
        const newPath = input.value.trim();
        if (!newPath || newPath === oldPath) {
            refreshFoldersOnly(slug);
            return;
        }
        _setStatus('proj-folder-status', 'Renaming…');
        const resp = await fetch('/api/projects/' + slug + '/folders', {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({old_path: oldPath, new_path: newPath}),
        });
        const data = await resp.json();
        if (data.error) {
            _setStatus('proj-folder-status', 'Error: ' + data.error, 'var(--red)');
        } else {
            _setStatus('proj-folder-status', 'Renamed.', 'var(--green)');
        }
        refreshFoldersOnly(slug);
    };
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { done = true; refreshFoldersOnly(slug); }
    });
}

async function addAliasFromForm(slug) {
    const input = document.getElementById('proj-alias-add');
    const alias = (input.value || '').trim();
    if (!alias) return;
    _setStatus('proj-alias-status', 'Adding…');
    const resp = await fetch('/api/projects/' + slug + '/aliases', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({alias}),
    });
    const data = await resp.json();
    if (data.error) {
        _setStatus('proj-alias-status', 'Error: ' + data.error, 'var(--red)');
    } else {
        input.value = '';
        _setStatus('proj-alias-status', 'Added.', 'var(--green)');
        refreshAliasesOnly(slug);
    }
}

async function removeAlias(slug, alias) {
    _setStatus('proj-alias-status', 'Removing…');
    const resp = await fetch('/api/projects/' + slug + '/aliases', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({alias}),
    });
    const data = await resp.json();
    if (data.error) {
        _setStatus('proj-alias-status', 'Error: ' + data.error, 'var(--red)');
    } else {
        _setStatus('proj-alias-status', 'Removed.', 'var(--green)');
        refreshAliasesOnly(slug);
    }
}

function beginEditAlias(slug, pill, oldAlias) {
    const textEl = pill.querySelector('.proj-alias-text');
    if (!textEl || textEl.tagName === 'INPUT') return;
    const input = document.createElement('input');
    input.type = 'text';
    input.value = oldAlias;
    input.style.cssText = 'font-size:12px; padding:1px 6px; background:var(--bg-primary); border:1px solid var(--accent); border-radius:3px; color:var(--text-primary); width:' + Math.max(80, oldAlias.length * 8) + 'px;';
    textEl.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const commit = async () => {
        if (done) return;
        done = true;
        const newAlias = input.value.trim();
        if (!newAlias || newAlias === oldAlias) {
            refreshAliasesOnly(slug);
            return;
        }
        _setStatus('proj-alias-status', 'Renaming…');
        const resp = await fetch('/api/projects/' + slug + '/aliases', {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({old_alias: oldAlias, new_alias: newAlias}),
        });
        const data = await resp.json();
        if (data.error) {
            _setStatus('proj-alias-status', 'Error: ' + data.error, 'var(--red)');
        } else {
            _setStatus('proj-alias-status', 'Renamed.', 'var(--green)');
        }
        refreshAliasesOnly(slug);
    };
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { done = true; refreshAliasesOnly(slug); }
    });
}

async function selectProject(slug) {
    _selectedProjectSlug = slug;
    // Reflect the selection in the URL hash so reloads and copy-paste
    // links restore it. _persistHash reads _selectedProjectSlug +
    // _projectsCache to write p=<id>.
    if (typeof _persistHash === 'function') _persistHash();
    // Highlight selected card
    document.querySelectorAll('.proj-card').forEach(c => {
        c.style.background = c.dataset.slug === slug ? 'var(--bg-tertiary)' : 'var(--bg-secondary)';
        c.style.borderColor = c.dataset.slug === slug ? 'var(--accent)' : 'var(--border)';
    });

    const detail = document.getElementById('project-detail');
    detail.innerHTML = '<div class="loading">Loading project details...</div>';

    const data = await fetchJSON('/api/projects/' + slug);
    // Race guard: the user may have clicked a different project while
    // this fetch was in flight. If so, drop the stale response so it
    // doesn't overwrite the currently-rendered detail pane.
    if (_selectedProjectSlug !== slug) return;
    if (!data || data.error) {
        detail.innerHTML = '<div class="empty-state">' + (data?.error || 'Failed to load') + '</div>';
        return;
    }

    const statusOptions = _statusOrder().map(s =>
        '<option value="' + s + '"' + (s === data.status ? ' selected' : '') + '>' + s + '</option>'
    ).join('');

    const folders = Array.isArray(data.folders) ? data.folders : [];
    const aliases = Array.isArray(data.aliases) ? data.aliases : [];

    let html = '<div style="max-width:760px;">';

    // Header: H2 name + status badge
    html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">';
    html += '<h2 style="margin:0; font-size:20px;">' + escapeHtml(data.name || data.slug) + '</h2>';
    html += statusBadge(data.status);
    html += '</div>';

    // Metadata strip (slug · origin · created · updated)
    html += '<div style="font-size:11px; color:var(--text-muted); margin-bottom:18px; display:flex; gap:14px; flex-wrap:wrap;">';
    html += '<span><code style="font-size:11px;">' + escapeHtml(data.slug) + '</code></span>';
    if (data.origin) {
        html += '<span>origin: <strong style="color:var(--text-secondary);">' + escapeHtml(data.origin) + '</strong></span>';
    }
    html += '<span>created ' + (data.created_at || '—').slice(0, 10) + '</span>';
    html += '<span>updated ' + (data.updated_at || '—').slice(0, 10) + '</span>';
    html += '</div>';

    // Name + Status, side-by-side
    html += '<div style="display:flex; gap:12px; margin-bottom:16px; align-items:flex-end;">';
    html += '<div style="flex:2; min-width:0;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Name</label>';
    html += '<input id="proj-name" type="text" value="' + (data.name || '').replace(/"/g, '&quot;') + '" style="width:100%; padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px; box-sizing:border-box;" />';
    html += '</div>';
    html += '<div style="flex:0 0 auto;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Status</label>';
    html += '<select id="proj-status" style="padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px;">' + statusOptions + '</select>';
    html += '</div>';
    html += '</div>';

    // Description (taller default; still drag-resizable)
    html += '<div style="margin-bottom:16px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Description</label>';
    html += '<textarea id="proj-desc" rows="10" style="width:100%; min-height:180px; padding:10px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px; line-height:1.5; resize:vertical; box-sizing:border-box;">' + escapeHtml(data.description || '') + '</textarea>';
    html += '</div>';

    // Folders section (editable). Each row: existence ✓/✗ + path
    // (click to edit) + archive toggle + remove. Add form at bottom.
    html += '<div style="margin-bottom:16px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:6px;">Folders (' + folders.length + ')</label>';
    html += '<div id="proj-folders-list" style="display:flex; flex-direction:column; gap:4px;">';
    if (folders.length === 0) {
        html += '<div style="color:var(--text-muted); font-size:12px; font-style:italic;">No folders attached.</div>';
    } else {
        folders.forEach(f => {
            html += renderFolderRow(f);
        });
    }
    html += '</div>';
    // Add-folder form
    html += '<div style="margin-top:8px; display:flex; gap:6px;">';
    html += '<input id="proj-folder-add-path" type="text" placeholder="Absolute path to new folder..." style="flex:1; padding:6px 10px; font-size:12px; font-family:var(--font-mono,monospace); background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); box-sizing:border-box;" />';
    html += '<button id="proj-folder-add-btn" class="nb-btn nb-btn-neutral" style="font-size:12px; padding:6px 12px;">Add folder</button>';
    html += '</div>';
    html += '<div id="proj-folder-status" style="font-size:11px; color:var(--text-muted); margin-top:4px; min-height:14px;"></div>';
    html += '</div>';

    // Aliases section (editable). Pill row + add form.
    html += '<div style="margin-bottom:24px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:6px;">Aliases (' + aliases.length + ')</label>';
    html += '<div id="proj-aliases-list" style="display:flex; flex-wrap:wrap; gap:6px;">';
    if (aliases.length === 0) {
        html += '<div style="color:var(--text-muted); font-size:12px; font-style:italic;">No aliases.</div>';
    } else {
        aliases.forEach(a => {
            html += renderAliasPill(a);
        });
    }
    html += '</div>';
    // Add-alias form
    html += '<div style="margin-top:8px; display:flex; gap:6px;">';
    html += '<input id="proj-alias-add" type="text" placeholder="Alternative slug (e.g. prior name)..." style="flex:1; max-width:280px; padding:6px 10px; font-size:12px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); box-sizing:border-box;" />';
    html += '<button id="proj-alias-add-btn" class="nb-btn nb-btn-neutral" style="font-size:12px; padding:6px 12px;">Add alias</button>';
    html += '</div>';
    html += '<div id="proj-alias-status" style="font-size:11px; color:var(--text-muted); margin-top:4px; min-height:14px;"></div>';
    html += '</div>';

    // Save button
    html += '<div style="margin-bottom:24px;">';
    html += '<button id="proj-save-btn" class="nb-btn nb-btn-approve" style="margin-right:8px;">Save Changes</button>';
    html += '<span id="proj-save-status" style="font-size:12px; color:var(--text-muted);"></span>';
    html += '</div>';

    // Add observation
    html += '<div style="border-top:1px solid var(--border); padding-top:16px; margin-top:16px;">';
    html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">Add Observation</div>';
    html += '<textarea id="proj-obs" rows="3" placeholder="Record a decision, pivot, blocker, or insight about this project..." style="width:100%; padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px; resize:vertical; box-sizing:border-box;"></textarea>';
    html += '<button id="proj-obs-btn" class="nb-btn nb-btn-neutral" style="margin-top:8px;">Retain Observation</button>';
    html += '<span id="proj-obs-status" style="font-size:12px; color:var(--text-muted); margin-left:8px;"></span>';
    html += '</div>';

    // Observations log (loaded async — does not block detail-pane render)
    html += '<div style="margin-top:20px;">';
    html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">Observations</div>';
    html += '<div id="proj-observations-log"><div class="loading">Loading observations...</div></div>';
    html += '</div>';

    html += '</div>';
    detail.innerHTML = html;

    // Attach handlers (avoids inline onclick quoting issues in Python string templates)
    document.getElementById('proj-save-btn').addEventListener('click', () => saveProject(slug));
    document.getElementById('proj-obs-btn').addEventListener('click', () => addObservation(slug));

    // Folder + alias editing handlers (per-row + add-form)
    wireFolderEvents(slug);
    wireAliasEvents(slug);

    // Enter-to-submit on the add forms
    const folderAddInput = document.getElementById('proj-folder-add-path');
    if (folderAddInput) {
        folderAddInput.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); addFolderFromForm(slug); }
        });
    }
    const aliasAddInput = document.getElementById('proj-alias-add');
    if (aliasAddInput) {
        aliasAddInput.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); addAliasFromForm(slug); }
        });
    }

    // Load observations log asynchronously
    loadProjectObservations(slug);
}

async function loadProjectObservations(slug) {
    const data = await fetchJSON('/api/projects/' + slug + '/memories?limit=30');
    // Race guard: if the user navigated away, the proj-observations-log
    // container might belong to a different project's detail pane.
    if (_selectedProjectSlug !== slug) return;
    const container = document.getElementById('proj-observations-log');
    if (!container) return;
    if (!data || !data.memories) {
        container.innerHTML = '<div class="empty-state">Could not load observations</div>';
        return;
    }

    const memories = data.memories;
    if (memories.length === 0) {
        container.innerHTML = '<div class="empty-state" style="padding:12px 0;">No observations yet</div>';
        return;
    }

    const logHtml = memories.map(m => {
        const dt = m.date ? new Date(m.date) : null;
        const time = dt ? dt.toLocaleDateString([], {month:'short', day:'numeric'}) + ' ' + dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
        const ft = m.fact_type || 'memory';
        const ftClass = ft === 'observation' ? 'warn' : ft === 'world' ? 'info' : 'info';
        const source = (m.tags || []).filter(t => t.startsWith('source:')).map(t => t.slice(7)).join(', ') || '';
        return '<div class="log-entry ' + ftClass + '">' +
            '<span class="log-ts">' + time + '</span>' +
            '<span class="log-kind">' + ft + (source ? ' (' + source + ')' : '') + '</span>' +
            '<span class="log-msg">' + escapeHtml(m.text) + '</span>' +
        '</div>';
    }).join('');

    container.innerHTML = '<div class="log-container">' + logHtml + '</div>';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function saveProject(slug) {
    const name = document.getElementById('proj-name').value.trim();
    const status = document.getElementById('proj-status').value;
    const description = document.getElementById('proj-desc').value.trim();
    const statusEl = document.getElementById('proj-save-status');

    statusEl.textContent = 'Saving...';
    statusEl.style.color = 'var(--text-muted)';

    try {
        const resp = await fetch('/api/projects/' + slug, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, status, description}),
        });
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = 'Error: ' + data.error;
            statusEl.style.color = 'var(--red)';
        } else {
            statusEl.textContent = 'Saved!';
            statusEl.style.color = 'var(--green)';
            setTimeout(() => statusEl.textContent = '', 2000);
            // Refresh the list to show updated name/status
            loadProjects();
        }
    } catch (e) {
        statusEl.textContent = 'Failed to save';
        statusEl.style.color = 'var(--red)';
    }
}

async function addObservation(slug) {
    const textarea = document.getElementById('proj-obs');
    const content = textarea.value.trim();
    if (!content) return;

    const statusEl = document.getElementById('proj-obs-status');
    statusEl.textContent = 'Retaining...';
    statusEl.style.color = 'var(--text-muted)';

    try {
        const resp = await fetch('/api/projects/' + slug + '/observe', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content}),
        });
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = 'Error: ' + data.error;
            statusEl.style.color = 'var(--red)';
        } else {
            statusEl.textContent = 'Retained!';
            statusEl.style.color = 'var(--green)';
            textarea.value = '';
            setTimeout(() => statusEl.textContent = '', 2000);
            // Refresh detail to show new memory
            selectProject(slug);
        }
    } catch (e) {
        statusEl.textContent = 'Failed to retain';
        statusEl.style.color = 'var(--red)';
    }
}

// ---- Surface handle for the SSE event bus ----
// Other processes (sync runs, MCP-driven mutations, agent edits) publish
// ``project.*`` events; ``core/event_bus.py`` debounces them and calls
// ``window.projectsSurface.refresh()`` when this tab is mounted.
window.projectsSurface = {
    refresh: function() {
        if (typeof loadProjects === 'function') return loadProjects();
    },
    isMounted: function() {
        return !!document.getElementById('projects-list');
    },
};
"""
