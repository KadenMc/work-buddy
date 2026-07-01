"""Per-action UI registry — Stage 4.6.

Different Standard Actions need different inputs (calendar event
wants a date picker; send_email wants subject/to/body; etc.).
This module provides:

- ``window.registerActionRenderer(actionName, fn)`` to register a
  specialized renderer.
- ``window.renderActionInRightPane(thread, action)`` — entry point
  used by the right-pane code path.
- ``window.renderActionGeneric`` — schema-driven default renderer
  (reads parameter_schema_for_action when available, else dumps a
  JSON view).
- 5 specialized renderers shipped in 4.6:
    create_calendar_event, create_task, send_email,
    file_reference, decompose

UX.md §7.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ===========================================================================
// Stage 4.6 — Per-action UI registry + specialized renderers
// ===========================================================================

(function () {
    if (typeof window.registerActionRenderer === "function") return;

    function _esc(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    }

    if (!window._actionRenderers) window._actionRenderers = {};

    window.registerActionRenderer = function (actionName, fn) {
        window._actionRenderers[actionName] = fn;
    };

    // Capabilities whose ``parameters`` are bound at dispatch time
    // (not authored ahead of time on the proposal). For these, an
    // empty parameters dict on the proposal is the normal state, not
    // a signal that there's nothing to show. The right-pane editor
    // surfaces a runtime-binding hint instead of "(no parameters)".
    const _RUNTIME_BOUND_BINDINGS = {
        chrome_tab_close: {
            from: "context_items",
            note: "Will close every Chrome tab in this group.",
        },
        chrome_tab_group: {
            from: "context_items",
            note: "Will create a Chrome tab group containing every tab in this group.",
        },
        chrome_tab_move: {
            from: "context_items",
            note: "Will move every tab in this group into a focus window.",
        },
        chrome_route_to_tasks: {
            from: "thread_id",
            note: "Will create one task per tab.",
        },
        chrome_route_to_umbrella_task: {
            from: "thread_id",
            note: "Will create a single umbrella task representing this group.",
        },
    };

    function _renderActionMeta(thread, action) {
        let html = '';
        // Header: capability name + kind badge
        html += '<div class="threads-action-meta-row">'
              + '<span class="threads-action-meta-name">'
              + _esc(action.name || "(unnamed action)")
              + '</span>';
        if (action.kind) {
            html += ' <span class="threads-action-meta-kind">'
                  + _esc(action.kind) + '</span>';
        }
        html += '</div>';
        // Plan summary (the LLM/agent's natural-language description
        // of what'll happen).
        if (action.plan_summary) {
            html += '<p class="threads-action-plan">'
                  + _esc(action.plan_summary) + '</p>';
        }
        // Rationale (why this action was chosen).
        if (action.rationale) {
            html += '<div class="threads-action-rationale">'
                  + '<div class="threads-section-label">Rationale</div>'
                  + '<p>' + _esc(action.rationale) + '</p>'
                  + '</div>';
        }
        // Confidence + model attribution.
        const meta = [];
        if (typeof action.confidence === "number") {
            meta.push('confidence ' + Math.round(action.confidence * 100) + '%');
        }
        if (action.model_used) {
            meta.push('via ' + _esc(action.model_used));
        }
        if (meta.length) {
            html += '<p class="threads-action-attribution">'
                  + meta.join(' · ') + '</p>';
        }
        return html;
    }

    function _renderActionParamsBlock(thread, action) {
        const params = action.parameters || {};
        const keys = Object.keys(params);
        let html = '<div class="threads-section-label">Parameters</div>';
        if (keys.length === 0) {
            const binding = _RUNTIME_BOUND_BINDINGS[action.name];
            if (binding) {
                const ctxItems = thread.context_items || [];
                const count = ctxItems.length;
                html += '<p class="threads-action-runtime-bound">'
                      + _esc(binding.note);
                if (binding.from === "context_items" && count > 0) {
                    html += ' <em>('
                          + count
                          + (count === 1 ? ' item' : ' items')
                          + ' in this thread.)</em>';
                }
                html += '</p>';
            } else {
                html += '<p class="threads-empty">(none)</p>';
            }
            return html;
        }
        html += '<dl class="threads-param-list">';
        for (const k of keys) {
            const v = params[k];
            const text = (typeof v === "string" || typeof v === "number")
                ? String(v)
                : JSON.stringify(v, null, 2);
            html += '<dt>' + _esc(k) + '</dt>';
            html += '<dd>' + (text.length > 100
                ? '<pre>' + _esc(text) + '</pre>'
                : _esc(text)) + '</dd>';
        }
        html += '</dl>';
        return html;
    }

    // Lazy-fetch a thread's own action library when the group cache
    // wasn't populated (the user opened a child directly rather than via
    // the umbrella's group grid). Stores the result under the parent id
    // — the key ``_renderActionSwitcher`` reads — then rerenders.
    // Guarded so each umbrella is fetched at most once in flight; an
    // empty/error result is cached as [] so we don't refetch in a loop.
    function _lazyFetchActionOptions(threadId, parentId) {
        const st = window._groupState;
        if (!st._actionOptionsFetching) st._actionOptionsFetching = {};
        if (st._actionOptionsFetching[parentId]) return;
        st._actionOptionsFetching[parentId] = true;
        fetch('/api/threads/' + encodeURIComponent(threadId)
              + '/action_options')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                st.actionOptionsByUmbrella[parentId] =
                    Array.isArray(data.action_options)
                        ? data.action_options : [];
            })
            .catch(err => {
                console.warn(
                    '[action-switcher] options fetch failed:', err);
                st.actionOptionsByUmbrella[parentId] = [];
            })
            .then(() => {
                delete st._actionOptionsFetching[parentId];
                if (typeof window._renderActiveThread === "function") {
                    window._renderActiveThread();
                }
            });
    }

    function _renderActionSwitcher(thread, action) {
        // The action-switcher dropdown reuses the per-source
        // ``action_options`` (cached in ``window._groupState``, keyed by
        // the umbrella/parent id). When the cache is unpopulated — the
        // user opened this child directly, so the umbrella's group grid
        // never ran — lazy-fetch the thread's own library and rerender.
        const parentId = thread.parent_id;
        if (!parentId
            || !window._groupState
            || !window._groupState.actionOptionsByUmbrella) return '';
        const options = window._groupState.actionOptionsByUmbrella[parentId];
        if (!Array.isArray(options)) {
            _lazyFetchActionOptions(thread.thread_id, parentId);
            return '';
        }
        if (options.length === 0) return '';
        const perGroup = options.filter(
            d => d.cardinality === "per_group",
        );
        if (perGroup.length === 0) return '';
        const tid = JSON.stringify(thread.thread_id);
        let html = '<div class="threads-action-switcher">';
        html += '<div class="threads-section-label">Switch action</div>';
        html += '<div class="threads-action-switcher-options">';
        for (const d of perGroup) {
            const isCurrent = d.capability_name === action.name;
            html += '<button class="threads-action-switcher-option'
                +     (isCurrent ? ' current' : '') + '" '
                +     'title="' + _esc(d.description || '') + '" '
                +     'onclick="threadsSetActionDraft('
                +       tid + ', '
                +       JSON.stringify(d.capability_name) + ')">'
                +     '<span class="label">' + _esc(d.label) + '</span>'
                + '</button>';
        }
        html += '</div></div>';
        return html;
    }

    window.renderActionGeneric = function (action, opts) {
        opts = opts || {};
        const thread = (opts && opts.thread) || { thread_id: opts && opts.threadId };
        let html = '<div class="threads-action-generic">';
        html += _renderActionMeta(thread, action);
        html += _renderActionParamsBlock(thread, action);
        html += _renderActionSwitcher(thread, action);
        if (action.required_contexts && action.required_contexts.length > 0) {
            html += '<p class="threads-action-required-contexts">'
                  + 'Required: ' + action.required_contexts.map(_esc).join(', ')
                  + '</p>';
        }
        html += '</div>';
        return html;
    };

    window.renderActionInRightPane = function (thread, action) {
        // An uncommitted action-switch draft takes over the right pane:
        // blank required fields to fill, then Approve (run) or hand it
        // back for refinement (Redirect). Nothing is persisted until then.
        if (window._draftActionFor
            && window._draftActionFor(thread.thread_id)) {
            return window.renderActionDraft(thread, action);
        }
        const fn = window._actionRenderers[action.name];
        const opts = {
            thread: thread,
            threadId: thread.thread_id,
            actionId: action.id,
            // Helper for renderers — produces the input event
            // attribute string that captures edits via the
            // global threadCardEditActionParam helper.
            paramHandler: function (paramName) {
                const tid = JSON.stringify(thread.thread_id);
                const aid = JSON.stringify(action.id);
                const pname = JSON.stringify(paramName);
                return 'oninput="threadCardEditActionParam('
                       + tid + ', ' + aid + ', ' + pname + ', this.value)"'
                       + ' onchange="threadCardEditActionParam('
                       + tid + ', ' + aid + ', ' + pname + ', this.value)"';
            },
        };
        if (typeof fn === "function") {
            return fn(action, opts);
        }
        return window.renderActionGeneric(action, opts);
    };

    // ===== Action-switch DRAFT (fill-or-refine resolution) ============
    // Switching an action opens an uncommitted client draft with blank
    // required fields. Approve commits + runs it deterministically; the
    // "Issue? Hand it back for refinement" group sends the switch + the
    // fields you filled + an optional note to the agent (Redirect).
    // Nothing hits the server until Approve or Redirect. The draft lives
    // in _groupState (not the thread-detail cache), so an SSE refresh
    // re-renders without wiping it.

    function _draftStore() {
        if (!window._groupState) window._groupState = {};
        if (!window._groupState.draftByThread) {
            window._groupState.draftByThread = {};
        }
        return window._groupState.draftByThread;
    }
    let _draftThreadRef = {};

    window._draftActionFor = function (threadId) {
        return _draftStore()[threadId] || null;
    };
    function _descriptorFor(thread, capabilityName) {
        const parentId = thread.parent_id;
        const opts = (window._groupState
            && window._groupState.actionOptionsByUmbrella
            && window._groupState.actionOptionsByUmbrella[parentId]) || [];
        for (const d of opts) {
            if (d.capability_name === capabilityName) return d;
        }
        return null;
    }
    function _humanize(name) {
        return String(name || "").replace(/_/g, " ")
            .replace(/\bid\b/gi, "ID");
    }
    function _draftRequiredFilled(threadId) {
        const d = window._draftActionFor(threadId);
        const thread = _draftThreadRef[threadId];
        if (!d || !thread) return false;
        const desc = _descriptorFor(thread, d.capability_name);
        const schema = (desc && desc.parameters) || [];
        for (const p of schema) {
            if (p.required && !String(d.params[p.name] || "").trim()) {
                return false;
            }
        }
        return true;
    }

    window.threadsSetActionDraft = function (threadId, capabilityName) {
        _draftStore()[threadId] = {
            capability_name: capabilityName, params: {}, message: "",
        };
        if (typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
    };
    window.threadsCancelDraft = function (threadId) {
        delete _draftStore()[threadId];
        if (typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
    };
    window.threadsDraftEditParam = function (threadId, pname, value) {
        const d = window._draftActionFor(threadId);
        if (!d) return;
        d.params[pname] = value;
        // Toggle Approve without a full re-render (keep input focus).
        const root = document.querySelector(
            '.threads-draft[data-thread-id="' + threadId + '"]');
        if (root) {
            const btn = root.querySelector('.threads-draft-approve');
            const ok = _draftRequiredFilled(threadId);
            if (btn) { btn.disabled = !ok; btn.classList.toggle('ready', ok); }
        }
    };
    window.threadsDraftEditMessage = function (threadId, value) {
        const d = window._draftActionFor(threadId);
        if (d) d.message = value;
        const root = document.querySelector(
            '.threads-draft[data-thread-id="' + threadId + '"]');
        if (root) {
            const w = root.querySelector('.threads-draft-warn');
            if (w) w.remove();
            const g = root.querySelector('.threads-draft-refine');
            if (g) g.classList.remove('warn');
        }
    };

    window.renderActionDraft = function (thread, action) {
        _draftThreadRef[thread.thread_id] = thread;
        const d = window._draftActionFor(thread.thread_id);
        const desc = _descriptorFor(thread, d.capability_name);
        const tid = JSON.stringify(thread.thread_id);
        const toLabel = (desc && desc.label) || d.capability_name;
        const schema = (desc && desc.parameters) || [];
        const required = schema.filter(p => p.required);
        const ready = _draftRequiredFilled(thread.thread_id);

        let html = '<div class="threads-draft" data-thread-id="'
            + _esc(thread.thread_id) + '">';
        html += '<div class="threads-draft-banner">'
            + '<i class="ti ti-arrows-exchange" aria-hidden="true"></i> '
            + 'Switching to <strong>' + _esc(toLabel) + '</strong> — not '
            + 'applied until you approve or hand it back. '
            + '<button class="threads-draft-cancel" '
            +   'onclick="threadsCancelDraft(' + tid + ')">cancel</button>'
            + '</div>';

        html += '<div class="threads-draft-fields">';
        for (const p of required) {
            const val = d.params[p.name] || "";
            html += _field(_humanize(p.name) + ' *',
                '<input type="text" class="threads-input" '
                + 'value="' + _esc(val) + '" '
                + 'placeholder="' + _esc(p.description || "") + '" '
                + 'oninput="threadsDraftEditParam(' + tid + ', '
                +   JSON.stringify(p.name) + ', this.value)">');
        }
        html += '</div>';

        html += '<div class="threads-draft-approve-row">'
            + '<span class="threads-draft-hint" '
            +   'title="Runs it exactly as filled. No agent, and the note '
            +   'below is not read.">Runs it as filled <i class="ti '
            +   'ti-info-circle" aria-hidden="true"></i></span>'
            + '<button class="threads-draft-approve' + (ready ? ' ready' : '')
            +   '" ' + (ready ? '' : 'disabled ')
            +   'onclick="threadsApproveDraft(' + tid + ')">Approve</button>'
            + '</div>';

        html += '<div class="threads-draft-refine">'
            + '<div class="threads-draft-refine-title">'
            +   '<i class="ti ti-arrow-back-up" aria-hidden="true"></i> '
            +   'Issue? Hand it back for refinement</div>'
            + '<textarea class="threads-draft-message" rows="2" '
            +   'placeholder="Optional note. e.g. use the mindfulness log, '
            +   'not the meditation note" '
            +   'oninput="threadsDraftEditMessage(' + tid + ', this.value)">'
            +   _esc(d.message || "") + '</textarea>'
            + '<div class="threads-draft-refine-row">'
            +   '<span class="threads-draft-hint" title="Sends the switch, '
            +     'whatever you filled in, and this note. The agent '
            +     'refines it and you review before anything runs.">Sends '
            +     'to the agent <i class="ti ti-info-circle" '
            +     'aria-hidden="true"></i></span>'
            +   '<button class="threads-draft-redirect" '
            +     'onclick="threadsRedirectDraft(' + tid + ')">Redirect</button>'
            + '</div></div>';

        html += '</div>';
        return html;
    };

    function _clearDraftAndRerender(threadId) {
        delete _draftStore()[threadId];
        try {
            if (typeof window.invalidateThreadCache === 'function') {
                window.invalidateThreadCache(threadId);
            }
        } catch (e) { /* best-effort */ }
        if (typeof window._renderActiveThread === 'function') {
            window._renderActiveThread();
        }
    }

    window.threadsApproveDraft = function (threadId) {
        const d = window._draftActionFor(threadId);
        if (!d || !_draftRequiredFilled(threadId)) return;
        if (String(d.message || "").trim()) {
            // Guard: the refinement note only sends with Redirect. Warn
            // rather than silently drop it (the "agent isn't listening"
            // failure).
            const g = document.querySelector(
                '.threads-draft[data-thread-id="' + threadId
                + '"] .threads-draft-refine');
            if (g && !g.querySelector('.threads-draft-warn')) {
                g.classList.add('warn');
                const w = document.createElement('div');
                w.className = 'threads-draft-warn';
                w.textContent = 'Your note only sends with Redirect. '
                    + 'Approve ignores it — clear the note, or hand it '
                    + 'back to send it.';
                g.appendChild(w);
            }
            return;
        }
        fetch('/api/threads/' + encodeURIComponent(threadId) + '/accept', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: {
                capability_name: d.capability_name, parameters: d.params,
            } }),
        }).then(r => r.json().then(b => ({ ok: r.ok, body: b })))
          .then(({ ok, body }) => {
            if (!ok) { window.alert('Approve failed: ' + (body.error || '')); return; }
            _clearDraftAndRerender(threadId);
          }).catch(e => window.alert('Approve failed: ' + e));
    };

    window.threadsRedirectDraft = function (threadId) {
        const d = window._draftActionFor(threadId);
        if (!d) return;
        fetch('/api/threads/' + encodeURIComponent(threadId)
              + '/redirect_action', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target_action: d.capability_name,
                params: d.params,
                feedback: String(d.message || ''),
            }),
        }).then(r => r.json().then(b => ({ ok: r.ok, body: b })))
          .then(({ ok, body }) => {
            if (!ok) { window.alert('Redirect failed: ' + (body.error || '')); return; }
            _clearDraftAndRerender(threadId);
          }).catch(e => window.alert('Redirect failed: ' + e));
    };

    // ----- Specialized renderers -------------------------------------

    // create_calendar_event
    window.registerActionRenderer("create_calendar_event", function (action, opts) {
        const p = action.parameters || {};
        const h = (opts && opts.paramHandler) || (() => '');
        return ''
            + '<div class="threads-action create_calendar_event">'
            + '<h4>Create calendar event</h4>'
            + _field("Title",
                '<input type="text" class="threads-input" '
                + h("title") + ' '
                + 'value="' + _esc(p.title || "") + '">')
            + _field("When",
                '<input type="datetime-local" class="threads-input" '
                + h("datetime") + ' '
                + 'value="' + _esc((p.datetime || "").slice(0, 16)) + '">')
            + _field("Duration (min)",
                '<input type="number" class="threads-input" '
                + h("duration_minutes") + ' '
                + 'value="' + _esc(p.duration_minutes || 60) + '">')
            + (p.location !== undefined ? _field("Location",
                '<input type="text" class="threads-input" '
                + h("location") + ' '
                + 'value="' + _esc(p.location || "") + '">') : '')
            + '</div>';
    });

    // create_task
    window.registerActionRenderer("create_task", function (action, opts) {
        const p = action.parameters || {};
        const h = (opts && opts.paramHandler) || (() => '');
        return ''
            + '<div class="threads-action create_task">'
            + '<h4>Create task</h4>'
            + _field("Description",
                '<textarea class="threads-textarea" rows="3" '
                + h("description") + '>'
                + _esc(p.description || p.text || "") + '</textarea>')
            + _field("Due date",
                '<input type="date" class="threads-input" '
                + h("due_date") + ' '
                + 'value="' + _esc(p.due_date || "") + '">')
            + _field("Namespace",
                '<input type="text" class="threads-input" '
                + h("namespace") + ' '
                + 'value="' + _esc(p.namespace || "") + '" '
                + 'placeholder="paper/ecg-classifier">')
            + _field("Priority",
                '<select class="threads-input" ' + h("priority") + '>'
                + ['low', 'medium', 'high'].map(o =>
                    '<option ' + ((p.priority || "medium") === o ? 'selected' : '')
                    + '>' + o + '</option>').join('')
                + '</select>')
            + '</div>';
    });

    // send_email
    window.registerActionRenderer("send_email", function (action, opts) {
        const p = action.parameters || {};
        const h = (opts && opts.paramHandler) || (() => '');
        const to = Array.isArray(p.to) ? p.to.join(', ') : (p.to || "");
        return ''
            + '<div class="threads-action send_email">'
            + '<h4>Send email</h4>'
            + _field("To",
                '<input type="email" class="threads-input" '
                + h("to") + ' '
                + 'value="' + _esc(to) + '" '
                + 'placeholder="comma-separated">')
            + _field("Subject",
                '<input type="text" class="threads-input" '
                + h("subject") + ' '
                + 'value="' + _esc(p.subject || "") + '">')
            + _field("Body",
                '<textarea class="threads-textarea" rows="10" '
                + h("body") + '>'
                + _esc(p.body || "") + '</textarea>')
            + '<div class="threads-action-warn">'
            + 'Email is irreversible — once sent, it cannot be unsent. '
            + 'Review carefully before approving.</div>'
            + '</div>';
    });

    // file_reference (Slice 6 — file content into the vault)
    window.registerActionRenderer("file_reference", function (action, opts) {
        const p = action.parameters || {};
        const h = (opts && opts.paramHandler) || (() => '');
        return ''
            + '<div class="threads-action file_reference">'
            + '<h4>File reference</h4>'
            + _field("Destination path",
                '<input type="text" class="threads-input" '
                + h("path") + ' '
                + 'value="' + _esc(p.path || "") + '" '
                + 'placeholder="Research/ECG/aug.md">')
            + _field("Content",
                '<textarea class="threads-textarea" rows="6" '
                + h("content") + '>'
                + _esc(p.content || "") + '</textarea>')
            + (p.append_to_existing
                ? '<p class="threads-stage-note">Will append to existing file.</p>'
                : '')
            + '</div>';
    });

    // decompose
    window.registerActionRenderer("decompose", function (action) {
        const p = action.parameters || {};
        const items = p.source_items || [];
        let html = '<div class="threads-action decompose">';
        html += '<h4>Decompose into sub-threads</h4>';
        html += _field("Strategy",
            '<input type="text" class="threads-input" '
            + 'value="' + _esc(p.strategy || "") + '" '
            + 'placeholder="chrome_tabs / journal_segments / ...">');
        html += '<div class="threads-decompose-items">'
              + '<div class="threads-section-label">Source items ('
              + items.length + ')</div>';
        if (items.length === 0) {
            html += '<p class="threads-empty">(none)</p>';
        } else {
            html += '<ul class="threads-list">';
            for (const item of items) {
                html += '<li class="threads-item">';
                html += '<div class="threads-item-label">'
                      + _esc(item.label || item.id || "(item)") + '</div>';
                if (item.source) {
                    html += '<div class="threads-item-source">'
                          + _esc(item.source) + '</div>';
                }
                html += '</li>';
            }
            html += '</ul>';
        }
        html += '</div>';
        if (p.sub_thread_action_bias) {
            html += _field("Sub-thread action bias",
                '<input type="text" class="threads-input" '
                + 'value="' + _esc(p.sub_thread_action_bias) + '" '
                + 'readonly>');
        }
        html += '</div>';
        return html;
    });

    // ----- Helper for label/control pairs ----------------------------

    function _field(label, control) {
        return ''
            + '<div class="threads-action-field">'
            + '<label>' + _esc(label) + '</label>'
            + control
            + '</div>';
    }
})();
"""


