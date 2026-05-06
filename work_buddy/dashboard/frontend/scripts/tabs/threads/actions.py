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

    function _renderActionSwitcher(thread, action) {
        // The action-switcher dropdown reuses the umbrella's
        // ``action_options`` (cached in ``window._groupState``).
        // Available only when the thread is a group child whose
        // umbrella has been visited in this session — otherwise we
        // skip the switcher rather than make a synchronous fetch
        // from inside an HTML-string renderer.
        const parentId = thread.parent_id;
        if (!parentId
            || !window._groupState
            || !window._groupState.actionOptionsByUmbrella) return '';
        const options = window._groupState.actionOptionsByUmbrella[parentId];
        if (!Array.isArray(options) || options.length === 0) return '';
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
                +     'onclick="threadsGroupSetActionProposal('
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
"""
