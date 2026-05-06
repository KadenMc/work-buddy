"""Browser notification and toast JS."""

from __future__ import annotations


def _notification_script() -> str:
    return """
// ---- Browser notification permission ----
if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
}

function notifyNewView(view) {
    const title = view.title || 'Workflow View';
    const vt = view.view_type || 'generic';
    const isThread = vt === 'thread_chat';
    const body = view.body || (isThread ? 'New conversation' : 'A workflow needs your attention.');
    const viewId = view.view_id;
    const tabName = 'wv-' + viewId;
    // Treat thread_chat as a request so it gets the purple pill / expand behavior
    const isRequest = isThread || (view.response_type && view.response_type !== 'none');
    const isCustom = vt !== 'generic';

    // Always show a toast. Also try a browser notification if tab is hidden.
    const activeBtn = document.querySelector('.tab-btn.active');
    if (!activeBtn || activeBtn.dataset.tab !== tabName) {
        showToast(title, body, tabName, viewId, view, isRequest, isCustom);
    }
    if (document.hidden) {
        sendBrowserNotification(title, body, tabName, viewId, view, isRequest, isCustom);
    }
}

function sendBrowserNotification(title, body, tabName, viewId, view, isRequest, isCustom) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    const n = new Notification(title, { body, icon: '/favicon.svg' });
    n.onclick = () => {
        window.focus();
        _ensureTabAndSwitch(viewId, view, tabName, isRequest, isCustom);
        n.close();
    };
}

window.showToast = function showToast(title, body, tabName, viewId, view, isRequest, isCustom) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toastClass = isRequest ? 'toast toast-request' : 'toast toast-note';
    const shortId = view.short_id ? ' [#' + view.short_id + ']' : '';
    const isThread = (view.view_type || '') === 'thread_chat';
    const pillHtml = isThread
        ? '<span class="type-pill request1">CHAT</span>'
        : isRequest
            ? '<span class="type-pill request1">REQUEST</span>'
            : '<span class="type-pill note1">NOTE</span>';
    // Use the expandable field if set, otherwise fall back to auto-detect
    const isExpandable = view.expandable != null ? view.expandable : (isRequest || (body && body.length > 30));
    const actionLabel = isThread ? 'Click to open chat' : isExpandable ? (isRequest ? 'Click to open' : 'Click to expand') : 'Click to dismiss';

    const toast = document.createElement('div');
    toast.className = toastClass;
    toast.dataset.viewId = viewId;
    toast.style.cursor = 'pointer';
    toast.innerHTML = `
        <div class="toast-header">
            <div style="display:flex;align-items:center;gap:6px">
                ${pillHtml}
                <span class="toast-title">${title}${shortId}</span>
            </div>
            <button class="toast-close">\u2715</button>
        </div>
        <div class="toast-body">${body}</div>
        <div style="font-size:10px;color:var(--text-muted);margin-top:2px">${actionLabel}</div>
    `;
    toast.addEventListener('click', (e) => {
        if (e.target.classList.contains('toast-close')) {
            toast.remove();
            if (!isRequest && !isCustom) {
                fetch('/api/workflow-views/' + viewId + '/dismiss', { method: 'POST' }).catch(() => {});
            }
        } else if (isRequest || isCustom || isExpandable) {
            _ensureTabAndSwitch(viewId, view, tabName, isRequest, isCustom);
            toast.remove();
        } else {
            toast.remove();
            fetch('/api/workflow-views/' + viewId + '/dismiss', { method: 'POST' }).catch(() => {});
        }
    });
    container.appendChild(toast);
}

function _ensureTabAndSwitch(viewId, view, tabName, isRequest, isCustom) {
    if (!document.querySelector('.tab-btn[data-tab="' + tabName + '"]')) {
        createWorkflowTab(view);
    }
    switchTab(tabName);
}

// --- Capability consent renderer ---
registerViewRenderer('capability_consent', async function(container, viewId, payload) {
    const data = await fetchJSON('/api/workflow-views/' + viewId);
    const title = (data && data.title) || 'Consent required';
    const body = (data && data.body) || '';
    const cmdName = payload.command_name || '';
    const operation = payload.operation || '';
    const risk = payload.risk || 'moderate';
    const ttl = payload.default_ttl || 5;
    const hasParams = payload.params && Object.keys(payload.params).length > 0;

    const riskColors = { low: 'high', moderate: 'medium', high: 'low' };  // badge class mapping

    let html = '<div style="max-width:520px;margin:2.5em auto;padding:0 1em">';
    html += '<div class="wv-group-card" style="border-radius:12px">';

    // Header
    html += '<div class="wv-group-header" style="padding:16px 20px 12px">';
    html += '<div class="wv-group-header-left">';
    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">';
    html += '<span class="type-pill request3" style="font-size:11px;padding:3px 10px">CONSENT</span>';
    html += '<span class="wv-badge ' + (riskColors[risk] || 'medium') + '">' + risk + ' risk</span>';
    html += '</div>';
    html += '<div class="wv-group-intent" style="font-size:17px">' + escapeHtml(title) + '</div>';
    html += '<div style="font-size:12px;margin-top:4px"><code style="background:var(--bg-tertiary);padding:2px 8px;border-radius:4px;font-size:12px">'
        + escapeHtml(operation) + '</code></div>';
    html += '</div></div>';

    // Body
    html += '<div class="wv-card-main" style="padding:16px 20px 20px">';
    if (body) {
        html += '<p style="font-size:14px;line-height:1.6;color:var(--text-secondary);margin:0 0 12px">'
            + escapeHtml(body) + '</p>';
    }
    if (hasParams) {
        html += '<p style="font-size:12px;color:var(--text-muted);margin:0 0 12px">Parameters will be replayed automatically after consent.</p>';
    }
    html += '<p style="font-size:13px;color:var(--text-muted);margin:0 0 16px">The command will re-execute automatically upon approval.</p>';

    // Buttons
    html += '<div id="cc-btns-'+viewId+'" class="nb-btn-group">';
    html += '<button class="nb-btn nb-btn-request" onclick="capConsentRespond(&#39;'+viewId+'&#39;,&#39;always&#39;)">Allow always</button>';
    html += '<button class="nb-btn nb-btn-neutral" onclick="capConsentRespond(&#39;'+viewId+'&#39;,&#39;temporary&#39;)">Allow for '+ttl+' min</button>';
    html += '<button class="nb-btn nb-btn-ghost" onclick="capConsentRespond(&#39;'+viewId+'&#39;,&#39;once&#39;)">Allow once</button>';
    html += '<button class="nb-btn nb-btn-deny" onclick="capConsentRespond(&#39;'+viewId+'&#39;,&#39;deny&#39;)">Deny</button>';
    html += '</div>';

    html += '</div></div></div>';
    container.innerHTML = html;
});

window.capConsentRespond = async function(viewId, value) {
    const btns = document.getElementById('cc-btns-' + viewId);
    if (btns) {
        if (value === 'deny') {
            btns.innerHTML = '<span style="color:var(--text-muted)">Denied</span>';
        } else {
            btns.innerHTML = '<span style="color:var(--green,#4caf50);font-weight:600">Granted \u2014 re-executing...</span>';
        }
    }
    try {
        await fetch('/api/workflow-views/' + viewId + '/respond', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: value }),
        });
        // Close tab after brief delay — result tab will auto-open if command produces one
        setTimeout(() => dismissAndRemoveTab(viewId), value === 'deny' ? 500 : 1500);
    } catch(e) { console.error('Consent response failed:', e); }
};

// --- Workflow consent renderer ---
registerViewRenderer('workflow_consent', async function(container, viewId, payload) {
    const data = await fetchJSON('/api/workflow-views/' + viewId);
    const title = (data && data.title) || 'Launch workflow';
    const body = (data && data.body) || '';
    const wfName = (payload && payload.workflow_name) || '';
    const slashCmd = (payload && payload.slash_command) || '';

    let html = '<div style="max-width:520px;margin:2.5em auto;padding:0 1em">';
    html += '<div class="wv-group-card" style="border-radius:12px">';

    // Header
    html += '<div class="wv-group-header" style="padding:16px 20px 12px">';
    html += '<div class="wv-group-header-left">';
    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">';
    html += '<span class="type-pill request3" style="font-size:11px;padding:3px 10px">WORKFLOW</span>';
    html += '<span style="color:var(--orange, #e5a045);font-size:13px;font-weight:600">\u2699 Agent Session</span>';
    html += '</div>';
    html += '<div class="wv-group-intent" style="font-size:17px">' + escapeHtml(title) + '</div>';
    html += '<div style="display:flex;align-items:center;gap:8px;margin-top:4px">';
    if (wfName) {
        html += '<code style="background:var(--bg-tertiary);padding:2px 8px;border-radius:4px;font-size:12px">'
            + escapeHtml(wfName) + '</code>';
    }
    if (slashCmd) {
        html += '<code style="background:var(--accent-subtle);color:var(--accent);padding:2px 8px;border-radius:4px;font-size:12px">/'
            + escapeHtml(slashCmd) + '</code>';
    }
    html += '</div>';
    html += '</div></div>';

    // Body
    html += '<div class="wv-card-main" style="padding:16px 20px 20px">';
    if (body) {
        html += '<p style="font-size:14px;line-height:1.6;color:var(--text-secondary);margin:0 0 12px">'
            + escapeHtml(body) + '</p>';
    }
    html += '<p style="font-size:13px;color:var(--text-muted);margin:0 0 16px">This will open a new Claude Code terminal window.</p>';

    // Optional prompt textarea
    html += '<div style="margin:0 0 16px">';
    html += '<label style="font-size:12px;color:var(--text-muted);display:block;margin-bottom:4px">'
        + 'Additional context (optional)</label>';
    html += '<textarea id="wfc-prompt-' + viewId + '" rows="3" '
        + 'placeholder="Add instructions or context for the agent..." '
        + 'style="width:100%;box-sizing:border-box;background:var(--bg-tertiary);color:var(--text-primary);'
        + 'border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:13px;'
        + 'resize:vertical;font-family:inherit;outline:none"></textarea>';
    html += '</div>';

    // Buttons
    html += '<div id="wfc-btns-'+viewId+'" class="nb-btn-group stretch">';
    html += '<button class="nb-btn nb-btn-approve" onclick="wfConsentRespond(&#39;'+viewId+'&#39;,&#39;launch&#39;)">Launch</button>';
    html += '<button class="nb-btn nb-btn-deny" onclick="wfConsentRespond(&#39;'+viewId+'&#39;,&#39;cancel&#39;)">Cancel</button>';
    html += '</div>';

    html += '</div></div></div>';
    container.innerHTML = html;

    // Focus the textarea for quick typing
    const ta = document.getElementById('wfc-prompt-' + viewId);
    if (ta) ta.focus();
});

window.wfConsentRespond = async function(viewId, value) {
    const btns = document.getElementById('wfc-btns-' + viewId);
    const promptEl = document.getElementById('wfc-prompt-' + viewId);
    const userPrompt = (promptEl && promptEl.value.trim()) || '';
    if (btns) {
        btns.innerHTML = value === 'launch'
            ? '<span style="color:var(--green,#4caf50);font-weight:600">Launching agent session...</span>'
            : '<span style="color:var(--text-muted)">Cancelled</span>';
    }
    try {
        await fetch('/api/workflow-views/' + viewId + '/respond', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: value, user_prompt: userPrompt }),
        });
        // Auto-close tab after short delay
        setTimeout(() => dismissAndRemoveTab(viewId), value === 'launch' ? 2000 : 500);
    } catch(e) { console.error('Consent response failed:', e); }
};

// --- Palette result renderer ---
registerViewRenderer('palette_result', function(container, viewId, payload) {
    const command = payload.command || 'Command';
    const result = payload.result || '';
    const isError = payload.is_error || false;
    const ts = payload.timestamp ? new Date(payload.timestamp * 1000).toLocaleTimeString() : '';

    let html = '<div style="max-width:720px;margin:2em auto;padding:0 1em">';
    html += '<div class="wv-group-card" style="border-radius:12px">';

    // Header
    html += '<div class="wv-group-header" style="padding:16px 20px 12px">';
    html += '<div class="wv-group-header-left">';
    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">';
    const pill = isError
        ? '<span class="type-pill request1" style="font-size:11px;padding:3px 10px">ERROR</span>'
        : '<span class="type-pill note1" style="font-size:11px;padding:3px 10px">RESULT</span>';
    html += pill;
    if (ts) html += '<span style="color:var(--text-muted);font-size:12px">' + escapeHtml(ts) + '</span>';
    html += '</div>';
    html += '<div class="wv-group-intent" style="font-size:17px"><code>' + escapeHtml(command) + '</code></div>';
    html += '</div></div>';

    // Body
    html += '<div class="wv-card-main" style="padding:16px 20px 20px">';
    html += '<pre style="background:var(--bg-tertiary);padding:14px 16px;border-radius:8px;overflow:auto;max-height:60vh;font-size:13px;line-height:1.5;color:var(--text-primary);white-space:pre-wrap;word-break:break-word;margin:0">'
        + escapeHtml(result) + '</pre>';

    // Copy button
    html += '<div style="margin-top:12px;text-align:right">';
    html += '<button class="nb-btn nb-btn-ghost" id="cp-copy-'+viewId+'">Copy</button>';
    html += '</div>';

    html += '</div></div></div>';
    container.innerHTML = html;

    // Bind copy button
    const copyBtn = document.getElementById('cp-copy-' + viewId);
    if (copyBtn) {
        copyBtn.addEventListener('click', function() {
            navigator.clipboard.writeText(result).then(() => {
                this.textContent = 'Copied!';
                setTimeout(() => this.textContent = 'Copy', 1500);
            });
        });
    }
});
"""



# ---------------------------------------------------------------------------
# Triage Clarify view renderer
# ---------------------------------------------------------------------------
