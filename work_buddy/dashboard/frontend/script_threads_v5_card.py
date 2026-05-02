"""Threads tab v5 — Stage 4.2 confirmation card layout (visual).

Implements the two-pane confirmation card layout (UX.md §4 + §5).
Visual + local-state only — backend wiring lands in Stage 4.3.

Exposed:
- ``window.renderConfirmationCard(threadData)`` — returns HTML for
  the card. Used by ``renderThreadDetail`` (in script_threads_v5.py).
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


def _threads_v5_card_script() -> str:
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

    // ----- Card rendering ----------------------------------------------

    window.renderConfirmationCard = function renderConfirmationCard(thread) {
        if (!thread) {
            return '<div class="threads-v5-card-empty">No thread loaded.</div>';
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

        let html = '<div class="threads-v5-card threads-v5-kind-' + kind + '" '
                 + 'data-thread-id="' + _esc(thread.thread_id) + '">';

        // Risk-amplifier emphasis for consent cards
        if (kind === "consent") {
            html += _renderRiskBanner(thread);
        }

        html += '<div class="threads-v5-card-body">';
        html += '<div class="threads-v5-card-left">';
        html += _renderHeader(thread);
        html += _renderIntentSection(thread, s);
        html += _renderContextSection(thread, s);
        html += _renderActionsSection(thread, s);
        html += _renderNamespaceTagsSection(thread, s);
        html += _renderSubThreadsLink(thread);
        html += '</div>';
        html += '<div class="threads-v5-card-right">';
        html += _renderRightPane(thread, s);
        html += '</div>';
        html += '</div>';

        html += _renderFooter(thread, hasFlags);
        html += '</div>';
        return html;
    }

    function _renderRiskBanner(thread) {
        // Aggregate intrinsic_amplifiers across actions
        let riskBits = [];
        for (const a of (thread.actions || [])) {
            const amp = a.intrinsic_amplifiers || {};
            for (const dim of Object.keys(amp)) {
                const val = amp[dim];
                if (val === "high" || val === "irreversible") {
                    riskBits.push(dim + "=" + val);
                }
            }
        }
        if (riskBits.length === 0) return '';
        return (
            '<div class="threads-v5-risk-banner">'
            + _icon("alert-triangle")
            + ' <strong>Consent gate:</strong> '
            + 'high-impact action — ' + _esc(riskBits.join(', '))
            + '</div>'
        );
    }


    // ----- Section renderers -------------------------------------------

    function _renderHeader(thread) {
        const urgent = thread.urgency === "surface_now";
        const stateLabel = (thread.fsm_state || "").replace(/_/g, " ");
        return (
            '<div class="threads-v5-card-header">'
            + '<div class="threads-v5-card-title">'
            +   _esc(thread.title || thread.thread_id)
            + '</div>'
            + '<div class="threads-v5-card-meta">'
            +   '<span class="threads-v5-state">' + _esc(stateLabel) + '</span>'
            +   (urgent
                    ? '<span class="threads-v5-urgency-pill high">SURFACE NOW</span>'
                    : '')
            + '</div>'
            + '</div>'
        );
    }

    function _renderIntentSection(thread, s) {
        // Intent: NO X-flag (UX.md §5.2). Only editable.
        const text = (thread.intent && thread.intent.text) || "(no intent inferred)";
        const editedText = s.edited.intent !== undefined
            ? s.edited.intent
            : text;
        return (
            '<div class="threads-v5-section">'
            + '<div class="threads-v5-section-label">Intent</div>'
            + '<div class="threads-v5-intent">'
            +   _esc(editedText)
            + '</div>'
            + '<button class="threads-v5-edit-btn" '
            +   'onclick="threadCardFocus(\'' + _esc(thread.thread_id) + '\', \'intent\')">'
            +   'Edit'
            + '</button>'
            + '</div>'
        );
    }

    function _renderContextSection(thread, s) {
        const items = thread.context_items || [];
        if (items.length === 0) {
            return (
                '<div class="threads-v5-section">'
                + '<div class="threads-v5-section-label">Context (none inferred)</div>'
                + '</div>'
            );
        }
        let html = '<div class="threads-v5-section">';
        html += '<div class="threads-v5-section-label">Context ('
              + items.length + ')</div>';
        html += '<ul class="threads-v5-list">';
        for (const ci of items) {
            const flagged = s.flagged.has(ci.id);
            html += '<li class="threads-v5-item'
                  + (flagged ? ' threads-v5-flagged' : '') + '">';
            html += '<div class="threads-v5-item-label">'
                  + _esc(ci.label || ci.id) + '</div>';
            html += '<div class="threads-v5-item-source">'
                  + _esc(ci.source || "") + (ci.type ? " · " + _esc(ci.type) : "")
                  + '</div>';
            html += '<div class="threads-v5-item-actions">';
            html += _flagBtn(thread.thread_id, ci.id, flagged);
            html += '<button class="threads-v5-edit-btn" '
                  + 'onclick="threadCardFocus(\'' + _esc(thread.thread_id) + '\', \''
                  + _esc(ci.id) + '\')">Edit</button>';
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
                '<div class="threads-v5-section">'
                + '<div class="threads-v5-section-label">Actions (none inferred)</div>'
                + '</div>'
            );
        }
        let html = '<div class="threads-v5-section">';
        html += '<div class="threads-v5-section-label">Actions ('
              + actions.length + ')</div>';
        html += '<ul class="threads-v5-list">';
        for (const a of actions) {
            const flagged = s.flagged.has(a.id);
            html += '<li class="threads-v5-item'
                  + (flagged ? ' threads-v5-flagged' : '') + '">';
            html += '<div class="threads-v5-item-label">'
                  + _kindIcon(a.kind) + ' ' + _esc(a.name || a.id) + '</div>';
            const summary = a.plan_summary || _summariseParams(a.parameters);
            if (summary) {
                html += '<div class="threads-v5-item-summary">'
                      + _esc(summary) + '</div>';
            }
            // Action-context status indicator (Stage 4.11 fills this in
            // for real; 4.2 just shows the placeholder hook).
            if (Array.isArray(a.required_contexts) && a.required_contexts.length) {
                html += '<div class="threads-v5-item-contexts">Requires: '
                      + a.required_contexts.map(_esc).join(', ')
                      + '</div>';
            }
            html += '<div class="threads-v5-item-actions">';
            html += _flagBtn(thread.thread_id, a.id, flagged);
            html += '<button class="threads-v5-edit-btn" '
                  + 'onclick="threadCardFocus(\'' + _esc(thread.thread_id) + '\', \''
                  + _esc(a.id) + '\')">Edit</button>';
            html += '</div>';
            html += '</li>';
        }
        html += '</ul>';
        html += '</div>';
        return html;
    }

    function _renderNamespaceTagsSection(thread, s) {
        const tags = thread.namespace_tags || [];
        return (
            '<div class="threads-v5-section">'
            + '<div class="threads-v5-section-label">Namespace tags</div>'
            + '<div class="threads-v5-tags">'
            +   (tags.length > 0
                    ? tags.map(t => '<span class="threads-v5-tag">'
                                  + _esc(t) + '</span>').join('')
                    : '<em class="threads-v5-empty">(none)</em>')
            + '</div>'
            + '</div>'
        );
    }

    function _renderSubThreadsLink(thread) {
        const n = thread.sub_thread_count || 0;
        if (n === 0) return '';
        return (
            '<div class="threads-v5-section">'
            + '<a class="threads-v5-subthread-link" href="#" '
            +   'onclick="event.preventDefault();threadsPushPath(\''
            +   _esc(thread.thread_id) + '\')">'
            +   '&equiv; Sub-threads (' + n + ') &rarr;'
            + '</a>'
            + '</div>'
        );
    }

    function _renderRightPane(thread, s) {
        const focused = s.focusedId;
        if (!focused) {
            return (
                '<div class="threads-v5-right-empty">'
                + '<p>Click any item or action on the left to edit it here.</p>'
                + '</div>'
            );
        }
        if (focused === "intent") {
            const edited = s.edited.intent !== undefined
                ? s.edited.intent
                : ((thread.intent && thread.intent.text) || "");
            return (
                '<div class="threads-v5-right-editor">'
                + '<h4>Edit intent</h4>'
                + '<textarea class="threads-v5-textarea" rows="6" '
                +   'oninput="threadCardEditIntent(\'' + _esc(thread.thread_id)
                +     '\', this.value)">' + _esc(edited) + '</textarea>'
                + '</div>'
            );
        }
        // Context-item or action editor (Stage 4.6 — per-action
        // specialized renderers via window._actionRenderers).
        const target = _findById(thread, focused);
        if (!target) {
            return (
                '<div class="threads-v5-right-empty">'
                + '<p>(focused element ' + _esc(focused) + ' not found)</p>'
                + '</div>'
            );
        }
        // Actions go through the action-renderer registry.
        if (target.kind === "action"
            && typeof window.renderActionInRightPane === "function") {
            return (
                '<div class="threads-v5-right-editor">'
                + window.renderActionInRightPane(thread, target)
                + '</div>'
            );
        }
        // Context items: simple inspector for now (modal richness
        // can land in a follow-up).
        return (
            '<div class="threads-v5-right-editor">'
            + '<h4>' + _esc((target.kind || "element").replace(/_/g, " "))
            +   ' &middot; <code>' + _esc(focused) + '</code></h4>'
            + '<pre class="threads-v5-json-view">'
            +   _esc(JSON.stringify(target, null, 2))
            + '</pre>'
            + '</div>'
        );
    }

    function _renderFooter(thread, hasFlags) {
        // Footer button set per UX.md §4.1 + §5.4. Backend wired in 4.3.
        const cleanupShown = !!thread.can_clean_up;
        const acceptDisabled = hasFlags;
        const acceptTitle = hasFlags
            ? "Resolve any flagged elements before accepting"
            : "Commit the current state";
        const tid = _esc(thread.thread_id);
        return (
            '<div class="threads-v5-card-footer">'
            + '<div class="threads-v5-footer-secondary">'
            +   '<button class="threads-v5-btn-icon" title="Dismiss" '
            +     'onclick="threadCommitAction(\'' + tid + '\', \'dismiss\')">'
            +     _icon("trash") + '</button>'
            +   (cleanupShown
                    ? '<button class="threads-v5-btn-icon" title="Clean up source" '
                    +   'onclick="threadCommitAction(\'' + tid + '\', \'cleanup\')">'
                    +   _icon("eraser") + '</button>'
                    : '')
            +   '<button class="threads-v5-btn-icon" title="Later (6h)" '
            +     'onclick="threadCommitAction(\'' + tid + '\', \'later\', {hours: 6})">'
            +     _icon("clock") + '</button>'
            + '</div>'
            + '<div class="threads-v5-footer-primary">'
            +   '<button class="threads-v5-btn threads-v5-btn-secondary" '
            +     'onclick="threadCommitAction(\'' + tid + '\', \'redirect\')">'
            +     _icon("corner-up-left") + ' Re-direct'
            +   '</button>'
            +   '<button class="threads-v5-btn threads-v5-btn-primary" '
            +     (acceptDisabled ? 'disabled ' : '')
            +     'title="' + _esc(acceptTitle) + '" '
            +     (acceptDisabled
                    ? ''
                    : 'onclick="threadCommitAction(\'' + tid + '\', \'accept\')"')
            +     '>' + _icon("check") + ' Accept'
            +   '</button>'
            + '</div>'
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
        let html = '<div class="threads-v5-card threads-v5-kind-clarification" '
                 + 'data-thread-id="' + tid + '">';
        html += _renderHeader(thread);
        html += '<div class="threads-v5-card-body threads-v5-clarify-body">';
        html += '<p class="threads-v5-clarify-prompt">' + _esc(promptText)
              + '</p>';
        html += '<textarea class="threads-v5-clarify-textarea" rows="6" '
              + 'placeholder="Tell the agent what you mean..." '
              + 'oninput="threadClarifyInput(\'' + tid + '\', this.value)">'
              + _esc(stored) + '</textarea>';
        if (thread.context_items && thread.context_items.length > 0) {
            html += '<div class="threads-v5-clarify-context">';
            html += '<div class="threads-v5-section-label">Context the agent has so far</div>';
            html += '<ul class="threads-v5-list">';
            for (const ci of thread.context_items) {
                html += '<li class="threads-v5-item">';
                html += '<div class="threads-v5-item-label">'
                      + _esc(ci.label || ci.id) + '</div>';
                html += '<div class="threads-v5-item-source">'
                      + _esc(ci.source || "") + '</div>';
                html += '</li>';
            }
            html += '</ul></div>';
        }
        html += '</div>';
        // Footer: Trash / Broom / Later / (Re-direct skipped — clarif IS the redirect target) / Accept
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
        const tid = _esc(thread.thread_id);
        const cleanupShown = !!thread.can_clean_up;
        return (
            '<div class="threads-v5-card-footer">'
            + '<div class="threads-v5-footer-secondary">'
            +   '<button class="threads-v5-btn-icon" title="Dismiss" '
            +     'onclick="threadCommitAction(\'' + tid + '\', \'dismiss\')">'
            +     _icon("trash") + '</button>'
            +   (cleanupShown
                    ? '<button class="threads-v5-btn-icon" title="Clean up source" '
                    +   'onclick="threadCommitAction(\'' + tid + '\', \'cleanup\')">'
                    +   _icon("eraser") + '</button>'
                    : '')
            +   '<button class="threads-v5-btn-icon" title="Later (6h)" '
            +     'onclick="threadCommitAction(\'' + tid + '\', \'later\', {hours: 6})">'
            +     _icon("clock") + '</button>'
            + '</div>'
            + '<div class="threads-v5-footer-primary">'
            +   '<button class="threads-v5-btn threads-v5-btn-primary" '
            +     'onclick="threadCommitAction(\'' + tid + '\', \'accept\', {input: window._clarifyInput[\'' + tid + '\'] || \'\'})">'
            +     _icon("check") + ' Accept'
            +   '</button>'
            + '</div>'
            + '</div>'
        );
    }

    // ----- Review card (UX.md §4.4.3) ----------------------------------

    function _renderReview(thread) {
        const tid = _esc(thread.thread_id);
        const ctx = thread.review_context || {};
        let html = '<div class="threads-v5-card threads-v5-kind-review" '
                 + 'data-thread-id="' + tid + '">';
        html += _renderHeader(thread);
        html += '<div class="threads-v5-card-body threads-v5-review-body">';
        html += '<div class="threads-v5-review-status">'
              + 'Result: ' + _esc(ctx.status || 'completed')
              + '</div>';
        if (ctx.summary) {
            html += '<div class="threads-v5-review-summary">'
                  + _esc(ctx.summary) + '</div>';
        }
        if (ctx.output) {
            html += '<pre class="threads-v5-review-output">'
                  + _esc(typeof ctx.output === "string"
                        ? ctx.output
                        : JSON.stringify(ctx.output, null, 2))
                  + '</pre>';
        }
        if (ctx.run_id) {
            html += '<p class="threads-v5-review-run">Run ID: <code>'
                  + _esc(ctx.run_id) + '</code></p>';
        }
        html += '</div>';
        // UX.md §4.4.3: Review cards have NO Dismiss (action's done).
        // Footer: Later / Re-direct / Mark done
        html += '<div class="threads-v5-card-footer">'
              + '<div class="threads-v5-footer-secondary">'
              +   '<button class="threads-v5-btn-icon" title="Later (6h)" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'later\', {hours: 6})">'
              +     _icon("clock") + '</button>'
              + '</div>'
              + '<div class="threads-v5-footer-primary">'
              +   '<button class="threads-v5-btn threads-v5-btn-secondary" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'redirect\')">'
              +     _icon("corner-up-left") + ' Re-direct'
              +   '</button>'
              +   '<button class="threads-v5-btn threads-v5-btn-primary" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'accept\')">'
              +     _icon("check") + ' Mark done'
              +   '</button>'
              + '</div>'
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
        let html = '<div class="threads-v5-card threads-v5-kind-redirect" '
                 + 'data-thread-id="' + tid + '">';
        html += _renderHeader(thread);
        html += '<div class="threads-v5-card-body threads-v5-redirect-body">';
        html += '<div class="threads-v5-redirect-failure">'
              + '<strong>Execution failed.</strong>';
        if (fc.error) {
            html += ' Error: <code>' + _esc(fc.error) + '</code>';
        }
        if (fc.step) {
            html += ' Step: <code>' + _esc(fc.step) + '</code>';
        }
        html += '</div>';
        html += '<p class="threads-v5-redirect-prompt">'
              + 'Tell the agent what to do now.</p>';
        html += '<textarea class="threads-v5-redirect-textarea" rows="5" '
              + 'placeholder="Describe what went wrong / what to try..." '
              + 'oninput="threadRedirectInput(\'' + tid + '\', this.value)">'
              + _esc(stored) + '</textarea>';
        html += '</div>';
        // Footer: Trash / Broom / Later / Re-direct (= submit feedback) / Skip (= accept)
        const cleanupShown = !!thread.can_clean_up;
        html += '<div class="threads-v5-card-footer">'
              + '<div class="threads-v5-footer-secondary">'
              +   '<button class="threads-v5-btn-icon" title="Dismiss" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'dismiss\')">'
              +     _icon("trash") + '</button>'
              +   (cleanupShown
                    ? '<button class="threads-v5-btn-icon" title="Clean up source" '
                    +   'onclick="threadCommitAction(\'' + tid + '\', \'cleanup\')">'
                    +   _icon("eraser") + '</button>'
                    : '')
              +   '<button class="threads-v5-btn-icon" title="Later (6h)" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'later\', {hours: 6})">'
              +     _icon("clock") + '</button>'
              + '</div>'
              + '<div class="threads-v5-footer-primary">'
              +   '<button class="threads-v5-btn threads-v5-btn-primary" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'redirect\', {feedback: window._redirectInput[\'' + tid + '\'] || \'\'})">'
              +     _icon("corner-up-left") + ' Submit redirect'
              +   '</button>'
              + '</div>'
              + '</div>';
        html += '</div>';
        return html;
    }

    // ----- Cleanup-failure card (UX.md §6.5) ---------------------------

    function _renderCleanupFailure(thread) {
        const tid = _esc(thread.thread_id);
        const cf = thread.cleanup_failure || {};
        let html = '<div class="threads-v5-card threads-v5-kind-cleanup-failure" '
                 + 'data-thread-id="' + tid + '">';
        html += _renderHeader(thread);
        html += '<div class="threads-v5-card-body threads-v5-cleanup-fail-body">';
        html += '<div class="threads-v5-cleanup-fail-banner">'
              + _icon("alert-triangle")
              + ' <strong>Cleanup failed.</strong></div>';
        if (cf.detail) {
            html += '<p class="threads-v5-cleanup-fail-detail">'
                  + _esc(cf.detail) + '</p>';
        }
        html += '<p class="threads-v5-stage-note">'
              + 'You can retry the cleanup or accept the failure '
              + '(closes this Thread without further action).</p>';
        html += '</div>';
        // Footer: Trash / Later / Accept failure / Retry
        html += '<div class="threads-v5-card-footer">'
              + '<div class="threads-v5-footer-secondary">'
              +   '<button class="threads-v5-btn-icon" title="Dismiss" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'dismiss\')">'
              +     _icon("trash") + '</button>'
              +   '<button class="threads-v5-btn-icon" title="Later (6h)" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'later\', {hours: 6})">'
              +     _icon("clock") + '</button>'
              + '</div>'
              + '<div class="threads-v5-footer-primary">'
              +   '<button class="threads-v5-btn threads-v5-btn-secondary" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'accept-cleanup-failure\')">'
              +     _icon("check") + ' Accept failure'
              +   '</button>'
              +   '<button class="threads-v5-btn threads-v5-btn-primary" '
              +     'onclick="threadCommitAction(\'' + tid + '\', \'retry-cleanup\')">'
              +     _icon("refresh-cw") + ' Retry'
              +   '</button>'
              + '</div>'
              + '</div>';
        html += '</div>';
        return html;
    }

    // ----- Helpers ------------------------------------------------------

    function _kindIcon(kind) {
        if (kind === "standard") return _icon("check-circle");
        if (kind === "improvised") return _icon("zap");
        if (kind === "suggestion") return _icon("lightbulb");
        return _icon("box");
    }

    function _flagBtn(threadId, itemId, flagged) {
        return '<button class="threads-v5-flag-btn'
             + (flagged ? ' threads-v5-flag-on' : '') + '" '
             + 'title="' + (flagged ? 'Unflag' : 'Flag as wrong') + '" '
             + 'onclick="threadCardToggleFlag(\'' + _esc(threadId) + '\', \''
             + _esc(itemId) + '\')">'
             + (flagged ? _icon("x-square") : _icon("x"))
             + '</button>';
    }

    function _summariseParams(params) {
        if (!params || typeof params !== "object") return "";
        const keys = Object.keys(params);
        if (keys.length === 0) return "";
        const first = keys[0];
        const v = params[first];
        const text = (typeof v === "string" || typeof v === "number")
            ? String(v) : JSON.stringify(v);
        if (text.length > 90) return first + ": " + text.slice(0, 87) + "...";
        return first + ": " + text;
    }

    function _findById(thread, id) {
        for (const ci of (thread.context_items || [])) {
            if (ci.id === id) return Object.assign({ kind: "context" }, ci);
        }
        for (const a of (thread.actions || [])) {
            if (a.id === id) return Object.assign({ kind: "action" }, a);
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
        };
        const p = paths[name] || '';
        return '<svg class="threads-v5-icon" width="16" height="16" '
             + 'viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             + 'stroke-width="2" stroke-linecap="round" '
             + 'stroke-linejoin="round">' + p + '</svg>';
    }
})();
"""


