"""Resolution Surface v5 card primitive (Stage 1.9 scaffold).

The v5 FSM publishes ``ResolutionRequest`` messages via the existing
consent system (DESIGN.md §7.3). Each request renders as a
Resolution Surface card; the card kind is derived from the FSM wait
state (DESIGN.md §15.1):

- ``confirmation`` (3 affordances: Accept / Edit / Redirect)
- ``clarification`` (2 affordances: Provide / Skip)
- ``consent``      (3 affordances: Approve / Reject / Edit-then-approve)
- ``review``       (3 affordances: Mark done / Redirect / Drop)
- ``redirect``     (2 affordances: Provide / Skip)

This module registers a frontend view-renderer for type
``resolution_request``. Stage 1.9: scaffold only — the renderer is
registered, the routing key is reserved, and a placeholder card is
rendered when called. **Nothing emits resolution_request views yet
in Stage 1.** Stage 2 wires the FSM to publish them through the
consent system.

Note: the Slice-1.5 ``script_resolution.py`` is a *Triage* surface
decorator. It pre-dates v5 and does not interpret v5
``ResolutionRequest`` payloads. The v5 surface is implemented here
to keep the v4 path intact while v5 stages roll out.
"""

from __future__ import annotations


def _resolution_surface_script() -> str:
    return r"""
// ---------------------------------------------------------------------------
// Resolution Surface v5 — DESIGN.md §15
// ---------------------------------------------------------------------------
//
// View type: 'resolution_request'
// Payload shape (matches work_buddy.threads.models.ResolutionRequest):
//   {
//     thread_id:        'th-abc',
//     fsm_state:        'awaiting_confirmation' | ...,
//     proposing_actor:  'agent' | 'user' | null,
//     urgency:          'defer' | 'surface_now',
//     payload:          { ... state-specific fields ... },
//     deadline:         ISO 8601 | null,
//     parent_event_id:  number | null,
//     card_kind:        'confirmation' | 'clarification' | 'consent' |
//                       'review' | 'redirect',
//   }
// ---------------------------------------------------------------------------

(function() {
    if (typeof registerViewRenderer !== 'function') return;

    // Card-kind → affordances (label + className + actionId)
    const AFFORDANCES = {
        confirmation: [
            { id: 'accept',   label: 'Accept',          className: 'btn-primary' },
            { id: 'edit',     label: 'Edit then accept', className: 'btn-secondary' },
            { id: 'redirect', label: 'Redirect',        className: 'btn-tertiary' },
        ],
        clarification: [
            { id: 'provide',  label: 'Provide',         className: 'btn-primary' },
            { id: 'skip',     label: 'Skip / drop',     className: 'btn-tertiary' },
        ],
        consent: [
            { id: 'approve',  label: 'Approve',         className: 'btn-primary' },
            { id: 'edit',     label: 'Edit parameters', className: 'btn-secondary' },
            { id: 'reject',   label: 'Reject',          className: 'btn-tertiary' },
        ],
        review: [
            { id: 'accept',   label: 'Mark done',       className: 'btn-primary' },
            { id: 'redirect', label: 'Redirect',        className: 'btn-secondary' },
            { id: 'drop',     label: 'Drop',            className: 'btn-tertiary' },
        ],
        redirect: [
            { id: 'provide',  label: 'Provide',         className: 'btn-primary' },
            { id: 'skip',     label: 'Skip / drop',     className: 'btn-tertiary' },
        ],
    };

    function _esc(s) {
        if (s === null || s === undefined) return '';
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function _renderHeader(payload) {
        const kind = payload.card_kind || 'confirmation';
        const tid = payload.thread_id || '?';
        const urgent = payload.urgency === 'surface_now';
        const stateLabel = (payload.fsm_state || '').replace(/_/g, ' ');
        return (
            '<div class="rs-header">'
            + '<span class="rs-kind kind-' + _esc(kind) + '">' + _esc(kind.toUpperCase()) + '</span>'
            + (urgent ? '<span class="rs-urgent">SURFACE NOW</span>' : '')
            + '<span class="rs-state">' + _esc(stateLabel) + '</span>'
            + '<span class="rs-tid">' + _esc(tid) + '</span>'
            + '</div>'
        );
    }

    function _renderAffordances(kind, threadId, parentEventId) {
        const buttons = AFFORDANCES[kind] || AFFORDANCES.confirmation;
        let html = '<div class="rs-affordances">';
        for (const a of buttons) {
            html += '<button class="rs-btn ' + _esc(a.className) + '" '
                + 'data-action="' + _esc(a.id) + '" '
                + 'data-thread-id="' + _esc(threadId) + '" '
                + 'data-parent-event-id="' + _esc(parentEventId || '') + '">'
                + _esc(a.label)
                + '</button>';
        }
        html += '</div>';
        return html;
    }

    function _renderPayloadPreview(payload) {
        // Stage 1.9 stub: pretty-print the state-specific payload
        // dict. Stage 2 swaps this for a per-card-kind renderer
        // that knows how to display proposals (intent guess,
        // context refs, action params, etc.) appropriately.
        let body = '';
        const inner = payload.payload || {};
        if (inner && typeof inner === 'object') {
            const keys = Object.keys(inner);
            if (keys.length === 0) {
                body = '<div class="rs-empty">No payload data.</div>';
            } else {
                body = '<dl class="rs-payload">';
                for (const k of keys) {
                    body += '<dt>' + _esc(k) + '</dt>';
                    body += '<dd>' + _esc(JSON.stringify(inner[k])) + '</dd>';
                }
                body += '</dl>';
            }
        }
        return body;
    }

    async function _submit(action, threadId, parentEventId) {
        // Stage 1.9: stub. Stage 2 POSTs to /api/threads/<id>/resolve
        // with { action, parent_event_id, ... }.
        console.log('[ResolutionSurface] would submit', {
            action, threadId, parentEventId,
        });
    }

    registerViewRenderer('resolution_request', function(container, viewId, payload) {
        if (!payload) {
            container.innerHTML = '<div class="empty-state">Missing resolution payload</div>';
            return;
        }
        const kind = payload.card_kind || 'confirmation';
        container.innerHTML =
            '<div class="rs-card kind-' + _esc(kind) + '">'
            + _renderHeader(payload)
            + '<div class="rs-body">'
            + _renderPayloadPreview(payload)
            + '</div>'
            + _renderAffordances(kind, payload.thread_id, payload.parent_event_id)
            + '</div>';

        // Wire button clicks
        container.querySelectorAll('.rs-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                _submit(
                    btn.dataset.action,
                    btn.dataset.threadId,
                    btn.dataset.parentEventId,
                );
            });
        });
    });

    // Public surface for tests / Stage 2 consumers
    window.__resolutionSurfaceV5 = { AFFORDANCES };
})();
"""


