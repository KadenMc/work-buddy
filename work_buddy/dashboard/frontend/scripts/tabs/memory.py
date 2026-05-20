"""Dashboard Memory tab JS — entity browse, create, edit.

The Memory tab hosts sub-views; v1 ships one: Entities. The sub-tab
bar is rendered with a single button (Entities) and the ``mst=`` URL
hash key, mirroring Settings' ``st=`` pattern. The bar is structural,
not cosmetic — future sub-views (Contracts, Projects rollup) will mount
under the same bar without an IA migration.

Entities view layout mirrors the Projects view: a sticky left list
of entities (optionally tag-filtered) and a detail panel on the right
with editable canonical name, description, tags, aliases, plus a
read-only recent-references log.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Memory tab: entity registry ----

let _entitiesCache = [];
let _selectedEntityId = null;
let _entityTagFilter = '';
let WB_MEMORY_SUBTAB = 'entities';

function switchMemorySubtab(mst) {
    if (mst !== 'entities') mst = 'entities';  // v1: only Entities exists
    WB_MEMORY_SUBTAB = mst;
    document.querySelectorAll('.memory-subtab-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.mst === mst));
    document.querySelectorAll('.memory-subtab-panel').forEach(p =>
        p.classList.toggle('active', p.id === 'msp-' + mst));
    if (typeof _persistHash === 'function') _persistHash();
}

async function loadMemory() {
    // Single sub-view in v1 — defer entirely to the Entities loader.
    return loadEntities();
}

async function loadEntities() {
    const params = new URLSearchParams();
    if (_entityTagFilter) params.set('tag', _entityTagFilter);
    const url = '/api/entities' + (params.toString() ? '?' + params : '');
    const data = await fetchJSON(url);
    if (!data) return;
    _entitiesCache = data.entities || [];
    renderEntityList(_entitiesCache);

    // Hash hydration: restore selection if ``e=<id>`` is set.
    if (!_selectedEntityId
        && window._urlState
        && Object.prototype.hasOwnProperty.call(window._urlState, 'e')) {
        const eid = parseInt(window._urlState.e, 10);
        delete window._urlState.e;
        if (!Number.isNaN(eid)) {
            const found = _entitiesCache.find(x => x.id === eid);
            if (found) selectEntity(eid);
        }
    }
}

function renderEntityList(entities) {
    const container = document.getElementById('entities-list');
    if (!container) return;
    if (entities.length === 0) {
        container.innerHTML = renderEntityListHeader() +
            '<div class="empty-state">No entities yet. Click <strong>+ New entity</strong> above to create one.</div>';
        wireEntityListHeader();
        return;
    }

    // Group by top-level tag (the first slash-segment of the first
    // tag), so a list with many people groups under "person" with a
    // sub-heading.
    const groups = {};
    entities.forEach(e => {
        const top = _topTag(e) || '(untagged)';
        if (!groups[top]) groups[top] = [];
        groups[top].push(e);
    });
    const groupKeys = Object.keys(groups).sort((a, b) => {
        // Untagged group goes last.
        if (a === '(untagged)') return 1;
        if (b === '(untagged)') return -1;
        return a.localeCompare(b);
    });

    let html = renderEntityListHeader();
    for (const key of groupKeys) {
        html += '<div class="entity-group-header">' + escapeHtml(key) +
                ' <span class="entity-group-count">' +
                groups[key].length + '</span></div>';
        for (const e of groups[key]) {
            const isSelected = (e.id === _selectedEntityId);
            const aliasCount = (e.aliases || []).length;
            const tagPreview = (e.tags || []).slice(0, 3)
                .map(t => '<span class="entity-tag-chip">' + escapeHtml(t.tag) + '</span>')
                .join(' ');
            html +=
                '<div class="entity-list-row' + (isSelected ? ' selected' : '') + '"' +
                ' onclick="selectEntity(' + e.id + ')">' +
                '  <div class="entity-list-name">' + escapeHtml(e.canonical_name) + '</div>' +
                (aliasCount > 0
                    ? '<div class="entity-list-aliases">aka ' + aliasCount + ' alias' + (aliasCount === 1 ? '' : 'es') + '</div>'
                    : '') +
                (tagPreview ? '<div class="entity-list-tags">' + tagPreview + '</div>' : '') +
                '</div>';
        }
    }
    container.innerHTML = html;
    wireEntityListHeader();
}

function renderEntityListHeader() {
    return '<div class="entity-list-header">' +
        '<button class="entity-new-btn" onclick="openEntityCreateForm()">+ New entity</button>' +
        '<input type="text" id="entity-tag-filter" class="entity-tag-filter-input" placeholder="Filter by tag (e.g. person)" value="' +
        escapeHtml(_entityTagFilter) + '" />' +
        '</div>';
}

function wireEntityListHeader() {
    const input = document.getElementById('entity-tag-filter');
    if (!input) return;
    input.addEventListener('change', () => {
        _entityTagFilter = input.value.trim();
        loadEntities();
    });
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            _entityTagFilter = input.value.trim();
            loadEntities();
        }
    });
}

function _topTag(entity) {
    const tags = entity.tags || [];
    if (tags.length === 0) return null;
    const first = tags[0].tag_norm || tags[0].tag || '';
    return first.split('/')[0];
}

async function selectEntity(eid) {
    _selectedEntityId = eid;
    if (typeof _persistHash === 'function') _persistHash();
    document.querySelectorAll('.entity-list-row').forEach(row => {
        row.classList.toggle('selected',
            row.getAttribute('onclick') === 'selectEntity(' + eid + ')');
    });
    await renderEntityDetail(eid);
}

async function renderEntityDetail(eid) {
    const detail = document.getElementById('entity-detail');
    if (!detail) return;
    detail.innerHTML = '<div class="loading">Loading entity...</div>';
    const data = await fetchJSON('/api/entities/' + eid);
    if (!data || data.error) {
        detail.innerHTML = '<div class="empty-state">' +
            escapeHtml(data && data.error ? data.error : 'Failed to load') +
            '</div>';
        return;
    }
    detail.innerHTML = _entityDetailHTML(data);
    wireEntityDetailEvents(data);
}

function _entityDetailHTML(e) {
    const tags = e.tags || [];
    const aliases = e.aliases || [];
    const recent = e.recent_references || [];
    const refCount = e.reference_count || 0;
    return (
        '<div class="entity-detail-card">' +
        '  <div class="entity-detail-header">' +
        '    <input id="entity-name-input" class="entity-name-input"' +
        '           value="' + escapeHtml(e.canonical_name) + '"' +
        '           data-original="' + escapeHtml(e.canonical_name) + '" />' +
        '    <button class="entity-delete-btn" onclick="deleteEntity(' + e.id + ')">Delete</button>' +
        '  </div>' +
        '  <div class="entity-detail-row">' +
        '    <label>Description</label>' +
        '    <textarea id="entity-description-input" class="entity-description-input"' +
        '              data-original="' + escapeHtml(e.description || '') + '"' +
        '              placeholder="What is this? Relationship context lives here.">' +
        escapeHtml(e.description || '') +
        '    </textarea>' +
        '    <div class="entity-save-row">' +
        '      <button class="entity-save-btn" onclick="saveEntityIdentity(' + e.id + ')">Save</button>' +
        '      <span id="entity-save-status" class="entity-save-status"></span>' +
        '    </div>' +
        '  </div>' +
        '  <div class="entity-detail-row">' +
        '    <label>Tags (' + tags.length + ')</label>' +
        '    <div id="entity-tags-list" class="entity-tags-list">' +
        tags.map(t => _renderTagChip(e.id, t)).join('') +
        '    </div>' +
        '    <div class="entity-tag-add-row">' +
        '      <input id="entity-tag-add" class="entity-tag-add-input" placeholder="Add tag (e.g. person/family)" />' +
        '      <button class="entity-add-chip-btn" onclick="addEntityTag(' + e.id + ')">Add</button>' +
        '    </div>' +
        '    <div id="entity-tag-status" class="entity-save-status"></div>' +
        '  </div>' +
        '  <div class="entity-detail-row">' +
        '    <label>Aliases (' + aliases.length + ')</label>' +
        '    <div id="entity-aliases-list" class="entity-aliases-list">' +
        aliases.map(a => _renderAliasChip(e.id, a)).join('') +
        '    </div>' +
        '    <div class="entity-alias-add-row">' +
        '      <input id="entity-alias-add" class="entity-alias-add-input" placeholder="Add alias" />' +
        '      <button class="entity-add-chip-btn" onclick="addEntityAlias(' + e.id + ')">Add</button>' +
        '    </div>' +
        '    <div id="entity-alias-status" class="entity-save-status"></div>' +
        '  </div>' +
        '  <div class="entity-detail-row">' +
        '    <label>Recent references (' + recent.length + ' of ' + refCount + ')</label>' +
        '    <div class="entity-refs-list">' +
        (recent.length === 0
            ? '<div class="entity-refs-empty">No references recorded yet.</div>'
            : recent.map(_renderRefRow).join('')) +
        '    </div>' +
        '  </div>' +
        '</div>'
    );
}

// Tag + alias chips embed the target value into an inline onclick.
// The value is encodeURIComponent-encoded on the way in and
// decodeURIComponent-decoded in the handler — encodeURIComponent
// output contains no quotes or backslashes, so it is always safe
// inside a single-quoted JS string literal regardless of what
// punctuation the tag or alias contains (e.g. an alias like O'Brien).
function _renderTagChip(eid, tag) {
    return '<span class="entity-tag-chip-edit">' +
        escapeHtml(tag.tag) +
        '<button class="entity-chip-x" title="Remove tag"' +
        ' onclick="removeEntityTag(' + eid + ', \'' +
        encodeURIComponent(tag.tag_norm) + '\')">×</button>' +
        '</span>';
}

function _renderAliasChip(eid, alias) {
    return '<span class="entity-alias-chip">' +
        escapeHtml(alias.alias) +
        '<button class="entity-chip-x" title="Remove alias"' +
        ' onclick="removeEntityAlias(' + eid + ', \'' +
        encodeURIComponent(alias.alias) + '\')">×</button>' +
        '</span>';
}

function _renderRefRow(ref) {
    const kindClass = 'entity-ref-kind-' + (ref.source_kind || 'other');
    return '<div class="entity-ref-row">' +
        '<span class="entity-ref-kind ' + kindClass + '">' + escapeHtml(ref.source_kind || '') + '</span>' +
        '<span class="entity-ref-path">' + escapeHtml(ref.source_path || '') + '</span>' +
        '<span class="entity-ref-time">' + escapeHtml((ref.occurred_at || '').slice(0, 19).replace('T', ' ')) + '</span>' +
        (ref.snippet ? '<div class="entity-ref-snippet">' + escapeHtml(ref.snippet) + '</div>' : '') +
        '</div>';
}

function wireEntityDetailEvents(_e) {
    // Enable Save on dirty changes — purely visual hint; the user can
    // hit Save unconditionally.
    const name = document.getElementById('entity-name-input');
    const desc = document.getElementById('entity-description-input');
    const saveBtn = document.querySelector('.entity-save-btn');
    if (!name || !desc || !saveBtn) return;
    function syncDirty() {
        const dirty = name.value !== name.dataset.original
            || desc.value !== desc.dataset.original;
        saveBtn.classList.toggle('dirty', dirty);
    }
    name.addEventListener('input', syncDirty);
    desc.addEventListener('input', syncDirty);

    // Enter in the tag/alias add inputs commits.
    const tagAdd = document.getElementById('entity-tag-add');
    if (tagAdd) tagAdd.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') {
            ev.preventDefault();
            addEntityTag(_e.id);
        }
    });
    const aliasAdd = document.getElementById('entity-alias-add');
    if (aliasAdd) aliasAdd.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') {
            ev.preventDefault();
            addEntityAlias(_e.id);
        }
    });
}

// ---- Mutations ----

function _entitySetStatus(elId, text, color) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.textContent = text;
    el.style.color = color || 'var(--text-muted)';
    if (text) setTimeout(() => {
        if (el.textContent === text) el.textContent = '';
    }, 3000);
}

async function saveEntityIdentity(eid) {
    const name = document.getElementById('entity-name-input').value.trim();
    const description = document.getElementById('entity-description-input').value;
    if (!name) {
        _entitySetStatus('entity-save-status', 'Name cannot be empty', 'var(--red)');
        return;
    }
    _entitySetStatus('entity-save-status', 'Saving…');
    const resp = await fetch('/api/entities/' + eid, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({canonical_name: name, description: description}),
    });
    const data = await resp.json();
    if (data.error) {
        _entitySetStatus('entity-save-status', 'Error: ' + data.error, 'var(--red)');
        return;
    }
    _entitySetStatus('entity-save-status', 'Saved.', 'var(--green)');
    await loadEntities();
    await renderEntityDetail(eid);
}

// Tag and alias edits are computed against SERVER truth, not the
// rendered DOM: fetch the entity's current set, mutate it, POST the
// result. DOM-scraping would drift if a concurrent edit landed
// between render and click, and it couldn't see the normalized
// tag_norm the server actually stored.
async function _fetchEntityTags(eid) {
    const data = await fetchJSON('/api/entities/' + eid);
    if (!data || data.error) return null;
    return (data.tags || []);
}

async function addEntityTag(eid) {
    const input = document.getElementById('entity-tag-add');
    const tag = (input.value || '').trim();
    if (!tag) return;
    const current = await _fetchEntityTags(eid);
    if (current === null) {
        _entitySetStatus('entity-tag-status', 'Could not load current tags.', 'var(--red)');
        return;
    }
    const newTags = current.map(t => t.tag).concat([tag]);
    _entitySetStatus('entity-tag-status', 'Adding…');
    const resp = await fetch('/api/entities/' + eid + '/tags', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tags: newTags}),
    });
    const data = await resp.json();
    if (data.error) {
        _entitySetStatus('entity-tag-status', 'Error: ' + data.error, 'var(--red)');
        return;
    }
    input.value = '';
    _entitySetStatus('entity-tag-status', 'Added.', 'var(--green)');
    await loadEntities();
    await renderEntityDetail(eid);
}

async function removeEntityTag(eid, encodedTagNorm) {
    const tagNorm = decodeURIComponent(encodedTagNorm);
    const current = await _fetchEntityTags(eid);
    if (current === null) {
        _entitySetStatus('entity-tag-status', 'Could not load current tags.', 'var(--red)');
        return;
    }
    // Filter by the server-stored tag_norm — an exact, unambiguous key.
    const remaining = current
        .filter(t => t.tag_norm !== tagNorm)
        .map(t => t.tag);
    _entitySetStatus('entity-tag-status', 'Removing…');
    const resp = await fetch('/api/entities/' + eid + '/tags', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tags: remaining}),
    });
    const data = await resp.json();
    if (data.error) {
        _entitySetStatus('entity-tag-status', 'Error: ' + data.error, 'var(--red)');
        return;
    }
    _entitySetStatus('entity-tag-status', 'Removed.', 'var(--green)');
    await loadEntities();
    await renderEntityDetail(eid);
}

async function addEntityAlias(eid) {
    const input = document.getElementById('entity-alias-add');
    const alias = (input.value || '').trim();
    if (!alias) return;
    _entitySetStatus('entity-alias-status', 'Adding…');
    const resp = await fetch('/api/entities/' + eid + '/aliases', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({alias: alias}),
    });
    const data = await resp.json();
    if (data.error) {
        _entitySetStatus('entity-alias-status', 'Error: ' + data.error, 'var(--red)');
        return;
    }
    input.value = '';
    _entitySetStatus('entity-alias-status', 'Added.', 'var(--green)');
    await loadEntities();
    await renderEntityDetail(eid);
}

async function removeEntityAlias(eid, encodedAlias) {
    const alias = decodeURIComponent(encodedAlias);
    _entitySetStatus('entity-alias-status', 'Removing…');
    const resp = await fetch('/api/entities/' + eid + '/aliases', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({alias: alias}),
    });
    const data = await resp.json();
    if (data.error) {
        _entitySetStatus('entity-alias-status', 'Error: ' + data.error, 'var(--red)');
        return;
    }
    _entitySetStatus('entity-alias-status', 'Removed.', 'var(--green)');
    await loadEntities();
    await renderEntityDetail(eid);
}

async function deleteEntity(eid) {
    const detailEl = document.getElementById('entity-detail');
    const name = detailEl && detailEl.querySelector('#entity-name-input');
    const display = name ? name.value : 'this entity';
    if (!window.confirm('Delete entity "' + display + '"? This cascades through tags, aliases, and references.')) {
        return;
    }
    const resp = await fetch('/api/entities/' + eid, {method: 'DELETE'});
    const data = await resp.json();
    if (data.error) {
        if (typeof showToast === 'function') {
            showToast('Error: ' + data.error, 'error');
        } else {
            alert('Error: ' + data.error);
        }
        return;
    }
    _selectedEntityId = null;
    const detail = document.getElementById('entity-detail');
    if (detail) {
        detail.innerHTML = '<div class="empty-state" style="margin-top:80px;">Select an entity to view details</div>';
    }
    await loadEntities();
}

// ---- Create form (inline modal-lite) ----

function openEntityCreateForm() {
    const detail = document.getElementById('entity-detail');
    if (!detail) return;
    detail.innerHTML =
        '<div class="entity-detail-card">' +
        '  <h2 class="entity-create-title">New entity</h2>' +
        '  <div class="entity-detail-row">' +
        '    <label>Canonical name</label>' +
        '    <input id="entity-create-name" class="entity-name-input" placeholder="e.g. Max McKeen" />' +
        '  </div>' +
        '  <div class="entity-detail-row">' +
        '    <label>Description</label>' +
        '    <textarea id="entity-create-desc" class="entity-description-input"' +
        '              placeholder="Free-form. Relationship context lives here."></textarea>' +
        '  </div>' +
        '  <div class="entity-detail-row">' +
        '    <label>Tags (comma-separated, hierarchical OK)</label>' +
        '    <input id="entity-create-tags" class="entity-name-input" placeholder="e.g. person, person/family" />' +
        '  </div>' +
        '  <div class="entity-detail-row">' +
        '    <label>Aliases (comma-separated)</label>' +
        '    <input id="entity-create-aliases" class="entity-name-input" placeholder="e.g. Max, M" />' +
        '  </div>' +
        '  <div class="entity-save-row">' +
        '    <button class="entity-save-btn dirty" onclick="submitEntityCreate()">Create</button>' +
        '    <button class="entity-cancel-btn" onclick="cancelEntityCreate()">Cancel</button>' +
        '    <span id="entity-create-status" class="entity-save-status"></span>' +
        '  </div>' +
        '</div>';
    setTimeout(() => {
        const f = document.getElementById('entity-create-name');
        if (f) f.focus();
    }, 50);
}

function cancelEntityCreate() {
    const detail = document.getElementById('entity-detail');
    if (!detail) return;
    if (_selectedEntityId) {
        renderEntityDetail(_selectedEntityId);
    } else {
        detail.innerHTML = '<div class="empty-state" style="margin-top:80px;">Select an entity to view details</div>';
    }
}

async function submitEntityCreate() {
    const name = document.getElementById('entity-create-name').value.trim();
    if (!name) {
        _entitySetStatus('entity-create-status', 'Name is required.', 'var(--red)');
        return;
    }
    const desc = document.getElementById('entity-create-desc').value;
    const tags = document.getElementById('entity-create-tags').value
        .split(',').map(s => s.trim()).filter(Boolean);
    const aliases = document.getElementById('entity-create-aliases').value
        .split(',').map(s => s.trim()).filter(Boolean);
    _entitySetStatus('entity-create-status', 'Creating…');
    const resp = await fetch('/api/entities', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            canonical_name: name,
            description: desc || null,
            tags: tags,
            aliases: aliases,
        }),
    });
    const data = await resp.json();
    if (data.error) {
        _entitySetStatus('entity-create-status', 'Error: ' + data.error, 'var(--red)');
        return;
    }
    _entitySetStatus('entity-create-status', 'Created.', 'var(--green)');
    await loadEntities();
    if (data.id) await selectEntity(data.id);
}
"""


