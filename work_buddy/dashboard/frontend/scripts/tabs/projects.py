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

async function loadProjects() {
    const data = await fetchJSON('/api/projects');
    if (!data) return;
    _projectsCache = data.projects || [];
    renderProjectList(_projectsCache);
}

function renderProjectList(projects) {
    const container = document.getElementById('projects-list');
    if (projects.length === 0) {
        container.innerHTML = '<div class="empty-state">No projects found</div>';
        return;
    }

    const groups = {active: [], inferred: [], paused: [], future: [], past: []};
    projects.forEach(p => {
        const g = groups[p.status] || groups['active'];
        g.push(p);
    });

    let html = '';
    for (const [status, items] of Object.entries(groups)) {
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

async function selectProject(slug) {
    _selectedProjectSlug = slug;
    // Highlight selected card
    document.querySelectorAll('.proj-card').forEach(c => {
        c.style.background = c.dataset.slug === slug ? 'var(--bg-tertiary)' : 'var(--bg-secondary)';
        c.style.borderColor = c.dataset.slug === slug ? 'var(--accent)' : 'var(--border)';
    });

    const detail = document.getElementById('project-detail');
    detail.innerHTML = '<div class="loading">Loading project details...</div>';

    const data = await fetchJSON('/api/projects/' + slug);
    if (!data || data.error) {
        detail.innerHTML = '<div class="empty-state">' + (data?.error || 'Failed to load') + '</div>';
        return;
    }

    const statusOptions = ['active', 'paused', 'past', 'future', 'inferred'].map(s =>
        '<option value="' + s + '"' + (s === data.status ? ' selected' : '') + '>' + s + '</option>'
    ).join('');

    let html = '<div style="max-width:700px;">';

    // Header
    html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">';
    html += '<h2 style="margin:0; font-size:20px;">' + (data.name || data.slug) + '</h2>';
    html += statusBadge(data.status);
    html += '</div>';

    // Editable fields
    html += '<div style="margin-bottom:16px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Name</label>';
    html += '<input id="proj-name" type="text" value="' + (data.name || '').replace(/"/g, '&quot;') + '" style="width:100%; padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px;" />';
    html += '</div>';

    html += '<div style="margin-bottom:16px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Status</label>';
    html += '<select id="proj-status" style="padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px;">' + statusOptions + '</select>';
    html += '</div>';

    html += '<div style="margin-bottom:16px;">';
    html += '<label style="font-size:11px; text-transform:uppercase; color:var(--text-muted); display:block; margin-bottom:4px;">Description</label>';
    html += '<textarea id="proj-desc" rows="3" style="width:100%; padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px; resize:vertical;">' + (data.description || '') + '</textarea>';
    html += '</div>';

    html += '<div style="margin-bottom:24px;">';
    html += '<button id="proj-save-btn" class="nb-btn nb-btn-approve" style="margin-right:8px;">Save Changes</button>';
    html += '<span id="proj-save-status" style="font-size:12px; color:var(--text-muted);"></span>';
    html += '</div>';

    // Memory section
    html += '<div style="border-top:1px solid var(--border); padding-top:16px; margin-top:16px;">';
    html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">Project Memory (Hindsight)</div>';
    if (data.memory) {
        html += '<div style="font-size:13px; line-height:1.6; color:var(--text-secondary); white-space:pre-wrap; max-height:300px; overflow-y:auto; background:var(--bg-tertiary); padding:12px; border-radius:6px;">' + escapeHtml(String(data.memory)) + '</div>';
    } else {
        html += '<div style="color:var(--text-muted); font-size:13px;">No project memories yet. Add observations below or use project_observe via MCP.</div>';
    }
    html += '</div>';

    // Add observation
    html += '<div style="margin-top:16px;">';
    html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">Add Observation</div>';
    html += '<textarea id="proj-obs" rows="3" placeholder="Record a decision, pivot, blocker, or insight about this project..." style="width:100%; padding:8px; background:var(--bg-tertiary); border:1px solid var(--border); border-radius:4px; color:var(--text-primary); font-size:14px; resize:vertical;"></textarea>';
    html += '<button id="proj-obs-btn" class="nb-btn nb-btn-neutral" style="margin-top:8px;">Retain Observation</button>';
    html += '<span id="proj-obs-status" style="font-size:12px; color:var(--text-muted); margin-left:8px;"></span>';
    html += '</div>';

    // Observations log (loaded async)
    html += '<div style="border-top:1px solid var(--border); padding-top:16px; margin-top:24px;">';
    html += '<div style="font-size:11px; text-transform:uppercase; color:var(--text-muted); margin-bottom:8px;">Observations</div>';
    html += '<div id="proj-observations-log"><div class="loading">Loading observations...</div></div>';
    html += '</div>';

    // Metadata
    html += '<div style="margin-top:24px; padding-top:16px; border-top:1px solid var(--border); font-size:11px; color:var(--text-muted);">';
    html += 'Created: ' + (data.created_at || '—').slice(0, 10) + ' &middot; Updated: ' + (data.updated_at || '—').slice(0, 10) + ' &middot; Slug: <code>' + data.slug + '</code>';
    html += '</div>';

    html += '</div>';
    detail.innerHTML = html;

    // Attach handlers (avoids inline onclick quoting issues in Python string templates)
    document.getElementById('proj-save-btn').addEventListener('click', () => saveProject(slug));
    document.getElementById('proj-obs-btn').addEventListener('click', () => addObservation(slug));

    // Load observations log asynchronously
    loadProjectObservations(slug);
}

async function loadProjectObservations(slug) {
    const container = document.getElementById('proj-observations-log');
    if (!container) return;

    const data = await fetchJSON('/api/projects/' + slug + '/memories?limit=30');
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
"""