def _threads_v5_card_styles() -> str:
    return r"""
/* Stage 4.2 — Confirmation card layout */

.threads-v5-card {
    max-width: 1100px;
    margin: 1.5em auto;
    background: var(--bg-secondary, #1a1a1a);
    border-radius: 10px;
    border: 1px solid var(--border, #333);
    overflow: hidden;
    color: var(--text, #ddd);
}

.threads-v5-card-empty {
    padding: 2em;
    color: var(--text-muted, #888);
    text-align: center;
}

.threads-v5-card-header {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border, #333);
}

.threads-v5-card-title {
    font-size: 17px;
    font-weight: 600;
    margin-bottom: 4px;
}

.threads-v5-card-meta {
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 12px;
    color: var(--text-muted, #888);
}

.threads-v5-state {
    text-transform: capitalize;
}

.threads-v5-urgency-pill.high {
    background: #c0392b;
    color: white;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
}

/* Two-pane body */
.threads-v5-card-body {
    display: grid;
    grid-template-columns: 1fr 360px;
    min-height: 280px;
}

.threads-v5-card-left {
    padding: 16px 20px;
    border-right: 1px solid var(--border, #333);
}

.threads-v5-card-right {
    padding: 16px 20px;
    background: var(--bg-tertiary, #0f0f0f);
}

.threads-v5-section {
    margin-bottom: 20px;
}

.threads-v5-section-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-muted, #888);
    margin-bottom: 6px;
}

.threads-v5-intent {
    color: var(--text, #ddd);
    line-height: 1.45;
    font-size: 14px;
    background: var(--bg-tertiary, #0f0f0f);
    padding: 10px 14px;
    border-radius: 6px;
    border: 1px solid var(--border, #333);
    margin-bottom: 6px;
}

.threads-v5-edit-btn {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 3px 9px;
    font-size: 11px;
    cursor: pointer;
}
.threads-v5-edit-btn:hover {
    background: var(--bg-tertiary, #0f0f0f);
    color: var(--text, #ddd);
}

.threads-v5-list {
    list-style: none;
    padding: 0;
    margin: 0;
}

.threads-v5-item {
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
}

/* X-flagged element styling: muted-red left-border + faded text */
.threads-v5-item.threads-v5-flagged {
    border-left-color: #c0392b;
    opacity: 0.55;
}

.threads-v5-item-label {
    font-size: 14px;
    color: var(--text, #ddd);
    grid-column: 1;
}

.threads-v5-item-summary,
.threads-v5-item-source,
.threads-v5-item-contexts {
    font-size: 12px;
    color: var(--text-muted, #888);
    grid-column: 1;
    margin-top: 2px;
}

.threads-v5-item-source code {
    background: var(--bg, #0a0a0a);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 11px;
}

.threads-v5-item-actions {
    grid-column: 2;
    grid-row: 1 / span 4;
    display: flex;
    gap: 4px;
    align-items: center;
}

.threads-v5-flag-btn {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    padding: 4px 7px;
    cursor: pointer;
    line-height: 0;
}
.threads-v5-flag-btn:hover {
    color: #e74c3c;
    border-color: #c0392b;
}
.threads-v5-flag-btn.threads-v5-flag-on {
    background: #c0392b;
    color: white;
    border-color: #c0392b;
}

/* Tags */
.threads-v5-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
}
.threads-v5-tag {
    background: var(--bg-tertiary, #0f0f0f);
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
}
.threads-v5-empty {
    color: var(--text-muted, #666);
    font-size: 12px;
}

/* Sub-thread link */
.threads-v5-subthread-link {
    color: var(--accent, #4a7fc1);
    text-decoration: none;
    font-size: 13px;
}
.threads-v5-subthread-link:hover { text-decoration: underline; }

/* Right pane */
.threads-v5-right-empty {
    color: var(--text-muted, #888);
    font-size: 12px;
    font-style: italic;
    padding: 1em;
}

.threads-v5-right-editor h4 {
    margin: 0 0 0.6em 0;
    font-size: 13px;
    color: var(--text, #ddd);
}

.threads-v5-textarea {
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

.threads-v5-json-view {
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
.threads-v5-card-footer {
    border-top: 1px solid var(--border, #333);
    padding: 12px 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--bg, #0a0a0a);
}

.threads-v5-footer-secondary,
.threads-v5-footer-primary {
    display: flex;
    gap: 8px;
    align-items: center;
}

.threads-v5-btn-icon {
    background: transparent;
    color: var(--text-muted, #888);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 6px 8px;
    cursor: pointer;
    line-height: 0;
}
.threads-v5-btn-icon:hover {
    color: var(--text, #ddd);
    background: var(--bg-tertiary, #1a1a1a);
}

.threads-v5-btn {
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 7px 14px;
    font-size: 13px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
}

.threads-v5-btn-primary {
    background: var(--accent, #4a7fc1);
    color: white;
}
.threads-v5-btn-primary:disabled {
    background: var(--bg-tertiary, #2a2a2a);
    color: var(--text-muted, #666);
    cursor: not-allowed;
}
.threads-v5-btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
}

.threads-v5-btn-secondary {
    background: var(--bg-tertiary, #2a2a2a);
    color: var(--text, #ddd);
    border-color: var(--border, #333);
}
.threads-v5-btn-secondary:hover {
    background: var(--bg, #1a1a1a);
}

.threads-v5-icon {
    display: inline-block;
    vertical-align: middle;
}

/* Stage 4.5 — Consent risk banner */
.threads-v5-risk-banner {
    background: #4a2424;
    color: #fbcaca;
    padding: 10px 18px;
    border-bottom: 1px solid var(--border, #333);
    font-size: 13px;
}

/* Clarification card */
.threads-v5-clarify-body {
    padding: 20px;
    display: block;
}
.threads-v5-clarify-prompt {
    color: var(--text, #ddd);
    font-size: 14px;
    margin: 0 0 12px 0;
}
.threads-v5-clarify-textarea {
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
.threads-v5-clarify-context { margin-top: 12px; }

/* Review card */
.threads-v5-review-body {
    padding: 20px;
    display: block;
}
.threads-v5-review-status {
    font-size: 14px;
    color: var(--text, #ddd);
    margin-bottom: 8px;
    font-weight: 600;
}
.threads-v5-review-summary {
    color: var(--text-muted, #aaa);
    margin-bottom: 8px;
    font-size: 13px;
}
.threads-v5-review-output {
    background: var(--bg, #0a0a0a);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 10px 12px;
    font-size: 12px;
    overflow: auto;
    max-height: 280px;
    color: var(--text-muted, #aaa);
}
.threads-v5-review-run {
    font-size: 12px;
    color: var(--text-muted, #888);
}
.threads-v5-review-run code {
    background: var(--bg, #0a0a0a);
    padding: 1px 6px;
    border-radius: 3px;
}

/* Redirect card */
.threads-v5-redirect-body {
    padding: 20px;
    display: block;
}
.threads-v5-redirect-failure {
    background: #2c1c1c;
    color: #f3a3a3;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 14px;
    font-size: 13px;
}
.threads-v5-redirect-failure code {
    background: rgba(0,0,0,0.4);
    padding: 1px 6px;
    border-radius: 3px;
}
.threads-v5-redirect-prompt {
    color: var(--text, #ddd);
    margin: 0 0 8px 0;
    font-size: 14px;
}
.threads-v5-redirect-textarea {
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
.threads-v5-cleanup-fail-body {
    padding: 20px;
    display: block;
}
.threads-v5-cleanup-fail-banner {
    background: #4a2424;
    color: #fbcaca;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 14px;
    font-size: 13px;
}
.threads-v5-cleanup-fail-detail {
    color: var(--text-muted, #aaa);
    font-size: 13px;
    margin-bottom: 12px;
}
"""