def styles() -> str:
    return r"""
/* Memory tab — sub-tab bar + entity surfaces */

.memory-subtab-bar {
    display: flex;
    gap: 4px;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--border, #303030);
}

.memory-subtab-btn {
    padding: 6px 12px;
    background: transparent;
    border: none;
    color: var(--text-muted, #888);
    cursor: pointer;
    font-size: 13px;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
}

.memory-subtab-btn.active {
    color: var(--text-primary, #fff);
    border-bottom-color: var(--accent, #6cf);
}

.memory-subtab-panel { display: none; }
.memory-subtab-panel.active { display: block; }

/* List header */
.entity-list-header {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 10px;
}

.entity-new-btn {
    background: var(--accent, #6cf);
    color: var(--bg-primary, #111);
    border: none;
    padding: 6px 10px;
    border-radius: 3px;
    cursor: pointer;
    font-weight: 600;
}

.entity-tag-filter-input {
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text-primary, #fff);
    border: 1px solid var(--border, #303030);
    padding: 5px 8px;
    border-radius: 3px;
    font-size: 12px;
}

/* List rows */
.entity-group-header {
    margin-top: 12px;
    margin-bottom: 4px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted, #888);
}

.entity-group-count {
    color: var(--text-muted, #888);
    font-weight: normal;
}

.entity-list-row {
    padding: 8px 10px;
    border-radius: 3px;
    cursor: pointer;
    margin-bottom: 4px;
    border: 1px solid transparent;
}

.entity-list-row:hover {
    background: var(--bg-secondary, #1a1a1a);
}

.entity-list-row.selected {
    background: var(--bg-secondary, #1a1a1a);
    border-color: var(--accent, #6cf);
}

.entity-list-name {
    font-weight: 600;
    color: var(--text-primary, #fff);
}

.entity-list-aliases {
    font-size: 11px;
    color: var(--text-muted, #888);
    margin-top: 2px;
}

.entity-list-tags {
    margin-top: 4px;
    display: flex;
    flex-wrap: wrap;
    gap: 3px;
}

.entity-tag-chip {
    display: inline-block;
    background: var(--bg-primary, #111);
    color: var(--text-muted, #aaa);
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 10px;
    border: 1px solid var(--border, #303030);
}

/* Detail panel */
.entity-detail-card {
    padding: 0;
}

.entity-detail-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
}

.entity-name-input {
    flex: 1;
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text-primary, #fff);
    border: 1px solid var(--border, #303030);
    padding: 8px 12px;
    border-radius: 3px;
    font-size: 18px;
    font-weight: 600;
}

.entity-delete-btn {
    background: transparent;
    color: var(--red, #f66);
    border: 1px solid var(--red, #f66);
    padding: 6px 12px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 12px;
}

.entity-delete-btn:hover {
    background: var(--red, #f66);
    color: var(--bg-primary, #111);
}

.entity-detail-row {
    margin-bottom: 14px;
}

.entity-detail-row label {
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted, #888);
    margin-bottom: 4px;
}

.entity-description-input {
    width: 100%;
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text-primary, #fff);
    border: 1px solid var(--border, #303030);
    padding: 8px 10px;
    border-radius: 3px;
    font-size: 13px;
    font-family: inherit;
    min-height: 70px;
    resize: vertical;
}

.entity-save-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 6px;
}

.entity-save-btn {
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #303030);
    padding: 5px 12px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 12px;
}

.entity-save-btn.dirty {
    background: var(--accent, #6cf);
    color: var(--bg-primary, #111);
    border-color: var(--accent, #6cf);
}

.entity-cancel-btn {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #303030);
    padding: 5px 12px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 12px;
}

.entity-save-status {
    font-size: 12px;
    color: var(--text-muted, #888);
}

/* Tag + alias chips (editable) */
.entity-tags-list, .entity-aliases-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-bottom: 6px;
    min-height: 24px;
}

.entity-tag-chip-edit, .entity-alias-chip {
    display: inline-flex;
    align-items: center;
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text-primary, #fff);
    padding: 2px 6px;
    border-radius: 10px;
    font-size: 11px;
    border: 1px solid var(--border, #303030);
    gap: 3px;
}

.entity-chip-x {
    background: transparent;
    color: var(--text-muted, #888);
    border: none;
    cursor: pointer;
    padding: 0 2px;
    font-size: 13px;
    line-height: 1;
}

.entity-chip-x:hover {
    color: var(--red, #f66);
}

.entity-tag-add-row, .entity-alias-add-row {
    display: flex;
    gap: 4px;
}

.entity-tag-add-input, .entity-alias-add-input {
    flex: 1;
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text-primary, #fff);
    border: 1px solid var(--border, #303030);
    padding: 4px 8px;
    border-radius: 3px;
    font-size: 12px;
}

.entity-add-chip-btn {
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #303030);
    padding: 4px 10px;
    border-radius: 3px;
    cursor: pointer;
    font-size: 12px;
}

/* Reference log */
.entity-refs-list {
    max-height: 220px;
    overflow-y: auto;
    border: 1px solid var(--border, #303030);
    border-radius: 3px;
    background: var(--bg-secondary, #1a1a1a);
}

.entity-refs-empty {
    padding: 12px;
    color: var(--text-muted, #888);
    font-size: 12px;
    font-style: italic;
}

.entity-ref-row {
    padding: 6px 10px;
    border-bottom: 1px solid var(--border, #303030);
    font-size: 12px;
}

.entity-ref-row:last-child { border-bottom: none; }

.entity-ref-kind {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 10px;
    margin-right: 6px;
    background: var(--bg-primary, #111);
    color: var(--text-muted, #aaa);
    border: 1px solid var(--border, #303030);
}

.entity-ref-path {
    color: var(--text-primary, #fff);
}

.entity-ref-time {
    color: var(--text-muted, #888);
    margin-left: 8px;
    font-size: 11px;
}

.entity-ref-snippet {
    color: var(--text-muted, #aaa);
    margin-top: 3px;
    font-size: 11px;
    padding-left: 12px;
    border-left: 2px solid var(--border, #303030);
}

/* Create form */
.entity-create-title {
    margin: 0 0 16px 0;
    font-size: 18px;
    color: var(--text-primary, #fff);
}
"""
