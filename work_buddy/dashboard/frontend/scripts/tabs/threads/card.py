"""Threads tab — confirmation card layout (visual).

Implements the two-pane confirmation card layout (UX.md §4 + §5).

Exposed:
- ``window.renderConfirmationCard(threadData)`` — returns HTML for
  the card. Used by ``renderThreadDetail`` (in tabs/threads/main.py).
- ``window.threadCardState`` — per-card local state (X-flags, edits,
  focused element). Persists in-page until card refresh.

The card data shape (mocked in 4.2; real backend in 4.3):
    {
        thread_id: str,
        title: str,
        urgency: 'defer' | 'surface_now',
        fsm_state: str,
        intent: { text: str, editable: bool },
        context_items: [
            { id: 'ci-N', label: str, payload: dict, source: str, type: str }
        ],
        actions: [
            { id: 'act-N', name: str, kind: 'standard'|'improvised'|'suggestion',
              parameters: dict, plan_summary: str | null,
              required_contexts: [str] }
        ],
        namespace_tags: [str],
        can_clean_up: bool,  // server tells us iff cleanup adapter applicable
        sub_thread_count: int,
    }
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ===========================================================================
// Stage 4.2 — Confirmation card layout (visual only)
// ===========================================================================

(function () {
    if (typeof window.renderConfirmationCard === "function") return;

    function _esc(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
    }

    // ----- Per-card local state ----------------------------------------
    //
    // Keyed by thread_id. Persists until the card is reloaded
    // (e.g., after the user navigates away or after a backend
    // commit returns fresh data).
    //
    //   { 'th-abc': { flagged: Set, edited: { intent: '...', ... }, focusedId: 'act-2' } }
    //
    if (!window.threadCardState) window.threadCardState = {};

    function _state(threadId) {
        if (!window.threadCardState[threadId]) {
            window.threadCardState[threadId] = {
                flagged: new Set(),       // ids of X-flagged elements
                edited: {},                // free-form edit overlay
                focusedId: null,           // currently-focused element
            };
        }
        return window.threadCardState[threadId];
    }

    function _hasFlags(threadId) {
        return _state(threadId).flagged.size > 0;
    }

    // ----- Public toggle helpers ---------------------------------------
    //
    // Wired into card buttons via inline onclick. They mutate local
    // state and re-render. Backend commit is Stage 4.3.

    window.threadCardToggleFlag = function (threadId, elementId) {
        const s = _state(threadId);
        if (s.flagged.has(elementId)) s.flagged.delete(elementId);
        else s.flagged.add(elementId);
        // Re-render whole card so footer buttons update gating
        if (typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
    };

    window.threadCardFocus = function (threadId, elementId) {
        _state(threadId).focusedId = elementId;
        if (typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
    };

    window.threadCardEditIntent = function (threadId, value) {
        _state(threadId).edited.intent = value;
        // No re-render — the user is typing
    };

    // 2026-05-03 — explicit confirm/discard for the intent editor.
    // The right-pane editor used to have no submit button, just the
    // implicit "edits flow into the next Accept click" path; users
    // had no way to lock in or back out of an edit without touching
    // the card. Now the editor has a ↩ confirm and × discard, plus
    // Enter / Esc keyboard semantics (Shift+Enter still inserts a
    // newline). Confirming locks the edit into ``s.edited.intent``
    // and closes the right pane; discarding clears the edit and
    // closes the right pane. The actual FSM commit still happens
    // when the user clicks Accept on the main card — the right
    // pane is for staging edits, not for executing them.
    window.threadCardConfirmIntentEdit = function (threadId) {
        const s = _state(threadId);
        s.focusedId = null;
        if (typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
    };
    window.threadCardDiscardIntentEdit = function (threadId) {
        const s = _state(threadId);
        delete s.edited.intent;
        s.focusedId = null;
        if (typeof window._renderActiveThread === "function") {
            window._renderActiveThread();
        }
    };

    // Per-action redirect on a singular-hoisted action chip. Prompts
    // for steering feedback, POSTs to /api/threads/<host>/redirect_action
    // which records a KIND_ACTION_REDIRECTED event and re-runs ONLY
    // action-layer inference (no walk back through intent/context).
    // On success, refreshes the thread view so the new (or pending)
    // action_inferred surfaces. Wired from the Redirect button in
    // ``_renderActionsSection``.
    window.threadCardRedirectAction = async function (hostThreadId) {
        const feedback = window.prompt(
            "Redirect this action — describe what you want different\\n"
            + "(e.g. 'reminder a week earlier', 'use different parameters'):",
            ""
        );
        if (feedback === null) return;  // user cancelled
        const trimmed = (feedback || "").trim();
        if (!trimmed) return;  // empty input
        try {
            const resp = await fetch(
                "/api/threads/" + encodeURIComponent(hostThreadId)
                + "/redirect_action",
                {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({feedback: trimmed}),
                },
            );
            const body = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                window.alert(
                    "Redirect failed: " + (body.error || resp.statusText)
                );
                return;
            }
            // Trigger a refresh of the active thread view so the
            // re-inferred action surfaces when it lands.
            if (typeof window._renderActiveThread === "function") {
                window._renderActiveThread();
            }
        } catch (e) {
            window.alert("Redirect failed: " + (e && e.message || e));
        }
    };
    // Keyboard handler shared by every Threads textarea. Attach via
    // ``onkeydown="return threadCardEditorKeydown(event, '<tid>', 'intent')"``.
    // Returns false to prevent the default newline insertion when we
    // intercept Enter / Esc; passes through Shift+Enter as a newline.
    window.threadCardEditorKeydown = function (event, threadId, target) {
        if (event.key === "Enter" && !event.shiftKey
            && !event.ctrlKey && !event.metaKey && !event.altKey) {
            event.preventDefault();
            if (target === "intent") {
                window.threadCardConfirmIntentEdit(threadId);
            }
            return false;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            if (target === "intent") {
                window.threadCardDiscardIntentEdit(threadId);
            }
            return false;
        }
        // Shift+Enter / plain typing: pass through.
        return true;
    };

    // Wave G — capture edits to action parameters in the right-pane
    // form. Each input/textarea/select wires
    // ``oninput="threadCardEditActionParam(tid, actId, paramName, this.value)"``.
    // We store under ``s.edited.action_params[actionId][paramName]``.
    // On Accept, threadCommitAction merges this into the request
    // body so the user's edits flow through the FSM transition
    // event log (and, eventually, the action dispatcher).
    window.threadCardEditActionParam = function (
        threadId, actionId, paramName, value,
    ) {
        const s = _state(threadId);
        if (!s.edited.action_params) s.edited.action_params = {};
        if (!s.edited.action_params[actionId]) {
            s.edited.action_params[actionId] = {};
        }
        s.edited.action_params[actionId][paramName] = value;
        // No re-render — the user is typing
    };

    // Public helper — the commit handler in tabs/threads/main.py
    // calls this to assemble the request body for /api/threads/<id>/accept.
    // Returns ``null`` if there are no edits (so we don't pollute
    // the request).
    window.threadCardCollectEdits = function (threadId) {
        const s = _state(threadId);
        const out = {};
        if (s.edited.intent !== undefined) {
            out.intent_override = s.edited.intent;
        }
        if (s.edited.action_params
            && Object.keys(s.edited.action_params).length > 0) {
            out.action_overrides = s.edited.action_params;
        }
        return Object.keys(out).length > 0 ? out : null;
    };

    // ----- Card rendering ----------------------------------------------

    window.renderConfirmationCard = function renderConfirmationCard(thread) {
        if (!thread) {
            return '<div class="threads-card-empty">No thread loaded.</div>';
        }
        // Dispatch by card_kind (Stage 4.5 — UX.md §4.2).
        // Defaults to 'confirmation' for backward compatibility.
        const kind = thread.card_kind || "confirmation";
        if (kind === "clarification") return _renderClarification(thread);
        if (kind === "review") return _renderReview(thread);
        if (kind === "redirect") return _renderRedirect(thread);
        if (kind === "cleanup_failure") return _renderCleanupFailure(thread);
        // confirmation / consent — same shape; consent emphasizes risk
        return _renderConfirmationOrConsent(thread, kind);
    };

    function _renderConfirmationOrConsent(thread, kind) {
        const s = _state(thread.thread_id);
        const hasFlags = _hasFlags(thread.thread_id);

        // User-feedback fix #1 (2026-05-03 morning): hide the
        // right pane unless something is focused. When focused,
        // show an X to clear focus and return to full-width body.
        const focused = !!s.focusedId;

        let html = '<div class="threads-card threads-kind-' + kind
                 + (focused ? ' threads-with-right-pane' : ' threads-full-width')
                 + '" '
                 + 'data-thread-id="' + _esc(thread.thread_id) + '">';

        // Risk-amplifier emphasis for consent cards
        if (kind === "consent") {
            html += _renderRiskBanner(thread);
        }

        html += '<div class="threads-card-body">';
        html += '<div class="threads-card-left">';
        html += _renderHeader(thread);
        html += _renderIntentSection(thread, s);
        html += _renderContextSection(thread, s);
        html += _renderActionsSection(thread, s);
        html += _renderNamespaceTagsSection(thread, s);
        html += _renderSubThreadsLink(thread);
        html += '</div>';
        if (focused) {
            html += '<div class="threads-card-right">';
            html += '<button class="threads-right-close" '
                  +   'title="Close editor (Esc)" '
                  +   wbActAttrs('threadCardCloseRightPane', {threadId: thread.thread_id}) + '>'
                  +   _icon("x") + '</button>';
            html += _renderRightPane(thread, s);
            html += '</div>';
        }
        html += '</div>';

        html += _renderFooter(thread, hasFlags);
        html += '</div>';
        return html;
    }

    function _renderRiskBanner(thread) {
        // Aggregate risk indicators across actions. Reads BOTH the
        // top-level fields (irreversibility, regret_potential,
        // risk_amplifier — set by improvised actions) AND the
        // intrinsic_amplifiers map (Standard Action template-level).
        // Either source qualifies as a "high impact" signal.
        let riskBits = [];
        const seen = new Set();
        for (const a of (thread.actions || [])) {
            const candidates = [];
            if (a.irreversibility === "high")
                candidates.push("irreversibility=high");
            if (a.regret_potential === "high")
                candidates.push("regret_potential=high");
            if (a.risk_amplifier === true)
                candidates.push("risk_amplifier");
            const amp = a.intrinsic_amplifiers || {};
            for (const dim of Object.keys(amp)) {
                const val = amp[dim];
                if (val === "high" || val === "irreversible")
                    candidates.push(dim + "=" + val);
            }
            for (const c of candidates) {
                if (!seen.has(c)) {
                    seen.add(c);
                    riskBits.push(c);
                }
            }
        }
        if (riskBits.length === 0) return '';
        return (
            '<div class="threads-risk-banner">'
            + _icon("alert-triangle")
            + ' <strong>Consent gate:</strong> '
            + 'high-impact action — ' + _esc(riskBits.join(', '))
            + '</div>'
        );
    }


    // ----- Section renderers -------------------------------------------

    // Wave F — friendlier state copy. The raw FSM state names are
    // engineering-internal; the user shouldn't see "awaiting
    // confirmation" but rather "Waiting on you to approve."
    function _friendlyState(state) {
        return ({
            "awaiting_intent_confirmation": "Confirm intent",
            "awaiting_intent_clarification": "Need clarification on intent",
            "awaiting_context_confirmation": "Confirm context",
            "awaiting_context_clarification": "Need clarification on context",
            "awaiting_action_clarification": "Need clarification on action",
            "awaiting_confirmation": "Approve action",
            "awaiting_review": "Review result",
            "awaiting_redirect": "Action failed — redirect needed",
            "awaiting_inference": "Queueing inference",
            "inferring_intent": "Inferring intent…",
            "inferring_context": "Inferring context…",
            "inferring_action": "Inferring action…",
            "executing": "Executing",
            "monitoring": "Monitoring sub-threads",
            "cleaning_up": "Cleaning up source",
            "done_cleanup_unsuccessful": "Cleanup failed",
            "done_cleanup_successful": "Done · source cleaned",
            "done": "Done",
            "dismissed": "Dismissed",
            "handed_off": "Handed off",
            "proposed": "Proposed",
        }[state]) || (state || "").replace(/_/g, " ");
    }

    function _renderHeader(thread) {
        const urgent = thread.urgency === "surface_now";
        const stateLabel = _friendlyState(thread.fsm_state);
        // Risk highlight pill — derived from action risk metadata
        // (irreversibility / regret_potential / risk_amplifier).
        // The consent card uses this to draw attention before the
        // user clicks Accept on something that mutates the world.
        const risk = thread.risk_highlight;
        let riskPill = '';
        if (risk === "high") {
            riskPill = '<span class="threads-risk-pill high">HIGH RISK</span>';
        } else if (risk === "medium") {
            riskPill = '<span class="threads-risk-pill medium">MEDIUM RISK</span>';
        } else if (risk === "low") {
            riskPill = '<span class="threads-risk-pill low">LOW RISK</span>';
        }
        // Relative timestamp — "5m ago" / "just now" / "2h ago".
        // Derived from latest_activity (most recent event ts).
        const ts = thread.latest_activity
            ? '<span class="threads-timestamp" '
              + 'title="' + _esc(thread.latest_activity) + '">'
              + _relativeTime(thread.latest_activity) + '</span>'
            : '';
        return (
            '<div class="threads-card-header">'
            + '<div class="threads-card-title">'
            +   _esc(thread.title || thread.thread_id)
            + '</div>'
            + '<div class="threads-card-meta">'
            +   '<span class="threads-state">' + _esc(stateLabel) + '</span>'
            +   (urgent
                    ? '<span class="threads-urgency-pill high">SURFACE NOW</span>'
                    : '')
            +   riskPill
            +   ts
            + '</div>'
            + _renderAutoAdvanceBreadcrumb(thread)
            + '</div>'
        );
    }

    // Relative-time helper. ISO timestamp → "just now" / "5m ago" /
    // "2h ago" / "3d ago". Intentionally simple — fine for the
    // single-user, low-volume context.
    function _relativeTime(iso) {
        if (!iso) return '';
        try {
            const t = new Date(iso).getTime();
            if (Number.isNaN(t)) return '';
            const delta = Math.max(0, (Date.now() - t) / 1000);
            if (delta < 30) return 'just now';
            if (delta < 60) return Math.floor(delta) + 's ago';
            if (delta < 3600) return Math.floor(delta / 60) + 'm ago';
            if (delta < 86400) return Math.floor(delta / 3600) + 'h ago';
            return Math.floor(delta / 86400) + 'd ago';
        } catch (e) { return ''; }
    }

    // Auto-advance breadcrumb — surfaces the autonomy resolver's
    // recent decisions so the user can see what the agent decided
    // on its own. "Agent auto-advanced through Intent (92%) +
    // Context (85%) → here." Helps build trust and lets the user
    // verify the agent's reasoning at a glance.
    function _renderAutoAdvanceBreadcrumb(thread) {
        const trail = thread.auto_advance_trail || [];
        // Only show advances (not the surfaced-not-advanced
        // decisions), since the surfaced ones are why the user is
        // looking at this card in the first place.
        const advances = trail.filter(d => d.advance);
        if (advances.length === 0) return '';
        const parts = advances.map(d => {
            const tgt = (d.target || '').charAt(0).toUpperCase()
                       + (d.target || '').slice(1);
            const conf = d.confidence != null
                ? ' (' + Math.round(d.confidence * 100) + '%)'
                : '';
            return _esc(tgt + conf);
        });
        return (
            '<div class="threads-auto-advance" '
            +   'title="The agent auto-advanced past these stages '
            +     'because confidence was sufficient and the policy '
            +     'allowed it.">'
            + _icon("zap") + ' Agent auto-advanced: '
            + parts.join(' &middot; ')
            + '</div>'
        );
    }

    function _renderIntentSection(thread, s) {
        // 2026-05-03 PM: per user feedback the intent now follows the
        // same inline-buttons pattern as context items and actions.
        // The earlier UX.md §5.2 stance ("Intent: NO X-flag, only
        // editable") was overruled — users WILL want to flag a wrong
        // intent inference, and the flag-then-edit-then-Accept loop
        // mirrors how they handle bad context/action proposals.
        const text = (thread.intent && thread.intent.text) || "(no intent inferred)";
        const editedText = s.edited.intent !== undefined
            ? s.edited.intent
            : text;
        const conf = thread.intent && thread.intent.confidence;
        // Use the synthetic id "intent" for flag tracking. The Accept
        // disabler in _renderFooter checks ``s.flagged.size > 0``, so
        // a flagged intent blocks Accept just like a flagged context
        // item or action.
        const flagged = s.flagged.has("intent");
        const tid = _esc(thread.thread_id);
        return (
            '<div class="threads-section">'
            + '<div class="threads-section-label">Intent'
            +   _confidenceBadge(conf)
            + '</div>'
            + '<div class="threads-item threads-intent-item'
            +   (flagged ? ' threads-flagged' : '') + '">'
            +   '<div class="threads-item-label threads-intent">'
            +     _esc(editedText)
            +   '</div>'
            +   '<div class="threads-item-actions">'
            +     _flagBtn(thread.thread_id, "intent", flagged)
            +     '<button class="threads-edit-btn" '
            +       'title="Edit intent" '
            +       wbActAttrs('threadCardFocusIntent', {threadId: thread.thread_id}) + '>'
            +       _icon("edit")
            +     '</button>'
            +   '</div>'
            + '</div>'
            + '</div>'
        );
    }

    // Confidence badge — small inline chip showing the agent's
    // self-reported confidence. Color-coded: green ≥0.8, yellow
    // 0.5-0.8, red <0.5. Helps the user calibrate "should I trust
    // this guess?" at a glance.
    function _confidenceBadge(conf) {
        if (conf == null) return '';
        const pct = Math.round(conf * 100);
        let cls = "low";
        if (conf >= 0.8) cls = "high";
        else if (conf >= 0.5) cls = "medium";
        return ' <span class="threads-confidence ' + cls + '" '
             + 'title="Agent self-reported confidence">'
             + pct + '%</span>';
    }

    function _renderContextSection(thread, s) {
        const items = thread.context_items || [];
        const ctxConf = thread.context && thread.context.confidence;
        if (items.length === 0) {
            return (
                '<div class="threads-section">'
                + '<div class="threads-section-label">Context (none inferred)'
                +   _confidenceBadge(ctxConf)
                + '</div>'
                + '</div>'
            );
        }
        let html = '<div class="threads-section">';
        html += '<div class="threads-section-label">Context ('
              + items.length + ')'
              + _confidenceBadge(ctxConf)
              + '</div>';
        html += '<ul class="threads-list">';
        for (const ci of items) {
            const flagged = s.flagged.has(ci.id);
            const tid = _esc(thread.thread_id);
            const cid = _esc(ci.id);
            // The whole ``<li>`` is the click target — clicking the
            // card opens the right-pane inspector. Inner action
            // buttons (flag / chevron) call event.stopPropagation so
            // they don't double-fire the inspector open. The chevron
            // is kept for visual affordance but is now redundant
            // with the card click.
            html += '<li class="threads-item threads-item-clickable'
                  + (flagged ? ' threads-flagged' : '') + '" '
                  + 'role="button" tabindex="0" '
                  + 'title="Click to expand for full context-item details" '
                  + wbActAttrs('threadCardFocusContext', {threadId: thread.thread_id, contextId: ci.id}) + '>';
            html += '<div class="threads-item-label">'
                  + _esc(ci.label || ci.id) + '</div>';
            html += '<div class="threads-item-source">'
                  + _esc(ci.source || "") + (ci.type ? " · " + _esc(ci.type) : "")
                  + '</div>';
            html += '<div class="threads-item-actions" '
                  + 'data-on-click="wbNoop">';
            // _flagBtn already stops propagation in its onclick;
            // wrapping the actions div is belt-and-braces so any
            // future inline action button doesn't accidentally
            // double-fire the card click.
            html += _flagBtn(thread.thread_id, ci.id, flagged);
            // Chevron stays as a visual hint that the card opens
            // an inspector. It used to be the only click target;
            // now it's redundant but inexpensive.
            html += '<span class="threads-expand-hint" aria-hidden="true">'
                  + _icon("chevron-right") + '</span>';
            html += '</div>';
            html += '</li>';
        }
        html += '</ul>';
        html += '</div>';
        return html;
    }

    function _renderActionsSection(thread, s) {
        const actions = thread.actions || [];
        if (actions.length === 0) {
            return (
                '<div class="threads-section">'
                + '<div class="threads-section-label">Actions (none inferred)</div>'
                + '</div>'
            );
        }
        // User-feedback (2026-05-03 afternoon): once the action has
        // been performed (FSM past the awaiting_confirmation gate),
        // edit/flag affordances are misleading — the action already
        // ran. Suppress the per-action action-row in post-execution
        // states. The action label, plan, risk metadata, and rationale
        // still render as a historical record.
        //
        // 2026-05-09 update (singular pattern): when actions are HOISTED
        // from sub-threads onto a `parent_relationship='singular'`
        // umbrella's card (see backend `render.py:_per_action_state_from_fsm`),
        // each action carries its own `settled` flag derived from its
        // host child's fsm_state. The thread-level `actionPerformed`
        // gate over-hides in this case (parent stays in MONITORING
        // while children settle independently) — use per-action
        // `a.settled` instead. For non-singular threads, fall back to
        // the legacy thread-level gate so single-action threads keep
        // their existing behaviour.
        const POST_ACTION_STATES = new Set([
            'executing', 'monitoring', 'cleaning_up',
            'done', 'dismissed', 'handed_off',
            'done_cleanup_successful', 'done_cleanup_unsuccessful',
        ]);
        const threadActionPerformed = POST_ACTION_STATES.has(thread.fsm_state);
        let html = '<div class="threads-section">';
        html += '<div class="threads-section-label">Actions ('
              + actions.length + ')</div>';
        html += '<ul class="threads-list">';
        for (const a of actions) {
            const flagged = s.flagged.has(a.id);
            const blocked = !!a.context_blocked;
            // Singular-pattern hoisted actions carry `a.settled`. Fall
            // back to the thread-level gate when `a.settled` is undefined.
            const actionPerformed = (typeof a.settled === 'boolean')
                ? a.settled
                : threadActionPerformed;
            // Singular-pattern hoisted actions also carry
            // `host_thread_id` pointing at the child Thread that hosts
            // the action's FSM/event-log. When present, make the WHOLE
            // <li> clickable to navigate to the child (full Approve /
            // Edit / Reject / Redirect surface) — same pattern as the
            // context-items list above. Inner buttons stopPropagation.
            const hostId = a.host_thread_id;
            const clickable = !!hostId;
            const tidEsc = _esc(thread.thread_id);
            const hostEsc = hostId ? _esc(hostId) : '';
            html += '<li class="threads-item'
                  + (clickable ? ' threads-item-clickable' : '')
                  + (flagged ? ' threads-flagged' : '')
                  + (blocked ? ' threads-ctx-blocked' : '')
                  + (actionPerformed ? ' threads-action-settled' : '') + '"'
                  + (clickable
                      ? (' role="button" tabindex="0" '
                          + 'title="Click to open this action\'s thread '
                          + 'for full Approve / Edit / Redirect / Reject" '
                          + wbActAttrs('threadsPushPathTarget', {targetId: hostId}))
                      : '')
                  + '>';
            // Action label: kind icon + name + small kind chip
            // (so the user sees both "Research..." and that it's
            // an improvised plan, not a Standard Action).
            const kindChip = a.kind
                ? ' <span class="threads-kind-chip ' + _esc(a.kind) + '">'
                  + _esc(a.kind) + '</span>'
                : '';
            // Per-action status badge for hoisted actions on singular
            // umbrellas: shows ✓ done / ✗ rejected / ! failed inline so
            // the user can see at a glance which of N proposals on this
            // thread are still pending vs already settled.
            let statusBadge = '';
            if (a.state === 'done') {
                statusBadge = ' <span class="threads-action-status-badge done" '
                            + 'title="Action completed">✓ done</span>';
            } else if (a.state === 'rejected') {
                statusBadge = ' <span class="threads-action-status-badge rejected" '
                            + 'title="Action dismissed">✗ rejected</span>';
            } else if (a.state === 'failed') {
                statusBadge = ' <span class="threads-action-status-badge failed" '
                            + 'title="Action failed during execution">! failed</span>';
            } else if (a.state === 'executing') {
                statusBadge = ' <span class="threads-action-status-badge executing" '
                            + 'title="Action executing">⟳ running</span>';
            }
            html += '<div class="threads-item-label">'
                  + _kindIcon(a.kind, a.name) + ' ' + _esc(a.name || a.id)
                  + kindChip
                  + statusBadge
                  + _confidenceBadge(a.confidence)
                  + '</div>';
            const summary = a.plan_summary || _summariseParams(a.parameters);
            if (summary) {
                html += '<div class="threads-item-summary">'
                      + _esc(summary) + '</div>';
            }
            // Risk metadata disclosure — declared by the agent for
            // improvised actions (and inherited from the Standard
            // Action template's intrinsic_amplifiers when standard).
            // Render inline so the user has the full risk picture
            // before clicking Accept.
            const riskBits = [];
            if (a.irreversibility) riskBits.push('irreversibility=' + a.irreversibility);
            if (a.regret_potential) riskBits.push('regret=' + a.regret_potential);
            if (a.risk_amplifier === true) riskBits.push('risk-amplifier');
            if (riskBits.length > 0) {
                const cls = (a.irreversibility === "high"
                             || a.regret_potential === "high"
                             || a.risk_amplifier === true)
                    ? 'threads-risk-row threads-risk-high'
                    : 'threads-risk-row';
                html += '<div class="' + cls + '" '
                      + 'title="Risk metadata declared by the agent">'
                      + _icon("alert-circle") + ' '
                      + _esc(riskBits.join(' · '))
                      + '</div>';
            }
            // Show the agent's rationale inline if present (helpful
            // for improvised actions the user might want to redirect).
            if (a.rationale) {
                html += '<div class="threads-item-rationale">'
                      + '<em>Why:</em> ' + _esc(a.rationale)
                      + '</div>';
            }
            // Suggestion-only: show what the agent is blocked on.
            if (a.kind === "suggestion" && a.blocked_on) {
                html += '<div class="threads-item-blocked">'
                      + '<em>Blocked on:</em> ' + _esc(a.blocked_on)
                      + '</div>';
            }
            // Action-context status indicator (Stage 4.11). Each
            // required context shows availability inline with a
            // ✓ / ⊘ glyph + reason on hover.
            const statuses = a.context_statuses || [];
            if (statuses.length > 0) {
                html += '<div class="threads-item-contexts">Requires: '
                      + statuses.map(s => {
                          const cls = s.available
                              ? 'threads-ctx-ok'
                              : (s.kind === 'user_only'
                                    ? 'threads-ctx-user'
                                    : 'threads-ctx-down');
                          const glyph = s.available ? '✓'
                                       : (s.kind === 'user_only' ? '◐' : '⊘');
                          const title = s.reason ? ' title="' + _esc(s.reason) + '"' : '';
                          return '<span class="threads-ctx ' + cls + '"'
                                 + title + '>'
                                 + glyph + ' ' + _esc(s.token)
                                 + '</span>';
                      }).join(' ')
                      + '</div>';
            } else if (Array.isArray(a.required_contexts) && a.required_contexts.length) {
                // Fallback when status lookup wasn't run
                html += '<div class="threads-item-contexts">Requires: '
                      + a.required_contexts.map(_esc).join(', ')
                      + '</div>';
            }
            if (!actionPerformed) {
                // Wrap inner action buttons in a div with
                // stopPropagation so clicking Edit / Flag / Redirect
                // doesn't double-fire the card-level navigate (when
                // this is a singular-hoisted action with host_thread_id,
                // the <li> itself is the click target). Inner _flagBtn
                // already stopPropagates; the wrapper is belt-and-
                // suspenders for the edit + redirect buttons.
                html += '<div class="threads-item-actions"'
                      + (clickable ? ' data-on-click="wbNoop"' : '')
                      + '>';
                html += _flagBtn(thread.thread_id, a.id, flagged);
                html += '<button class="threads-edit-btn" '
                      + 'title="Edit action" '
                      + wbActAttrs('threadCardFocusAction', {threadId: thread.thread_id, actionId: a.id}) + '>'
                      + _icon("edit") + '</button>';
                // Per-action Redirect. Hoisted singular actions carry
                // host_thread_id, so the redirect targets the child
                // Thread's FSM (where the action_inferred event lives).
                // The handler prompts for steering feedback and POSTs
                // to /api/threads/<host>/redirect_action, which re-runs
                // ONLY action-layer inference (no walk back through
                // intent / context).
                if (clickable) {
                    html += '<button class="threads-redirect-btn" '
                          + 'title="Redirect: ask the LLM to try this action '
                          + 'again with your feedback" '
                          + wbActAttrs('threadCardRedirectHostAction', {hostId: hostId}) + '>'
                          + _icon("refresh-cw") + '</button>';
                }
                html += '</div>';
            }
            // Chevron hint that the card is clickable — same affordance
            // as the context-items list. Only renders when the action
            // is singular-hoisted (i.e. host_thread_id present); for
            // top-level threads the action is rendered in-place and
            // doesn't navigate.
            if (clickable) {
                html += '<span class="threads-expand-hint" aria-hidden="true">'
                      + _icon("chevron-right") + '</span>';
            }
            html += '</li>';
        }
        html += '</ul>';
        html += '</div>';
        return html;
    }

    function _renderNamespaceTagsSection(thread, s) {
        const tags = thread.namespace_tags || [];
        return (
            '<div class="threads-section">'
            + '<div class="threads-section-label">Namespace tags</div>'
            + '<div class="threads-tags">'
            +   (tags.length > 0
                    ? tags.map(t => '<span class="threads-tag">'
                                  + _esc(t) + '</span>').join('')
                    : '<em class="threads-empty">(none)</em>')
            + '</div>'
            + _renderTimelineLink(thread)
            + '</div>'
        );
    }

    // Wave C: timeline / event-log link. Surfaces the full event
    // history for the thread in a modal. UX.md §11 says inspect=
    // event IDs are URL-routable; this is a quick "see everything"
    // affordance that opens the inspector.
    function _renderTimelineLink(thread) {
        return (
            '<div class="threads-timeline-link-row">'
            + '<a href="#" '
            +   'class="threads-timeline-link" '
            +   'title="Open the full event log for this thread" '
            +   'data-on-click="threadCardOpenEvlog">'
            +   _icon("list") + ' View timeline'
            + '</a>'
            + '</div>'
        );
    }

    function _renderSubThreadsLink(thread) {
        // User-feedback fix #2 (2026-05-03 morning): hide the
        // sub-thread section entirely when count=0. Wave F's
        // "(none)" version was unnecessary visual noise.
        // Wave I: aggregated state badges per UX.md §8.1
        // ("5 done · 4 awaiting consent · 2 awaiting clarification").
        // 2026-05-03 followup: prior version pushed the parent's
        // own ID, creating a self-referencing breadcrumb. Replaced
        // with inline sub-thread mini-cards (UX.md §3.2 + §8.4 —
        // "sub-thread list shows children of that Thread").
        // 2026-05-04: group-parents render the multi-column drag/
        // drop grid here in place of the flat list — see
        // tabs/threads/group.py:renderGroupSubThreads. The
        // section header + aggregated state badges stay; the body
        // is the only thing that swaps.
        const isGroup = thread.parent_relationship === 'group'
            && typeof window.renderGroupSubThreads === 'function';
        const isSingular = thread.parent_relationship === 'singular';
        const n = thread.sub_thread_count || 0;
        // Group-parents always render the section: sibling columns
        // may have content even when this group has zero children
        // of its own. Non-group parents hide the section at count=0.
        // Singular-pattern umbrellas (inline-capture multi-record):
        // children's actions have been HOISTED onto the parent's
        // Actions section by the backend render. Showing them again
        // here as Sub-threads is redundant — drop the section
        // entirely so the umbrella card reads as ONE thread with N
        // actions. See `threads/grouping` (singular pattern).
        if (isSingular) return '';
        if (n === 0 && !isGroup) return '';
        const counts = thread.sub_thread_state_counts || {};
        const badges = _renderStateBadges(counts);
        let html = '<div class="threads-section">'
                 + '<div class="threads-section-label">'
                 +   'Sub-threads (' + n + ')'
                 + '</div>';
        if (badges) {
            html += '<div class="threads-state-badges">'
                  + badges + '</div>';
        }
        if (isGroup) {
            html += window.renderGroupSubThreads(thread);
            html += '</div>';
            return html;
        }
        // Lazy-load the sub-thread list. The first render shows a
        // placeholder; we fetch /api/threads/<id>/sub asynchronously
        // and inject the cards via DOM insertion. We cache results
        // on window._subThreadCache keyed by parent thread_id so
        // re-renders don't re-fetch.
        const cached = (window._subThreadCache || {})[thread.thread_id];
        if (cached) {
            html += _renderSubThreadCards(cached);
        } else {
            html += '<div class="threads-subthread-loading" '
                  + 'data-parent-id="' + _esc(thread.thread_id) + '">'
                  + 'Loading sub-threads...</div>';
            // Trigger async fetch on next tick
            const tid = thread.thread_id;
            setTimeout(() => _loadSubThreads(tid), 0);
        }
        html += '</div>';
        return html;
    }

    function _loadSubThreads(parentId) {
        if (!window._subThreadCache) window._subThreadCache = {};
        if (window._subThreadCache[parentId]) return;
        fetch('/api/threads/' + encodeURIComponent(parentId) + '/sub')
            .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
            .then(data => {
                window._subThreadCache[parentId] = data.threads || [];
                if (typeof window._renderActiveThread === 'function') {
                    window._renderActiveThread();
                }
            })
            .catch(err => {
                console.warn('[threads] sub-thread fetch failed:', err);
                window._subThreadCache[parentId] = [];
                if (typeof window._renderActiveThread === 'function') {
                    window._renderActiveThread();
                }
            });
    }

    function _renderSubThreadCards(subThreads) {
        if (!subThreads || subThreads.length === 0) {
            return '<p class="threads-empty">'
                 + '<em>(no sub-threads loaded)</em></p>';
        }
        let html = '<ul class="threads-subthread-list">';
        for (const sub of subThreads) {
            const stateLabel = _friendlyState(sub.fsm_state);
            const intent = (sub.intent && sub.intent.text)
                            || sub.title || sub.thread_id;
            const cls = 'threads-subthread-card '
                + (sub.display_mode === 'mid_process'
                    ? 'threads-mid-process' : '')
                + (sub.display_mode === 'terminal'
                    ? 'threads-terminal' : '');
            html += '<li class="' + cls + '" '
                  + wbActAttrs('threadsPushPathTarget', {targetId: sub.thread_id}) + '>'
                  + '<div class="threads-subthread-meta">'
                  +   '<span class="threads-subthread-state">'
                  +     _esc(stateLabel) + '</span>'
                  +   (sub.risk_highlight
                        ? '<span class="threads-toplist-risk-dot '
                            + _esc(sub.risk_highlight) + '"></span>'
                        : '')
                  + '</div>'
                  + '<div class="threads-subthread-title">'
                  +   _esc(sub.title || sub.thread_id) + '</div>'
                  + (intent && intent !== sub.title
                        ? '<div class="threads-subthread-intent">'
                          + _esc(intent.length > 140
                              ? intent.slice(0, 137) + '...' : intent)
                          + '</div>'
                        : '')
                  + _renderSubThreadActionsPreview(sub)
                  + '</li>';
        }
        html += '</ul>';
        return html;
    }

    // Inline preview of a sub-thread's proposed actions on the mini-card.
    // The user sees what is about to happen without entering each thread,
    // and can go straight to editing the proposed action with a single
    // click. The action-edit pencil opens
    // the sub-thread WITH its right-pane editor already focused on the
    // action, so the "edit before entering" affordance is one click away
    // even if we don't render a full action editor inline.
    function _renderSubThreadActionsPreview(sub) {
        const actions = sub.actions || [];
        if (actions.length === 0) return '';
        let html = '<div class="threads-subthread-actions">';
        for (const a of actions) {
            const name = a.name || a.id || "(unnamed)";
            const kind = a.kind || "";
            html += '<div class="threads-subthread-action">'
                  +   _kindIcon(a.kind, a.name) + ' '
                  +   '<span class="threads-subthread-action-name">'
                  +     _esc(name) + '</span>'
                  +   (kind
                        ? ' <span class="threads-kind-chip ' + _esc(kind) + '">'
                          + _esc(kind) + '</span>'
                        : '')
                  +   '<button class="threads-subthread-edit-btn" '
                  +     'title="Edit this proposed action (opens the right-pane editor)" '
                  +     'aria-label="Edit proposed action" '
                  +     wbActAttrs('threadsOpenSubThreadActionBtn', {subThreadId: sub.thread_id, actionId: a.id}) + '>'
                  +     _icon("edit")
                  +   '</button>'
                  + '</div>';
        }
        html += '</div>';
        return html;
    }

    // Wave I: tiny inline state-badge row showing how many
    // sub-threads are in each state. Sorted by category so the
    // user-actionable counts come first ("5 awaiting consent")
    // and the agent-internal/terminal counts come later
    // ("3 done").
    function _renderStateBadges(counts) {
        if (!counts || Object.keys(counts).length === 0) return '';
        // Pretty labels — keep them short for compact display.
        const PRETTY = {
            "awaiting_intent_confirmation": "intent",
            "awaiting_intent_clarification": "intent clarif",
            "awaiting_context_confirmation": "context",
            "awaiting_context_clarification": "context clarif",
            "awaiting_action_clarification": "action clarif",
            "awaiting_confirmation": "consent",
            "awaiting_review": "review",
            "awaiting_redirect": "redirect",
            "awaiting_inference": "queued",
            "inferring_intent": "inferring",
            "inferring_context": "inferring",
            "inferring_action": "inferring",
            "executing": "executing",
            "monitoring": "monitoring",
            "cleaning_up": "cleaning",
            "done_cleanup_unsuccessful": "cleanup failed",
            "done_cleanup_successful": "done",
            "done": "done",
            "dismissed": "dismissed",
            "handed_off": "handed off",
            "proposed": "proposed",
        };
        // Order categories: actionable first, then in-flight, then terminal
        const ORDER = [
            "awaiting_confirmation",
            "awaiting_intent_confirmation",
            "awaiting_context_confirmation",
            "awaiting_intent_clarification",
            "awaiting_context_clarification",
            "awaiting_action_clarification",
            "awaiting_review",
            "awaiting_redirect",
            "done_cleanup_unsuccessful",
            "awaiting_inference",
            "inferring_intent",
            "inferring_context",
            "inferring_action",
            "executing",
            "monitoring",
            "cleaning_up",
            "proposed",
            "done",
            "done_cleanup_successful",
            "dismissed",
            "handed_off",
        ];
        const seen = new Set();
        const parts = [];
        for (const k of ORDER) {
            if (k in counts && counts[k] > 0) {
                seen.add(k);
                parts.push('<span class="threads-state-badge" '
                    + 'title="' + _esc(k) + '">'
                    + counts[k] + ' ' + _esc(PRETTY[k] || k)
                    + '</span>');
            }
        }
        // Any unrecognized states still appear at the end.
        for (const k of Object.keys(counts)) {
            if (!seen.has(k) && counts[k] > 0) {
                parts.push('<span class="threads-state-badge">'
                    + counts[k] + ' ' + _esc(k) + '</span>');
            }
        }
        return parts.join(' &middot; ');
    }

    function _renderRightPane(thread, s) {
        const focused = s.focusedId;
        if (!focused) {
            return (
                '<div class="threads-right-empty">'
                + '<p>Click any item or action on the left to edit it here.</p>'
                + '</div>'
            );
        }
        if (focused === "intent") {
            const edited = s.edited.intent !== undefined
                ? s.edited.intent
                : ((thread.intent && thread.intent.text) || "");
            const tidJs = _esc(thread.thread_id);
            return (
                '<div class="threads-right-editor">'
                + '<h4>Edit intent</h4>'
                + '<p class="threads-editor-hint">'
                +   '<kbd>Enter</kbd> to confirm &middot; '
                +   '<kbd>Shift</kbd>+<kbd>Enter</kbd> for newline &middot; '
                +   '<kbd>Esc</kbd> to discard'
                + '</p>'
                + '<textarea class="threads-textarea" rows="6" autofocus '
                +   wbActAttrs('threadCardEditIntentInput', {threadId: thread.thread_id}, 'input') + ' '
                +   wbActAttrs('threadCardIntentEditorKeydown', {threadId: thread.thread_id}, 'keydown')
                + '>' + _esc(edited) + '</textarea>'
                + '<div class="threads-editor-actions">'
                +   '<button class="threads-editor-btn threads-editor-btn-cancel" '
                +     'title="Discard edit (Esc)" '
                +     wbActAttrs('threadCardDiscardIntentEditBtn', {threadId: thread.thread_id}) + '>'
                +     '<span class="threads-editor-icon">&times;</span>'
                +     '<span class="threads-editor-label">Discard</span>'
                +   '</button>'
                +   '<button class="threads-editor-btn threads-editor-btn-confirm" '
                +     'title="Confirm edit (Enter). The edit is staged; click Accept on '
                +       'the main card to commit it to the thread." '
                +     wbActAttrs('threadCardConfirmIntentEditBtn', {threadId: thread.thread_id}) + '>'
                +     '<span class="threads-editor-icon">&#x21A9;</span>'
                +     '<span class="threads-editor-label">Confirm</span>'
                +   '</button>'
                + '</div>'
                + '</div>'
            );
        }
        // Context-item or action editor (Stage 4.6 — per-action
        // specialized renderers via window._actionRenderers).
        const target = _findById(thread, focused);
        if (!target) {
            return (
                '<div class="threads-right-empty">'
                + '<p>(focused element ' + _esc(focused) + ' not found)</p>'
                + '</div>'
            );
        }
        // Actions go through the action-renderer registry. The
        // discriminator is ``_kind`` to avoid colliding with the
        // action's own ``kind`` field (see _findById).
        if (target._kind === "action"
            && typeof window.renderActionInRightPane === "function") {
            return (
                '<div class="threads-right-editor">'
                + window.renderActionInRightPane(thread, target)
                + '</div>'
            );
        }
        // Context-item inspector — pretty-printed fields with
        // human labels rather than raw JSON. The payload is shown
        // as a key/value table; long values truncated with click-
        // to-expand. Much friendlier than the prior JSON dump.
        return (
            '<div class="threads-right-editor">'
            + '<h4>Context item &middot; <code>' + _esc(focused) + '</code></h4>'
            + _renderContextItemInspector(target)
            + '</div>'
        );
    }

    function _renderContextItemInspector(item) {
        let html = '<table class="threads-ci-table">';
        const rows = [
            ["Label", item.label],
            ["Source", item.source],
            ["Type", item.type],
        ];
        for (const [k, v] of rows) {
            if (v === undefined || v === null || v === "") continue;
            html += '<tr><th>' + _esc(k) + '</th>'
                  + '<td>' + _esc(v) + '</td></tr>';
        }
        const payload = item.payload || {};
        const payloadKeys = Object.keys(payload);
        if (payloadKeys.length > 0) {
            html += '<tr><th colspan="2" class="threads-ci-payload-header">'
                  + 'Payload</th></tr>';
            for (const k of payloadKeys) {
                const raw = payload[k];
                if (raw === undefined || raw === null || raw === "") continue;
                html += '<tr><th>' + _esc(k) + '</th>'
                      + '<td>'
                      + _renderPayloadValue(k, raw)
                      + '</td></tr>';
            }
        }
        html += '</table>';
        return html;
    }

    function _renderPayloadValue(key, raw) {
        // Multi-line strings (raw_text, body, etc.) need preserved
        // newlines + a scrollable box so the inspector doesn't blow
        // up vertically. Anything ≤ 1 line of plain text stays as the
        // pre-existing inline ``<code>`` shape.
        if (typeof raw === "object") {
            const json = JSON.stringify(raw, null, 2);
            return _renderScrollableBlock(json, /*language*/ "json");
        }
        const s = String(raw);
        // URLs render as a clickable link.
        if (typeof raw === "string" && /^https?:\/\//.test(s)) {
            const href = _esc(s);
            const display = s.length > 80 ? s.slice(0, 77) + "…" : s;
            return '<a href="' + href + '" target="_blank" rel="noopener" '
                 + 'class="threads-ci-payload-link">'
                 + _esc(display) + '</a>';
        }
        const isMultiLine = s.includes("\n");
        if (isMultiLine || s.length > 200) {
            return _renderScrollableBlock(s, /*language*/ "text");
        }
        return '<code>' + _esc(s) + '</code>';
    }

    function _renderScrollableBlock(text, language) {
        // ``<pre>`` preserves whitespace + newlines verbatim. We give
        // it a fixed max-height so it scrolls vertically once content
        // exceeds ~6 lines (CSS controls the exact threshold).
        return '<pre class="threads-ci-payload-block" '
             + 'data-lang="' + _esc(language || "text") + '">'
             + _esc(text)
             + '</pre>';
    }

    function _renderFooter(thread, hasFlags) {
        // User-feedback fix #6 (2026-05-03 morning): unified icon
        // row instead of left/right cluster. All buttons are
        // icon-only with tooltips on hover; color-coded for
        // scannability (destructive red, neutral muted, primary
        // green, redirect orange).
        // Per UX.md §4.1 + §5.4 (footer affordances).
        const cleanupShown = !!thread.can_clean_up;
        const acceptDisabled = hasFlags;
        const acceptTitle = hasFlags
            ? "Resolve any flagged elements before accepting"
            : "Approve and commit";
        const tid = _esc(thread.thread_id);
        return (
            '<div class="threads-card-footer">'
            +   '<button class="threads-btn-icon threads-btn-destructive" '
            +     'title="Dismiss this thread" '
            +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'dismiss'}) + '>'
            +     _icon("trash") + '</button>'
            +   (cleanupShown
                    ? '<button class="threads-btn-icon threads-btn-neutral" '
                    +   'title="Clean up inciting event source" '
                    +   wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'cleanup'}) + '>'
                    +   _icon("broom") + '</button>'
                    : '')
            +   '<button class="threads-btn-icon threads-btn-neutral" '
            +     'title="Later — left-click defers 6h; right-click for options" '
            +     wbActAttrs('threadCommitActionLater', {threadId: thread.thread_id}) + ' '
            +     'data-on-contextmenu="threadsLaterPopup">'
            +     _icon("clock") + '</button>'
            +   '<button class="threads-btn-icon threads-btn-redirect" '
            +     'title="Redirect — give the agent feedback and re-infer" '
            +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'redirect'}) + '>'
            +     _icon("corner-up-left") + '</button>'
            +   '<button class="threads-btn-icon threads-btn-accept" '
            +     (acceptDisabled ? 'disabled ' : '')
            +     'title="' + _esc(acceptTitle) + '" '
            +     (acceptDisabled
                    ? ''
                    : wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'accept'}))
            +     '>' + _icon("check") + '</button>'
            + '</div>'
        );
    }

    // ----- Clarification card (UX.md §4.4.2) ---------------------------

    // Per-thread clarification input state
    if (!window._clarifyInput) window._clarifyInput = {};

    window.threadClarifyInput = function (threadId, value) {
        window._clarifyInput[threadId] = value;
    };

    function _renderClarification(thread) {
        const tid = _esc(thread.thread_id);
        const promptText = _clarificationPromptFor(thread.fsm_state);
        const stored = window._clarifyInput[thread.thread_id] || "";
        let html = '<div class="threads-card threads-kind-clarification" '
                 + 'data-thread-id="' + tid + '">';
        html += _renderHeader(thread);
        html += '<div class="threads-card-body threads-clarify-body">';
        html += '<p class="threads-clarify-prompt">' + _esc(promptText)
              + '</p>';
        html += '<textarea class="threads-clarify-textarea" rows="6" '
              + 'placeholder="Tell the agent what you mean..." '
              + wbActAttrs('threadClarifyInputChange', {threadId: thread.thread_id}, 'input') + '>'
              + _esc(stored) + '</textarea>';
        if (thread.context_items && thread.context_items.length > 0) {
            html += '<div class="threads-clarify-context">';
            html += '<div class="threads-section-label">Context the agent has so far</div>';
            html += '<ul class="threads-list">';
            for (const ci of thread.context_items) {
                html += '<li class="threads-item">';
                html += '<div class="threads-item-label">'
                      + _esc(ci.label || ci.id) + '</div>';
                html += '<div class="threads-item-source">'
                      + _esc(ci.source || "") + '</div>';
                html += '</li>';
            }
            html += '</ul></div>';
        }
        html += '</div>';
        // Footer: Trash / Broom / Later / (Redirect skipped — clarif IS the redirect target) / Accept
        html += _renderClarificationFooter(thread);
        html += '</div>';
        return html;
    }

    function _clarificationPromptFor(state) {
        if (state === "awaiting_intent_clarification")
            return "The agent couldn't infer your intent. What are you trying to accomplish?";
        if (state === "awaiting_context_clarification")
            return "The agent couldn't infer the relevant context. What should it look at?";
        if (state === "awaiting_action_clarification")
            return "The agent has no action candidate. What should it do (or pick from the catalog)?";
        return "Please clarify.";
    }

    function _renderClarificationFooter(thread) {
        // User-feedback fix #6: unified icon-only footer.
        const tid = _esc(thread.thread_id);
        const cleanupShown = !!thread.can_clean_up;
        return (
            '<div class="threads-card-footer">'
            +   '<button class="threads-btn-icon threads-btn-destructive" '
            +     'title="Dismiss this thread" '
            +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'dismiss'}) + '>'
            +     _icon("trash") + '</button>'
            +   (cleanupShown
                    ? '<button class="threads-btn-icon threads-btn-neutral" '
                    +   'title="Clean up inciting event source" '
                    +   wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'cleanup'}) + '>'
                    +   _icon("broom") + '</button>'
                    : '')
            +   '<button class="threads-btn-icon threads-btn-neutral" '
            +     'title="Later — left-click defers 6h; right-click for options" '
            +     wbActAttrs('threadCommitActionLater', {threadId: thread.thread_id}) + ' '
            +     'data-on-contextmenu="threadsLaterPopup">'
            +     _icon("clock") + '</button>'
            +   '<button class="threads-btn-icon threads-btn-accept" '
            +     'title="Submit clarification" '
            +     wbActAttrs('threadCommitActionClarifyAccept', {threadId: thread.thread_id}) + '>'
            +     _icon("check") + '</button>'
            + '</div>'
        );
    }

    // ----- Review card (UX.md §4.4.3) ----------------------------------

    function _renderReview(thread) {
        const tid = _esc(thread.thread_id);
        const ctx = thread.review_context || {};
        let html = '<div class="threads-card threads-kind-review" '
                 + 'data-thread-id="' + tid + '">';
        html += _renderHeader(thread);
        html += '<div class="threads-card-body threads-review-body">';
        html += '<div class="threads-review-status">'
              + 'Result: ' + _esc(ctx.status || 'completed')
              + '</div>';
        if (ctx.summary) {
            html += '<div class="threads-review-summary">'
                  + _esc(ctx.summary) + '</div>';
        }
        if (ctx.output) {
            html += '<pre class="threads-review-output">'
                  + _esc(typeof ctx.output === "string"
                        ? ctx.output
                        : JSON.stringify(ctx.output, null, 2))
                  + '</pre>';
        }
        if (ctx.run_id) {
            html += '<p class="threads-review-run">Run ID: <code>'
                  + _esc(ctx.run_id) + '</code></p>';
        }
        html += '</div>';
        // UX.md §4.4.3: Review cards have NO Dismiss (action's done).
        // Footer: Later / Redirect / Mark done — unified icon row
        // (user-feedback fix #6).
        html += '<div class="threads-card-footer">'
              +   '<button class="threads-btn-icon threads-btn-neutral" '
              +     'title="Later — left-click defers 6h; right-click for options" '
              +     wbActAttrs('threadCommitActionLater', {threadId: thread.thread_id}) + ' '
              +     'data-on-contextmenu="threadsLaterPopup">'
              +     _icon("clock") + '</button>'
              +   '<button class="threads-btn-icon threads-btn-redirect" '
              +     'title="Redirect — give the agent feedback and re-infer" '
              +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'redirect'}) + '>'
              +     _icon("corner-up-left") + '</button>'
              +   '<button class="threads-btn-icon threads-btn-accept" '
              +     'title="Mark done" '
              +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'accept'}) + '>'
              +     _icon("check") + '</button>'
              + '</div>';
        html += '</div>';
        return html;
    }

    // ----- Redirect card (UX.md §4.4.4) --------------------------------

    if (!window._redirectInput) window._redirectInput = {};

    window.threadRedirectInput = function (threadId, value) {
        window._redirectInput[threadId] = value;
    };

    function _renderRedirect(thread) {
        const tid = _esc(thread.thread_id);
        const fc = thread.failure_context || {};
        const stored = window._redirectInput[thread.thread_id] || "";
        let html = '<div class="threads-card threads-kind-redirect" '
                 + 'data-thread-id="' + tid + '">';
        html += _renderHeader(thread);
        html += '<div class="threads-card-body threads-redirect-body">';
        html += '<div class="threads-redirect-failure">'
              + '<strong>Execution failed.</strong>';
        if (fc.error) {
            html += ' Error: <code>' + _esc(fc.error) + '</code>';
        }
        if (fc.step) {
            html += ' Step: <code>' + _esc(fc.step) + '</code>';
        }
        html += '</div>';
        html += '<p class="threads-redirect-prompt">'
              + 'Tell the agent what to do now.</p>';
        html += '<textarea class="threads-redirect-textarea" rows="5" '
              + 'placeholder="Describe what went wrong / what to try..." '
              + wbActAttrs('threadRedirectInputChange', {threadId: thread.thread_id}, 'input') + '>'
              + _esc(stored) + '</textarea>';
        html += '</div>';
        // Footer: Trash / Broom / Later / Submit redirect — unified
        // icon row (user-feedback fix #6).
        const cleanupShown = !!thread.can_clean_up;
        html += '<div class="threads-card-footer">'
              +   '<button class="threads-btn-icon threads-btn-destructive" '
              +     'title="Dismiss this thread" '
              +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'dismiss'}) + '>'
              +     _icon("trash") + '</button>'
              +   (cleanupShown
                    ? '<button class="threads-btn-icon threads-btn-neutral" '
                    +   'title="Clean up inciting event source" '
                    +   wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'cleanup'}) + '>'
                    +   _icon("broom") + '</button>'
                    : '')
              +   '<button class="threads-btn-icon threads-btn-neutral" '
              +     'title="Later — left-click defers 6h; right-click for options" '
              +     wbActAttrs('threadCommitActionLater', {threadId: thread.thread_id}) + ' '
              +     'data-on-contextmenu="threadsLaterPopup">'
              +     _icon("clock") + '</button>'
              +   '<button class="threads-btn-icon threads-btn-redirect" '
              +     'title="Submit redirect feedback" '
              +     wbActAttrs('threadCommitActionRedirectSubmit', {threadId: thread.thread_id}) + '>'
              +     _icon("corner-up-left") + '</button>'
              + '</div>';
        html += '</div>';
        return html;
    }

    // ----- Cleanup-failure card (UX.md §6.5) ---------------------------

    function _renderCleanupFailure(thread) {
        const tid = _esc(thread.thread_id);
        const cf = thread.cleanup_failure || {};
        let html = '<div class="threads-card threads-kind-cleanup-failure" '
                 + 'data-thread-id="' + tid + '">';
        html += _renderHeader(thread);
        html += '<div class="threads-card-body threads-cleanup-fail-body">';
        html += '<div class="threads-cleanup-fail-banner">'
              + _icon("alert-triangle")
              + ' <strong>Cleanup failed.</strong></div>';
        if (cf.detail) {
            html += '<p class="threads-cleanup-fail-detail">'
                  + _esc(cf.detail) + '</p>';
        }
        html += '<p class="threads-stage-note">'
              + 'You can retry the cleanup or accept the failure '
              + '(closes this Thread without further action).</p>';
        html += '</div>';
        // Footer: Trash / Later / Accept failure / Retry — unified
        // icon row (user-feedback fix #6).
        html += '<div class="threads-card-footer">'
              +   '<button class="threads-btn-icon threads-btn-destructive" '
              +     'title="Dismiss this thread" '
              +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'dismiss'}) + '>'
              +     _icon("trash") + '</button>'
              +   '<button class="threads-btn-icon threads-btn-neutral" '
              +     'title="Later — left-click defers 6h; right-click for options" '
              +     wbActAttrs('threadCommitActionLater', {threadId: thread.thread_id}) + ' '
              +     'data-on-contextmenu="threadsLaterPopup">'
              +     _icon("clock") + '</button>'
              +   '<button class="threads-btn-icon threads-btn-neutral" '
              +     'title="Accept the failure and close this thread" '
              +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'accept-cleanup-failure'}) + '>'
              +     _icon("check") + '</button>'
              +   '<button class="threads-btn-icon threads-btn-redirect" '
              +     'title="Retry cleanup" '
              +     wbActAttrs('threadCommitActionVerb', {threadId: thread.thread_id, verb: 'retry-cleanup'}) + '>'
              +     _icon("refresh-cw") + '</button>'
              + '</div>';
        html += '</div>';
        return html;
    }

    // ----- Helpers ------------------------------------------------------

    function _kindIcon(kind, name) {
        // Wave C: prefer an action-name-derived icon when we can
        // detect a known action shape (calendar event, email, task,
        // file, decompose). Falls back to kind-level icons.
        if (name) {
            const n = String(name).toLowerCase();
            if (/calendar|invite|schedule|meeting/.test(n)) return _icon("calendar");
            if (/email|mail|send_msg|send_email/.test(n)) return _icon("mail");
            if (/task|todo|create_task/.test(n)) return _icon("check-square");
            if (/file|note|reference/.test(n)) return _icon("file");
            if (/decompose|split|sub-thread/.test(n)) return _icon("git-branch");
        }
        if (kind === "standard") return _icon("check-circle");
        if (kind === "improvised") return _icon("zap");
        if (kind === "suggestion") return _icon("lightbulb");
        return _icon("box");
    }

    function _flagBtn(threadId, itemId, flagged) {
        return '<button class="threads-flag-btn'
             + (flagged ? ' threads-flag-on' : '') + '" '
             + 'title="' + (flagged ? 'Unflag' : 'Flag as wrong') + '" '
             + wbActAttrs('threadCardToggleFlagBtn', {threadId: threadId, itemId: itemId}) + '>'
             + (flagged ? _icon("x-square") : _icon("x"))
             + '</button>';
    }

    function _summariseParams(params) {
        if (!params || typeof params !== "object") return "";
        const keys = Object.keys(params);
        if (keys.length === 0) return "";
        // Surface the decision-critical values on the card itself (a note
        // target, a task id) rather than hiding them behind Edit. Show up
        // to three params, humanised, so the user can judge accept/reject
        // without opening the editor.
        const parts = [];
        for (const k of keys.slice(0, 3)) {
            const v = params[k];
            const text = (typeof v === "string" || typeof v === "number")
                ? String(v) : JSON.stringify(v);
            const val = text.length > 60 ? text.slice(0, 57) + "..." : text;
            parts.push(k.replace(/_/g, " ") + ": " + val);
        }
        return parts.join("  ·  ");
    }

    function _findById(thread, id) {
        // 2026-05-03 PM: use ``_kind`` (underscore-prefixed) as the
        // discriminator so it doesn't collide with the action's own
        // ``kind`` field (``"standard"`` / ``"improvised"`` /
        // ``"suggestion"`` / ``"clarification"``). The earlier
        // implementation did ``Object.assign({kind:"action"}, a)``,
        // and ``a.kind`` won the merge — so the right-pane dispatcher
        // saw e.g. ``"improvised"``, fell through the action branch,
        // and rendered the action as a "Context item" inspector.
        for (const ci of (thread.context_items || [])) {
            if (ci.id === id) return Object.assign({ _kind: "context" }, ci);
        }
        for (const a of (thread.actions || [])) {
            if (a.id === id) return Object.assign({ _kind: "action" }, a);
        }
        return null;
    }

    // Inline SVG icons. We call the full Lucide library
    // "Lucide-flavoured" — these are minimalist stand-ins. Stage 4.16
    // can swap in the real Lucide library if available at that point.
    function _icon(name) {
        const paths = {
            "check": '<polyline points="20 6 9 17 4 12"></polyline>',
            "x": '<line x1="18" y1="6" x2="6" y2="18"></line>'
                + '<line x1="6" y1="6" x2="18" y2="18"></line>',
            "x-square": '<rect x="3" y="3" width="18" height="18" rx="2"></rect>'
                + '<line x1="9" y1="9" x2="15" y2="15"></line>'
                + '<line x1="15" y1="9" x2="9" y2="15"></line>',
            "trash": '<polyline points="3 6 5 6 21 6"></polyline>'
                + '<path d="M19 6l-1 14a2 2 0 0 1 -2 2H8a2 2 0 0 1-2-2L5 6"></path>',
            "eraser": '<path d="M3 17l9-9 4 4-9 9H3v-4z"></path>'
                + '<path d="M14 4l4 4"></path>',
            "clock": '<circle cx="12" cy="12" r="10"></circle>'
                + '<polyline points="12 6 12 12 16 14"></polyline>',
            "corner-up-left": '<polyline points="9 14 4 9 9 4"></polyline>'
                + '<path d="M20 20v-7a4 4 0 0 0-4-4H4"></path>',
            "check-circle": '<circle cx="12" cy="12" r="10"></circle>'
                + '<polyline points="9 12 11 14 15 10"></polyline>',
            "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>',
            "lightbulb": '<path d="M9 18h6M10 21h4M12 2a6 6 0 0 0-3.2 11.1c.6.4 1 .8 1.2 1.4V18h4v-3.5c.2-.6.6-1 1.2-1.4A6 6 0 0 0 12 2z"></path>',
            "box": '<path d="M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path>',
            "alert-triangle": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>'
                + '<line x1="12" y1="9" x2="12" y2="13"></line>'
                + '<line x1="12" y1="17" x2="12.01" y2="17"></line>',
            "refresh-cw": '<polyline points="23 4 23 10 17 10"></polyline>'
                + '<polyline points="1 20 1 14 7 14"></polyline>'
                + '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"></path>'
                + '<path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14"></path>',
            "alert-circle": '<circle cx="12" cy="12" r="10"></circle>'
                + '<line x1="12" y1="8" x2="12" y2="12"></line>'
                + '<line x1="12" y1="16" x2="12.01" y2="16"></line>',
            "list": '<line x1="8" y1="6" x2="21" y2="6"></line>'
                + '<line x1="8" y1="12" x2="21" y2="12"></line>'
                + '<line x1="8" y1="18" x2="21" y2="18"></line>'
                + '<line x1="3" y1="6" x2="3.01" y2="6"></line>'
                + '<line x1="3" y1="12" x2="3.01" y2="12"></line>'
                + '<line x1="3" y1="18" x2="3.01" y2="18"></line>',
            "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"></rect>'
                + '<line x1="16" y1="2" x2="16" y2="6"></line>'
                + '<line x1="8" y1="2" x2="8" y2="6"></line>'
                + '<line x1="3" y1="10" x2="21" y2="10"></line>',
            "mail": '<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path>'
                + '<polyline points="22,6 12,13 2,6"></polyline>',
            "check-square": '<polyline points="9 11 12 14 22 4"></polyline>'
                + '<path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>',
            "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>'
                + '<polyline points="14 2 14 8 20 8"></polyline>',
            "git-branch": '<line x1="6" y1="3" x2="6" y2="15"></line>'
                + '<circle cx="18" cy="6" r="3"></circle>'
                + '<circle cx="6" cy="18" r="3"></circle>'
                + '<path d="M18 9a9 9 0 0 1-9 9"></path>',
            // Broom icon (user-feedback fix #5, 2026-05-03 morning).
            // The earlier "eraser" path was a pencil shape; this is
            // an actual broom: handle on top-right, bristles at
            // bottom-left.
            "broom": '<path d="M19.36 2.72l1.42 1.42-5.5 5.5-1.41-1.42z"></path>'
                + '<path d="M14.65 7.43l-9.93 9.93a3 3 0 0 0-.7 3.05l.7 2.09 7.07-7.07"></path>'
                + '<path d="M11.79 15.43l-2.83 2.83-3.54-3.54"></path>',
            // Pencil-edit icon for inline edit affordances on
            // intent / context items / actions (user-feedback fix #5).
            "edit": '<path d="M12 20h9"></path>'
                + '<path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path>',
            // Right-chevron — used as the "expand for more info"
            // affordance on view-only items (e.g. context items).
            // Reads as "drill in" rather than "toggle visibility"
            // (user-feedback 2026-05-03 PM): the eye icon was
            // considered for view-only entries but the expand-
            // arrow vibe matched the user's intent better.
            "chevron-right": '<polyline points="9 6 15 12 9 18"></polyline>',
        };
        const p = paths[name] || '';
        return '<svg class="threads-icon" width="16" height="16" '
             + 'viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             + 'stroke-width="2" stroke-linecap="round" '
             + 'stroke-linejoin="round">' + p + '</svg>';
    }

    // ----- Delegated action adapters (replaces inline on*= handlers) ---
    // See core/delegation.py. Each adapter reads its args from
    // el.dataset (camelCase of the data-* attrs wbActAttrs wrote) and
    // calls the same window.* function the old inline handler called,
    // with the same arguments in the same order.

    window.wbAction('threadCardCloseRightPane', function (el) {
        threadCardFocus(el.dataset.threadId, null);
    });

    window.wbAction('threadCardFocusIntent', function (el) {
        threadCardFocus(el.dataset.threadId, 'intent');
    });

    window.wbAction('threadCardFocusContext', function (el) {
        threadCardFocus(el.dataset.threadId, el.dataset.contextId);
    });

    window.wbAction('threadCardFocusAction', function (el) {
        threadCardFocus(el.dataset.threadId, el.dataset.actionId);
    });

    // Shared "navigate to a thread by id" action. Used both by the
    // singular-hoisted action <li> (data-target-id = host_thread_id)
    // and the sub-thread mini-card <li> (data-target-id = sub.thread_id).
    window.wbAction('threadsPushPathTarget', function (el) {
        threadsPushPath(el.dataset.targetId);
    });

    window.wbAction('threadCardRedirectHostAction', function (el) {
        threadCardRedirectAction(el.dataset.hostId);
    });

    window.wbAction('threadCardOpenEvlog', function (el, e) {
        e.preventDefault();
        threadsOpenInspector('evlog');
    });

    window.wbAction('threadsOpenSubThreadActionBtn', function (el) {
        threadsOpenSubThreadAction(el.dataset.subThreadId, el.dataset.actionId);
    });

    window.wbAction('threadCardEditIntentInput', function (el) {
        threadCardEditIntent(el.dataset.threadId, el.value);
    });

    window.wbAction('threadCardIntentEditorKeydown', function (el, e) {
        threadCardEditorKeydown(e, el.dataset.threadId, 'intent');
    });

    window.wbAction('threadCardDiscardIntentEditBtn', function (el) {
        threadCardDiscardIntentEdit(el.dataset.threadId);
    });

    window.wbAction('threadCardConfirmIntentEditBtn', function (el) {
        threadCardConfirmIntentEdit(el.dataset.threadId);
    });

    window.wbAction('threadClarifyInputChange', function (el) {
        threadClarifyInput(el.dataset.threadId, el.value);
    });

    window.wbAction('threadRedirectInputChange', function (el) {
        threadRedirectInput(el.dataset.threadId, el.value);
    });

    window.wbAction('threadCardToggleFlagBtn', function (el) {
        threadCardToggleFlag(el.dataset.threadId, el.dataset.itemId);
    });

    // Shared verb-dispatch action for the footer buttons that call
    // threadCommitAction(tid, verb) with no extra option object
    // (dismiss / cleanup / redirect / accept / accept-cleanup-failure /
    // retry-cleanup).
    window.wbAction('threadCommitActionVerb', function (el) {
        threadCommitAction(el.dataset.threadId, el.dataset.verb);
    });

    // "Later" button — left-click always defers 6h via the option
    // object; the right-click (oncontextmenu) popup stays a plain
    // inline handler since the dispatcher has no contextmenu event.
    window.wbAction('threadCommitActionLater', function (el) {
        threadCommitAction(el.dataset.threadId, 'later', {hours: 6});
    });

    // Clarification-card Accept — submits the staged free-text input.
    window.wbAction('threadCommitActionClarifyAccept', function (el) {
        const t = el.dataset.threadId;
        threadCommitAction(t, 'accept', {input: window._clarifyInput[t] || ''});
    });

    // Redirect-card submit — sends the staged redirect feedback text.
    window.wbAction('threadCommitActionRedirectSubmit', function (el) {
        const t = el.dataset.threadId;
        threadCommitAction(t, 'redirect', {feedback: window._redirectInput[t] || ''});
    });

    // Right-click "Later" -> options popup (contextmenu delegated). threadId
    // comes from the button's data-thread-id (emitted by its click action).
    window.wbAction('threadsLaterPopup', function (el, e) {
        e.preventDefault();
        threadsShowLaterPopup(el, el.dataset.threadId);
    });
})();
"""


