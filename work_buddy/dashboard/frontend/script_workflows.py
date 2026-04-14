"""Workflow view polling and tab management JS."""

from __future__ import annotations


def _workflow_views_script() -> str:
    return r"""
// ---- Workflow view polling ----
const _knownViews = new Set();
const _viewRenderers = {};  // viewType → function(container, viewId, payload)
const _viewPayloads = {};   // viewId → payload (cached)
const _pageLoadTime = Date.now() / 1000;  // unix seconds

async function pollWorkflowViews() {
    try {
        const data = await fetchJSON('/api/workflow-views');
        if (!data || !data.views) return;

        const currentIds = new Set(data.views.map(v => v.view_id));

        // Detect new views
        for (const view of data.views) {
            if (!_knownViews.has(view.view_id) && view.status === 'active') {
                _knownViews.add(view.view_id);
                const isCustom = (view.view_type || 'generic') !== 'generic';
                const vt = view.view_type || '';
                const isPalette = vt.startsWith('palette_') || vt === 'workflow_consent' || vt === 'capability_consent';
                // Views that existed before page load: create tab but skip toast
                const isPreExisting = view.created_at && view.created_at < (_pageLoadTime - 5);
                // Custom views auto-open tabs. Palette-originated views
                // go direct-to-view (no toast) and switch immediately.
                if (isCustom) {
                    createWorkflowTab(view);
                    if (isPalette) {
                        switchTab('wv-' + view.view_id);
                        continue;  // skip toast entirely
                    }
                }
                if (!isPreExisting) notifyNewView(view);
            }
        }

        // Detect removed views (dismissed or expired)
        for (const id of _knownViews) {
            if (!currentIds.has(id)) {
                _knownViews.delete(id);
                removeWorkflowTab(id);
            }
        }
    } catch (e) {
        console.error('Workflow view poll error:', e);
    }
}

function createWorkflowTab(view) {
    const tabBar = document.getElementById('workflow-tabs');
    const tabName = 'wv-' + view.view_id;
    if (document.querySelector('.tab-btn[data-tab="' + tabName + '"]')) return;

    const isRequest = view.response_type && view.response_type !== 'none';
    const colorClass = isRequest ? 'tab-request' : 'tab-note';

    const btn = document.createElement('button');
    btn.className = 'tab-btn workflow-tab flash ' + colorClass;
    btn.dataset.tab = tabName;
    btn.dataset.viewId = view.view_id;

    const label = document.createElement('span');
    label.textContent = view.title || 'Workflow';
    btn.appendChild(label);

    const closeBtn = document.createElement('span');
    closeBtn.className = 'tab-close';
    closeBtn.textContent = '\u2715';
    closeBtn.title = 'Close tab';
    closeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        dismissAndRemoveTab(view.view_id);
    });
    btn.appendChild(closeBtn);

    btn.addEventListener('click', () => switchTab(tabName));
    tabBar.appendChild(btn);

    const panel = document.createElement('div');
    panel.className = 'tab-panel';
    panel.id = 'panel-' + tabName;
    panel.innerHTML = '<div class="loading">Loading view...</div>';
    document.body.insertBefore(panel, document.getElementById('toast-container'));

    _viewPayloads[view.view_id] = view.payload;
    _viewMeta[view.view_id] = { response_type: view.response_type, short_id: view.short_id };
    loadWorkflowView(view.view_id);
}

const _viewMeta = {};

async function dismissAndRemoveTab(viewId) {
    try { await fetch('/api/workflow-views/' + viewId + '/dismiss', { method: 'POST' }); } catch(e) {}
    _knownViews.delete(viewId);
    removeWorkflowTab(viewId);
}

function removeWorkflowTab(viewId) {
    const tabName = 'wv-' + viewId;
    const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
    const panel = document.getElementById('panel-' + tabName);
    if (btn) btn.remove();
    if (panel) panel.remove();
    delete _viewPayloads[viewId];
    _renderedViews.delete(viewId);

    // Clean up any lingering toast for this view
    const toast = document.querySelector(`.toast[data-view-id="${viewId}"]`);
    if (toast) toast.remove();

    // Switch to overview if this was active
    const active = document.querySelector('.tab-btn.active');
    if (!active) switchTab('overview');
}

const _renderedViews = new Set();

async function loadWorkflowView(viewId) {
    // Don't re-render if already loaded — preserves live user state
    if (_renderedViews.has(viewId)) return;

    let payload = _viewPayloads[viewId];
    if (!payload) {
        const data = await fetchJSON('/api/workflow-views/' + viewId);
        if (!data) return;
        payload = data.payload;
        _viewPayloads[viewId] = payload;
    }

    const panel = document.getElementById('panel-wv-' + viewId);
    if (!panel) return;

    const viewType = payload.type || 'generic';
    const renderer = _viewRenderers[viewType];
    if (renderer) {
        panel.innerHTML = '';
        renderer(panel, viewId, payload);
        _renderedViews.add(viewId);
    } else {
        panel.innerHTML = '<div class="empty-state">Unknown view type: ' + viewType + '</div>';
    }
}

function registerViewRenderer(type, fn) {
    _viewRenderers[type] = fn;
}

// --- Generic renderer for standard notification/request types ---
registerViewRenderer('generic', async function(container, viewId, payload) {
    const data = await fetchJSON('/api/workflow-views/' + viewId);
    const title = (data && data.title) || 'Notification';
    const body = (data && data.body) || '';
    const meta = (payload && payload.consent_meta) || null;
    const viewMeta = _viewMeta[viewId] || {};
    const isRequest = viewMeta.response_type && viewMeta.response_type !== 'none';
    const shortId = viewMeta.short_id;
    const respType = data.response_type || viewMeta.response_type || 'none';

    let html = '<div style="max-width:640px;margin:2.5em auto;padding:0 1em">';
    html += '<div class="wv-group-card" style="border-radius:12px">';

    // Header
    html += '<div class="wv-group-header" style="padding:16px 20px 12px">';
    html += '<div class="wv-group-header-left">';
    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">';
    if (isRequest) {
        html += '<span class="type-pill request3" style="font-size:11px;padding:3px 10px">REQUEST</span>';
        if (shortId) html += '<span style="color:var(--text-muted);font-size:13px;font-weight:600">#' + escapeHtml(shortId) + '</span>';
    } else {
        html += '<span class="type-pill note1" style="font-size:11px;padding:3px 10px">NOTE</span>';
    }
    if (meta && meta.risk) {
        const riskClass = {low:'high', moderate:'medium', high:'low'}[meta.risk] || 'medium';
        html += '<span class="wv-badge ' + riskClass + '" style="margin-left:4px">' + meta.risk + ' risk</span>';
    }
    html += '</div>';
    html += '<div class="wv-group-intent" style="font-size:17px">' + escapeHtml(title) + '</div>';
    if (meta && meta.operation) {
        html += '<div class="wv-context-subtitle" style="font-size:12px;margin-top:4px"><code style="background:var(--bg-tertiary);padding:2px 8px;border-radius:4px;font-size:12px">'
            + escapeHtml(meta.operation) + '</code></div>';
    }
    html += '</div></div>';

    // Body + response inputs
    html += '<div class="wv-card-main" style="padding:16px 20px 20px">';
    if (body) {
        html += '<p class="wv-rationale" style="font-size:14px;line-height:1.6;color:var(--text-secondary);margin:0 0 16px">' + escapeHtml(body) + '</p>';
    }

    // Button class mapping: approve=green, deny=red, request=purple, neutral=blue, ghost=muted
    const btnClassMap = {
        always: 'nb-btn nb-btn-request', temporary: 'nb-btn nb-btn-neutral',
        once: 'nb-btn nb-btn-ghost', deny: 'nb-btn nb-btn-deny',
        yes: 'nb-btn nb-btn-approve', no: 'nb-btn nb-btn-deny',
    };

    if (respType === 'boolean') {
        html += '<div class="nb-btn-group stretch">';
        html += '<button class="nb-btn nb-btn-approve" onclick="submitGenericResponse(&#39;'+viewId+'&#39;,&#39;yes&#39;)">Yes</button>';
        html += '<button class="nb-btn nb-btn-deny" onclick="submitGenericResponse(&#39;'+viewId+'&#39;,&#39;no&#39;)">No</button>';
        html += '</div>';
    } else if (respType === 'choice') {
        let choices = (data.choices && data.choices.length) ? data.choices : [];
        if (!choices.length && meta) {
            choices = [{key:'always',label:'Allow always',description:'Permanent'},{key:'temporary',label:'Allow for 5 min',description:'Temporary'},{key:'once',label:'Allow once',description:'Single use'},{key:'deny',label:'Deny',description:'Do not proceed'}];
        }
        if (choices.length) {
            html += '<div class="nb-btn-group">';
            for (const c of choices) {
                const key = c.key||'', label = c.label||key, desc = c.description||'';
                const cls = btnClassMap[key] || 'nb-btn nb-btn-request';
                html += '<button class="'+cls+'" onclick="submitGenericResponse(&#39;'+viewId+'&#39;,&#39;'+key+'&#39;)" title="'+escapeHtml(desc)+'">'+escapeHtml(label)+'</button>';
            }
            html += '</div>';
        }
    } else if (respType === 'freeform') {
        html += '<div style="margin-top:8px">';
        html += '<textarea id="freeform-'+viewId+'" rows="4" style="width:100%;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:8px;color:var(--text-primary);padding:12px;font-family:inherit;font-size:14px;resize:vertical;line-height:1.5" placeholder="Type your response..."></textarea>';
        html += '<button class="nb-btn nb-btn-approve" onclick="submitFreeformResponse(&#39;'+viewId+'&#39;,&#39;freeform-'+viewId+'&#39;)" style="margin-top:10px;width:100%">Submit</button>';
        html += '</div>';
    }

    html += '</div></div></div>';
    container.innerHTML = html;
});

async function submitFreeformResponse(viewId, textareaId) {
    const el = document.getElementById(textareaId);
    if (!el || !el.value.trim()) return;
    await submitGenericResponse(viewId, el.value.trim());
}

async function submitGenericResponse(viewId, value) {
    try {
        const resp = await fetch('/api/workflow-views/' + viewId + '/respond', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phase: 'generic', value: value}),
        });
        if (resp.ok) {
            const panel = document.getElementById('panel-wv-' + viewId);
            if (panel) panel.innerHTML = '<div class="empty-state">Response submitted: ' + value + '</div>';
            setTimeout(() => {
                const btn = document.querySelector('.tab-btn[data-tab="wv-' + viewId + '"]');
                if (btn) btn.remove();
                if (panel) panel.remove();
            }, 2000);
        }
    } catch(e) { console.error('Submit failed:', e); }
}

function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text || '';
    return d.innerHTML;
}

// Start polling for workflow views
setInterval(pollWorkflowViews, 3000);
pollWorkflowViews();

// ---- Deep-link hash routing: #view/{notification_id} ----
function handleHashRoute() {
    const hash = window.location.hash;
    if (!hash) return;
    const match = hash.match(/^#view\/(.+)$/);
    if (!match) return;
    const viewId = match[1];
    const tabName = 'wv-' + viewId;

    // Remove the toast for this view (it's being opened directly)
    const toast = document.querySelector('.toast[data-view-id="' + viewId + '"]');
    if (toast) toast.remove();

    // If the tab already exists, just switch to it
    if (document.querySelector('.tab-btn[data-tab="' + tabName + '"]')) {
        switchTab(tabName);
        return;
    }

    // Tab doesn't exist yet — the view may not have arrived via polling.
    // Check the API directly and create the tab if the view exists.
    fetch('/api/workflow-views/' + viewId)
        .then(r => r.ok ? r.json() : null)
        .then(view => {
            if (view && view.status === 'active') {
                createWorkflowTab(view);
                switchTab(tabName);
            }
            // If view not found, it may arrive on the next poll cycle —
            // the hash stays in the URL so hashchange fires again if needed.
        })
        .catch(() => {});
}
window.addEventListener('hashchange', handleHashRoute);
// Check on initial load
handleHashRoute();

// ---- Deep-link polling: Obsidian can POST a target view to /api/deeplink ----
// This enables tab reuse — the existing dashboard tab navigates instead of
// opening a duplicate. Checked every poll cycle alongside workflow views.
async function checkDeepLink() {
    try {
        const resp = await fetch('/api/deeplink');
        const data = await resp.json();
        if (data.pending && data.view_id) {
            window.location.hash = '#view/' + data.view_id;
            window.focus();
        }
    } catch (e) {}
}
setInterval(checkDeepLink, 3000);
"""


# ---------------------------------------------------------------------------
# Browser notifications + in-page toasts
# ---------------------------------------------------------------------------