def _resolution_surface_styles() -> str:
    return r"""
.rs-card {
    max-width: 720px;
    margin: 1.5em auto;
    padding: 16px 20px;
    border-radius: 10px;
    background: var(--bg-secondary, #1a1a1a);
    border: 1px solid var(--border, #333);
}
.rs-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 14px;
    flex-wrap: wrap;
}
.rs-kind {
    font-size: 11px;
    font-weight: 700;
    padding: 3px 9px;
    border-radius: 4px;
    background: var(--bg-tertiary, #2a2a2a);
    color: var(--text, #ddd);
}
.rs-kind.kind-confirmation { background: #2d4a8a; color: white; }
.rs-kind.kind-clarification { background: #6b3d8a; color: white; }
.rs-kind.kind-consent { background: #8a3d3d; color: white; }
.rs-kind.kind-review { background: #3d6b3d; color: white; }
.rs-kind.kind-redirect { background: #8a6b3d; color: white; }
.rs-urgent {
    font-size: 11px;
    font-weight: 700;
    padding: 3px 9px;
    border-radius: 4px;
    background: #c0392b;
    color: white;
    text-transform: uppercase;
}
.rs-state {
    font-size: 13px;
    color: var(--text-muted, #888);
    text-transform: capitalize;
}
.rs-tid {
    margin-left: auto;
    font-family: var(--font-mono, monospace);
    font-size: 12px;
    color: var(--text-muted, #888);
}
.rs-body { margin-bottom: 16px; }
.rs-payload {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 4px 12px;
    font-size: 13px;
}
.rs-payload dt {
    font-weight: 600;
    color: var(--text-muted, #888);
}
.rs-payload dd { margin: 0; word-break: break-word; }
.rs-empty {
    font-size: 13px;
    color: var(--text-muted, #888);
    font-style: italic;
}
.rs-affordances {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    flex-wrap: wrap;
}
.rs-btn {
    padding: 8px 16px;
    border-radius: 6px;
    border: none;
    font-size: 13px;
    cursor: pointer;
}
.rs-btn.btn-primary { background: var(--accent, #4a7fc1); color: white; }
.rs-btn.btn-secondary { background: var(--bg-tertiary, #2a2a2a); color: var(--text, #ddd); }
.rs-btn.btn-tertiary { background: transparent; color: var(--text-muted, #888); }
"""