def styles() -> str:
    return r"""
/* Stage 4.2 — Confirmation card layout */

.threads-card {
    max-width: 1100px;
    margin: 1.5em auto;
    background: var(--bg-secondary, #1a1a1a);
    border-radius: 10px;
    border: 1px solid var(--border, #333);
    overflow: hidden;
    color: var(--text, #ddd);
}

.threads-card-empty {
    padding: 2em;
    color: var(--text-muted, #888);
    text-align: center;
}

.threads-card-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border, #333);
}

.threads-card-title {
    font-size: 17px;
    font-weight: 600;
    margin-bottom: 4px;
}

.threads-card-meta {
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 12px;
    color: var(--text-muted, #888);
}

.threads-state {
    text-transform: capitalize;
}

.threads-urgency-pill.high {
    background: #c0392b;
    color: white;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
}

/* Two-pane body — right pane only renders when something is
   focused (user-feedback fix #1, 2026-05-03 morning). */
.threads-card-body {
    display: grid;
    grid-template-columns: 1fr;
    min-height: 280px;
}

.threads-card.threads-with-right-pane .threads-card-body {
    grid-template-columns: 1fr 360px;
}

.threads-card-left {
    padding: 16px 20px;
}

.threads-card.threads-with-right-pane .threads-card-left {
    border-right: 1px solid var(--border, #333);
}

.threads-card-right {
    padding: 16px 20px;
    background: var(--bg-tertiary, #0f0f0f);
    position: relative;
}

/* X button on the right pane — close + return to full width */
.threads-right-close {
    position: absolute;
    top: 8px;
    right: 8px;
    background: transparent;
    color: var(--text-muted, #888);
    border: none;
    cursor: pointer;
    padding: 4px;
    border-radius: 4px;
    line-height: 0;
}
.threads-right-close:hover {
    background: var(--bg-secondary, #1a1a1a);
    color: var(--text, #ddd);
}

.threads-section {
    margin-bottom: 20px;
}

.threads-section-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-muted, #888);
    margin-bottom: 6px;
}

.threads-intent {
    color: var(--text, #ddd);
    line-height: 1.45;
    font-size: 14px;
    background: var(--bg-tertiary, #0f0f0f);
    padding: 10px 14px;
    border-radius: 6px;
    border: 1px solid var(--border, #333);
    margin-bottom: 6px;
}

/* User-feedback fix #5 (2026-05-03 morning): edit buttons
   become icon-only (pencil) with tooltip on hover, matching the
   footer's icon-only style. Square shape so they sit cleanly
   alongside the X-flag button. */
.threads-edit-btn {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 4px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 0;
}
.threads-edit-btn:hover {
    background: var(--bg-tertiary, #0f0f0f);
    color: var(--text, #ddd);
}

/* Per-action Redirect button. Same chrome as the edit button so it
   doesn't visually shout; the action label is enough signal that it's
   a re-inference affordance. */
.threads-redirect-btn {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 4px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 0;
}
.threads-redirect-btn:hover {
    background: var(--bg-tertiary, #0f0f0f);
    color: var(--accent, #6cf);
}

.threads-list {
    list-style: none;
    padding: 0;
    margin: 0;
}

.threads-item {
    padding: 10px 14px;
    background: var(--bg-tertiary, #0f0f0f);
    border-radius: 6px;
    border: 1px solid var(--border, #333);
    margin-bottom: 6px;
    border-left: 3px solid transparent;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 8px;
    align-items: center;
    transition: background-color 80ms, border-color 80ms;
}

/* Clickable context cards — whole row opens the right-pane
 * inspector. Hover hint + cursor + focus ring for keyboard users. */
.threads-item-clickable {
    cursor: pointer;
}
.threads-item-clickable:hover {
    background: var(--bg-secondary, #1a1a1a);
    border-color: var(--accent, #4a7fc1);
}
.threads-item-clickable:focus-visible {
    outline: 2px solid var(--accent, #4a7fc1);
    outline-offset: 2px;
}
/* The chevron is now a visual hint, not a click target. Brighten
 * on row hover to reinforce the affordance. */
.threads-item-clickable .threads-expand-hint {
    color: var(--text-muted, #666);
    display: inline-flex;
    align-items: center;
    transition: color 80ms, transform 80ms;
}
.threads-item-clickable:hover .threads-expand-hint {
    color: var(--accent, #4a7fc1);
    transform: translateX(2px);
}

/* X-flagged element styling: muted-red left-border + faded text */
.threads-item.threads-flagged {
    border-left-color: #c0392b;
    opacity: 0.55;
}

/* Context-blocked action: amber left-border + slightly faded */
.threads-item.threads-ctx-blocked {
    border-left-color: #b8860b;
    opacity: 0.85;
}

/* Singular-pattern: settled action (done / rejected / failed) on a
   parent_relationship='singular' umbrella's hoisted actions list.
   Gray-out so the user can see at a glance which proposals on this
   umbrella are still pending vs already dealt with. The per-action
   action-row (Edit / Flag) is also suppressed by the renderer when
   `a.settled === true`. See `_renderActionsSection` in this file
   for the gating logic. */
.threads-item.threads-action-settled {
    opacity: 0.55;
    /* Subtle visual cue beyond opacity — a faint strikethrough on
       the action's main label tells the eye "this is done" even when
       the user has the page contrast set high. */
}
.threads-item.threads-action-settled .threads-item-label {
    text-decoration-line: line-through;
    text-decoration-color: rgba(120, 120, 120, 0.45);
    text-decoration-thickness: 1px;
}

/* Per-action status badge inline with the action label. */
.threads-action-status-badge {
    display: inline-block;
    margin-left: 6px;
    padding: 1px 6px;
    border-radius: 3px;
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    font-weight: 500;
    vertical-align: middle;
    text-decoration: none !important;  /* Don't strike-through the badge */
}
.threads-action-status-badge.done {
    background: rgba(50, 150, 80, 0.20);
    color: #6dd99a;
}
.threads-action-status-badge.rejected {
    background: rgba(150, 150, 150, 0.18);
    color: #999;
}
.threads-action-status-badge.failed {
    background: rgba(200, 100, 30, 0.18);
    color: #d8884a;
}
.threads-action-status-badge.executing {
    background: rgba(60, 130, 200, 0.18);
    color: #6da8d9;
}

/* (Singular-pattern hoisted actions: the whole <li> is clickable —
   navigates via threadsPushPath to the child Thread. The card-click
   pattern uses .threads-item-clickable from earlier in this stylesheet
   plus a chevron hint at the end. No separate "Open thread" link
   needed.) */

/* Per-context status pill */
.threads-ctx {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    margin-right: 6px;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
}
.threads-ctx-ok {
    background: rgba(50, 150, 80, 0.15);
    color: #6dd99a;
}
.threads-ctx-down {
    background: rgba(180, 100, 30, 0.15);
    color: #d8a06d;
}
.threads-ctx-user {
    background: rgba(100, 100, 180, 0.15);
    color: #9da4d4;
}

.threads-item-label {
    font-size: 14px;
    color: var(--text, #ddd);
    grid-column: 1;
}

.threads-item-summary,
.threads-item-source,
.threads-item-contexts {
    font-size: 12px;
    color: var(--text-muted, #888);
    grid-column: 1;
    margin-top: 2px;
}

.threads-item-source code {
    background: var(--bg, #0a0a0a);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
}

.threads-item-actions {
    grid-column: 2;
    grid-row: 1 / span 4;
    display: flex;
    gap: 4px;
    align-items: center;
}

.threads-flag-btn {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 4px 7px;
    cursor: pointer;
    line-height: 0;
}
.threads-flag-btn:hover {
    color: #e74c3c;
    border-color: #c0392b;
}
.threads-flag-btn.threads-flag-on {
    background: #c0392b;
    color: white;
    border-color: #c0392b;
}

/* Tags */
.threads-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
}
.threads-tag {
    background: var(--bg-tertiary, #0f0f0f);
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
}
.threads-empty {
    color: var(--text-muted, #666);
    font-size: 12px;
}

/* Sub-thread link */
.threads-subthread-link {
    color: var(--accent, #4a7fc1);
    text-decoration: none;
    font-size: 13px;
}
.threads-subthread-link:hover { text-decoration: underline; }

/* Right pane */
.threads-right-empty {
    color: var(--text-muted, #888);
    font-size: 12px;
    font-style: italic;
    padding: 1em;
}

.threads-right-editor h4 {
    margin: 0 0 0.6em 0;
    font-size: 13px;
    color: var(--text, #ddd);
}

.threads-textarea {
    width: 100%;
    background: var(--bg, #0a0a0a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 8px 10px;
    font-family: inherit;
    font-size: 13px;
    resize: vertical;
}

/* Editor hint line — small grey caption above the textarea
 * explaining Enter / Shift+Enter / Esc semantics. */
.threads-editor-hint {
    color: var(--text-muted, #888);
    font-size: 11px;
    margin: 0 0 6px 0;
}
.threads-editor-hint kbd {
    background: var(--bg-tertiary, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 1px 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 10px;
}

/* Editor action buttons (Discard / Confirm) — sit below the
 * textarea, right-aligned. Mirror the dashboard's neutral / accent
 * button colour pair so the pair reads as cancel/submit. */
.threads-editor-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 8px;
}
.threads-editor-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: transparent;
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 5px;
    padding: 6px 12px;
    font-size: 12px;
    cursor: pointer;
}
.threads-editor-btn:hover {
    background: var(--bg-tertiary, #1a1a1a);
}
.threads-editor-btn-confirm {
    border-color: var(--accent, #4a7fc1);
    color: var(--accent, #4a7fc1);
}
.threads-editor-btn-confirm:hover {
    background: var(--accent, #4a7fc1);
    color: #fff;
}
.threads-editor-btn-cancel {
    color: var(--text-muted, #888);
}
.threads-editor-icon {
    font-size: 14px;
    line-height: 1;
}
.threads-editor-label {
    font-size: 12px;
}

.threads-json-view {
    background: var(--bg, #0a0a0a);
    color: var(--text-muted, #aaa);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 10px;
    font-size: 12px;
    overflow: auto;
    max-height: 300px;
}

/* Footer */
.threads-card-footer {
    border-top: 1px solid var(--border, #333);
    padding: 12px 20px;
    display: flex;
    /* User-feedback fix #6: unified icon row, no left/right
       cluster split. */
    justify-content: flex-end;
    gap: 8px;
    align-items: center;
    background: var(--bg, #0a0a0a);
}

.threads-footer-secondary,
.threads-footer-primary {
    display: flex;
    gap: 8px;
    align-items: center;
}

.threads-btn-icon {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 6px 8px;
    cursor: pointer;
    line-height: 0;
    transition: color 100ms, background 100ms, border-color 100ms;
}
.threads-btn-icon:hover:not(:disabled) {
    color: var(--text, #ddd);
    background: var(--bg-tertiary, #1a1a1a);
}
.threads-btn-icon:disabled {
    opacity: 0.4;
    cursor: not-allowed;
}

/* User-feedback fix #6: per-action color-coding for footer
   icons. Themed via CSS variables to stay consistent with the
   dashboard's existing color palette. Hover slightly intensifies
   the color. */
.threads-btn-icon.threads-btn-destructive {
    color: #c66464;
    border-color: rgba(198, 100, 100, 0.4);
}
.threads-btn-icon.threads-btn-destructive:hover:not(:disabled) {
    color: #ff8888;
    background: rgba(198, 100, 100, 0.08);
    border-color: rgba(198, 100, 100, 0.6);
}
.threads-btn-icon.threads-btn-neutral {
    color: var(--text-muted, #888);
}
.threads-btn-icon.threads-btn-redirect {
    color: #d99868;
    border-color: rgba(217, 152, 104, 0.4);
}
.threads-btn-icon.threads-btn-redirect:hover:not(:disabled) {
    color: #ffb888;
    background: rgba(217, 152, 104, 0.08);
    border-color: rgba(217, 152, 104, 0.6);
}
.threads-btn-icon.threads-btn-accept {
    color: #66cc66;
    border-color: rgba(102, 204, 102, 0.4);
}
.threads-btn-icon.threads-btn-accept:hover:not(:disabled) {
    color: #88dd88;
    background: rgba(102, 204, 102, 0.1);
    border-color: rgba(102, 204, 102, 0.6);
}

.threads-btn {
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 7px 14px;
    font-size: 13px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
}

.threads-btn-primary {
    background: var(--accent, #4a7fc1);
    color: white;
}
.threads-btn-primary:disabled {
    background: var(--bg-tertiary, #2a2a2a);
    color: var(--text-muted, #666);
    cursor: not-allowed;
}
.threads-btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
}

.threads-btn-secondary {
    background: var(--bg-tertiary, #2a2a2a);
    color: var(--text, #ddd);
    border-color: var(--border, #333);
}
.threads-btn-secondary:hover {
    background: var(--bg, #1a1a1a);
}

.threads-icon {
    display: inline-block;
    vertical-align: middle;
}

/* Stage 4.5 — Consent risk banner */
.threads-risk-banner {
    background: #4a2424;
    color: #fbcaca;
    padding: 10px 18px;
    border-bottom: 1px solid var(--border, #333);
    font-size: 13px;
}

/* Wave A/B (2026-05-03) — Risk pill on the card header.
   Color-codes the consent card so the user can see at a glance
   whether they're about to approve something with real risk. */
.threads-risk-pill {
    display: inline-block;
    margin-left: 8px;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.5px;
    text-transform: uppercase;
}
.threads-risk-pill.high {
    background: #4a2424;
    color: #ff8888;
    border: 1px solid #ff5555;
}
.threads-risk-pill.medium {
    background: #4a3624;
    color: #ffbb88;
    border: 1px solid #ff9955;
}
.threads-risk-pill.low {
    background: #244a2c;
    color: #88dd88;
    border: 1px solid #66cc66;
}

/* Confidence badge — inline chip showing agent self-reported
   confidence on intent / context / action sections. Helps the
   user calibrate trust at a glance. */
.threads-confidence {
    display: inline-block;
    padding: 1px 6px;
    margin-left: 6px;
    border-radius: 8px;
    font-size: 10px;
    font-weight: 500;
    vertical-align: 1px;
}
.threads-confidence.high {
    background: rgba(102, 204, 102, 0.15);
    color: #88dd88;
}
.threads-confidence.medium {
    background: rgba(255, 153, 85, 0.15);
    color: #ffbb88;
}
.threads-confidence.low {
    background: rgba(255, 85, 85, 0.15);
    color: #ff8888;
}

/* Auto-advance breadcrumb — shows the agent's recent autonomy
   decisions (intent + context auto-advanced under PLAN_THEN_REVIEW). */
.threads-auto-advance {
    margin-top: 8px;
    padding: 6px 12px;
    background: rgba(74, 127, 193, 0.08);
    border-left: 2px solid var(--accent, #4a7fc1);
    color: var(--text-muted, #aaa);
    font-size: 11px;
    font-style: italic;
    border-radius: 0 4px 4px 0;
    display: flex;
    align-items: center;
    gap: 6px;
}

/* Relative timestamp in card header */
.threads-timestamp {
    margin-left: auto;
    font-size: 11px;
    color: var(--text-muted, #888);
    font-weight: normal;
    cursor: help;
}

/* Action kind chip — small badge next to the action name showing
   "standard" | "improvised" | "suggestion". */
.threads-kind-chip {
    display: inline-block;
    margin-left: 6px;
    padding: 1px 7px;
    border-radius: 8px;
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    vertical-align: 1px;
    background: rgba(170, 170, 170, 0.12);
    color: var(--text-muted, #aaa);
}
.threads-kind-chip.standard {
    background: rgba(74, 127, 193, 0.15);
    color: #88bbee;
}
.threads-kind-chip.improvised {
    background: rgba(255, 153, 85, 0.15);
    color: #ffaa66;
}
.threads-kind-chip.suggestion {
    background: rgba(170, 170, 170, 0.12);
    color: var(--text-muted, #aaa);
}

/* Risk-disclosure row inside an action item.
   Surfaces irreversibility / regret_potential / risk_amplifier. */
.threads-risk-row {
    margin-top: 4px;
    font-size: 11px;
    color: var(--text-muted, #888);
    display: flex;
    align-items: center;
    gap: 4px;
}
.threads-risk-row.threads-risk-high {
    color: #ff8888;
}

/* Rationale + blocked-on inline text */
.threads-item-rationale,
.threads-item-blocked {
    margin-top: 4px;
    font-size: 12px;
    color: var(--text-muted, #aaa);
    line-height: 1.4;
}

/* User-feedback followup (2026-05-03): inline sub-thread list
   under the parent's detail view. Each is a small clickable
   card that drills into the sub-thread when clicked. */
.threads-subthread-list {
    list-style: none;
    padding: 0;
    margin: 8px 0 0 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.threads-subthread-card {
    background: var(--bg-tertiary, #0f0f0f);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 10px 14px;
    cursor: pointer;
    transition: border-color 80ms, background 80ms;
}
.threads-subthread-card:hover {
    border-color: var(--accent, #4a7fc1);
    background: var(--bg-secondary, #1a1a1a);
}
.threads-subthread-card.threads-mid-process {
    opacity: 0.6;
    border-style: dashed;
}
.threads-subthread-card.threads-terminal {
    opacity: 0.55;
}
.threads-subthread-meta {
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 11px;
    color: var(--text-muted, #888);
    margin-bottom: 4px;
    text-transform: capitalize;
}
.threads-subthread-title {
    color: var(--text, #ddd);
    font-weight: 500;
    font-size: 13px;
    line-height: 1.3;
    margin-bottom: 2px;
}
.threads-subthread-intent {
    color: var(--text-muted, #aaa);
    font-size: 12px;
    line-height: 1.4;
}

/* Inline action preview on a sub-thread mini-card.
 * One row per proposed action with a tiny edit-pencil that opens the
 * sub-thread already focused on that action's right-pane editor. */
.threads-subthread-actions {
    margin-top: 6px;
    border-top: 1px dashed rgba(80,80,80,0.4);
    padding-top: 6px;
}
.threads-subthread-action {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-muted, #aaa);
    padding: 2px 0;
}
.threads-subthread-action-name {
    color: var(--text, #ddd);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1 1 auto;
    min-width: 0;
}
.threads-subthread-edit-btn {
    background: transparent;
    border: none;
    padding: 2px 6px;
    color: var(--text-muted, #888);
    cursor: pointer;
    border-radius: 3px;
    flex: 0 0 auto;
}
.threads-subthread-edit-btn:hover {
    color: var(--accent, #4a7fc1);
    background: var(--bg-tertiary, #1a1a1a);
}

.threads-subthread-loading {
    color: var(--text-muted, #888);
    font-size: 12px;
    font-style: italic;
    padding: 8px 0;
}

/* Wave I — sub-thread aggregated state badges */
.threads-state-badges {
    margin-top: 6px;
    font-size: 11px;
    color: var(--text-muted, #888);
}
.threads-state-badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--bg, #0a0a0a);
    color: var(--text-muted, #aaa);
    border: 1px solid rgba(60,60,60,0.5);
    font-size: 10px;
    margin: 2px 4px 2px 0;
}

/* Wave C — timeline / event-log link */
.threads-timeline-link-row {
    margin-top: 10px;
    text-align: right;
}
.threads-timeline-link {
    color: var(--text-muted, #888);
    text-decoration: none;
    font-size: 11px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    cursor: pointer;
}
.threads-timeline-link:hover {
    color: var(--accent, #4a7fc1);
}

/* Context-item inspector table in the right pane. */
.threads-ci-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}
.threads-ci-table th {
    text-align: left;
    color: var(--text-muted, #888);
    font-weight: 500;
    padding: 4px 8px 4px 0;
    width: 30%;
    vertical-align: top;
}
.threads-ci-table td {
    padding: 4px 0;
    vertical-align: top;
    word-break: break-word;
}
.threads-ci-payload-header {
    padding-top: 12px !important;
    color: var(--text, #ddd) !important;
    font-weight: 600 !important;
    border-top: 1px solid var(--border, #333);
}
.threads-ci-table code {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    background: var(--bg, #1a1a1a);
    padding: 1px 4px;
    border-radius: 3px;
}

/* Multi-line payload block — preserves newlines, scrolls vertically
 * once content exceeds ~6 lines so the inspector stays readable
 * even for journal segments (raw_text) or pasted JSON blobs. */
.threads-ci-payload-block {
    margin: 2px 0;
    padding: 8px 10px;
    background: var(--bg, #1a1a1a);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 12px;
    line-height: 1.45;
    color: var(--text, #ddd);
    /* ~6 lines visible; scroll past that. ``ch`` for horizontal
     * sanity, but mostly we expect long vertical content. */
    max-height: calc(1.45em * 6 + 16px);
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-word;
    /* Soft border accent based on language to hint type — JSON gets
     * a slightly different shade than plain text. */
}
.threads-ci-payload-block[data-lang="json"] {
    border-left: 3px solid var(--accent, #4a7fc1);
}
.threads-ci-payload-block[data-lang="text"] {
    border-left: 3px solid var(--text-muted, #555);
}

.threads-ci-payload-link {
    color: var(--accent, #4a7fc1);
    text-decoration: none;
    word-break: break-all;
}
.threads-ci-payload-link:hover {
    text-decoration: underline;
}

/* Clarification card */
.threads-clarify-body {
    padding: 20px;
    display: block;
}
.threads-clarify-prompt {
    color: var(--text, #ddd);
    font-size: 14px;
    margin: 0 0 12px 0;
}
.threads-clarify-textarea {
    width: 100%;
    padding: 10px 12px;
    background: var(--bg, #0a0a0a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    font: inherit;
    font-size: 14px;
    resize: vertical;
    margin-bottom: 14px;
}
.threads-clarify-context { margin-top: 12px; }

/* Review card */
.threads-review-body {
    padding: 20px;
    display: block;
}
.threads-review-status {
    font-size: 14px;
    color: var(--text, #ddd);
    margin-bottom: 8px;
    font-weight: 600;
}
.threads-review-summary {
    color: var(--text-muted, #aaa);
    margin-bottom: 8px;
    font-size: 13px;
}
.threads-review-output {
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 10px 12px;
    font-size: 12px;
    overflow: auto;
    max-height: 280px;
    color: var(--text-muted, #aaa);
}
.threads-review-run {
    font-size: 12px;
    color: var(--text-muted, #888);
}
.threads-review-run code {
    background: var(--bg, #0a0a0a);
    padding: 1px 6px;
    border-radius: 3px;
}

/* Redirect card */
.threads-redirect-body {
    padding: 20px;
    display: block;
}
.threads-redirect-failure {
    background: #2c1c1c;
    color: #f3a3a3;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 14px;
    font-size: 13px;
}
.threads-redirect-failure code {
    background: rgba(0,0,0,0.4);
    padding: 1px 6px;
    border-radius: 3px;
}
.threads-redirect-prompt {
    color: var(--text, #ddd);
    margin: 0 0 8px 0;
    font-size: 14px;
}
.threads-redirect-textarea {
    width: 100%;
    padding: 10px 12px;
    background: var(--bg, #0a0a0a);
    color: var(--text, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    font: inherit;
    font-size: 14px;
    resize: vertical;
}

/* Cleanup-failure card */
.threads-cleanup-fail-body {
    padding: 20px;
    display: block;
}
.threads-cleanup-fail-banner {
    background: #4a2424;
    color: #fbcaca;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 14px;
    font-size: 13px;
}
.threads-cleanup-fail-detail {
    color: var(--text-muted, #aaa);
    font-size: 13px;
    margin-bottom: 12px;
}
"""