def styles() -> str:
    return r"""
/* Stage 4.6 — per-action specialized UI */

.threads-action {
    color: var(--text, #ddd);
}

.threads-action h4 {
    margin: 0 0 12px 0;
    font-size: 13px;
    font-weight: 600;
    color: var(--text, #ddd);
}

.threads-action-field {
    margin-bottom: 10px;
}

.threads-action-field label {
    display: block;
    font-size: 11px;
    color: var(--text-muted, #888);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 4px;
}

.threads-input,
.threads-action .threads-textarea {
    width: 100%;
    padding: 6px 10px;
    background: var(--bg, #0a0a0a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    font: inherit;
    font-size: 13px;
}

.threads-action .threads-textarea {
    resize: vertical;
    font-family: var(--font-mono, monospace);
}

.threads-action-warn {
    background: #4a2424;
    color: #fbcaca;
    padding: 8px 10px;
    border-radius: 4px;
    font-size: 12px;
    margin-top: 8px;
}

.threads-action-required-contexts {
    color: var(--text-muted, #888);
    font-size: 11px;
    margin-top: 8px;
}

.threads-action-generic .threads-param-list {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 4px 12px;
    font-size: 13px;
}

.threads-action-generic dt {
    color: var(--text-muted, #888);
    font-weight: 600;
}

.threads-action-generic dd {
    margin: 0;
    color: var(--text, #ddd);
    word-break: break-word;
}

.threads-action-generic dd pre {
    background: var(--bg, #0a0a0a);
    padding: 4px 8px;
    border-radius: 4px;
    overflow-x: auto;
    font-size: 11px;
}

.threads-decompose-items {
    margin-top: 12px;
}

/* Generic action-meta block (shown in the right pane when the user
 * clicks Edit action on a sub-thread). Surfaces the capability name,
 * plan summary, rationale, attribution, parameters (or runtime-binding
 * hint), and an inline action switcher. */

.threads-action-meta-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 8px;
}

.threads-action-meta-name {
    font-size: 14px;
    font-weight: 600;
    color: var(--text, #ddd);
    font-family: var(--font-mono, monospace);
}

.threads-action-meta-kind {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 1px 5px;
}

.threads-action-plan {
    margin: 0 0 12px 0;
    color: var(--text, #ddd);
    font-size: 13px;
    line-height: 1.4;
}

.threads-action-rationale {
    margin: 12px 0;
    padding-left: 10px;
    border-left: 2px solid var(--border, #333);
}

.threads-action-rationale p {
    margin: 4px 0 0 0;
    color: var(--text-muted-2, #aaa);
    font-size: 13px;
    line-height: 1.4;
    font-style: italic;
}

.threads-action-attribution {
    margin: 4px 0 12px 0;
    color: var(--text-muted, #888);
    font-size: 11px;
}

.threads-action-runtime-bound {
    margin: 4px 0 12px 0;
    color: var(--text-muted-2, #aaa);
    font-size: 12px;
    line-height: 1.4;
}

.threads-action-runtime-bound em {
    color: var(--text-muted, #888);
    font-style: normal;
}

.threads-action-switcher {
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid var(--border, #333);
}

.threads-action-switcher-options {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 6px;
}

.threads-action-switcher-option {
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 12px;
    color: var(--text, #ddd);
    cursor: pointer;
}

.threads-action-switcher-option:hover {
    border-color: var(--accent, #4a7fc1);
    color: var(--accent, #4a7fc1);
}

.threads-action-switcher-option.current {
    background: var(--accent-dim, #1f3a55);
    border-color: var(--accent, #4a7fc1);
    color: var(--text, #ddd);
}

.threads-action-switcher-option.current::after {
    content: " ✓";
}

/* Action-switch draft editor (fill-or-refine). */
.threads-draft {
    display: flex;
    flex-direction: column;
    gap: 12px;
}
.threads-draft-banner {
    font-size: 12px;
    background: var(--bg-warning, #3a3320);
    color: var(--text-warning, #e0c060);
    border-radius: 6px;
    padding: 8px 10px;
    line-height: 1.5;
}
.threads-draft-banner strong { color: var(--text, #ddd); }
.threads-draft-cancel {
    background: transparent;
    border: none;
    color: var(--text-muted, #888);
    cursor: pointer;
    text-decoration: underline;
    font-size: 12px;
    padding: 0 2px;
}
.threads-draft-fields { display: flex; flex-direction: column; gap: 10px; }
.threads-draft-approve-row,
.threads-draft-refine-row {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
}
.threads-draft-hint {
    font-size: 11px;
    color: var(--text-muted, #888);
    margin-right: auto;
    cursor: help;
}
.threads-draft-approve {
    border: 1px solid var(--border, #333);
    background: transparent;
    color: var(--text-muted, #888);
    border-radius: 4px;
    padding: 6px 16px;
    font-size: 13px;
    cursor: not-allowed;
}
.threads-draft-approve.ready {
    background: var(--accent, #4a7fc1);
    border-color: var(--accent, #4a7fc1);
    color: #fff;
    cursor: pointer;
}
.threads-draft-refine {
    border: 1px solid var(--border, #333);
    border-left: 2px solid var(--accent-2, #8f7fdd);
    border-radius: 0 6px 6px 0;
    padding: 10px 12px;
    background: var(--bg-secondary, #1a1a1a);
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.threads-draft-refine-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--accent-2, #8f7fdd);
}
.threads-draft-message {
    width: 100%;
    font-size: 13px;
    resize: vertical;
    background: var(--bg, #0a0a0a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 6px 10px;
}
.threads-draft-redirect {
    border: 1px solid var(--accent-2, #8f7fdd);
    background: transparent;
    color: var(--accent-2, #8f7fdd);
    border-radius: 4px;
    padding: 6px 16px;
    font-size: 13px;
    cursor: pointer;
}
.threads-draft-refine.warn { border-color: var(--danger, #c0504d); }
.threads-draft-warn {
    font-size: 12px;
    color: var(--danger-text, #f0a0a0);
    background: var(--bg-danger, #3a2020);
    border-radius: 4px;
    padding: 8px 10px;
}
"""
