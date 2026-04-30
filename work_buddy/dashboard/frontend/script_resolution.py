"""Resolution Surface JS (Slice 1.5).

Decorator over ``renderTriageReview`` that adds the unified
Resolution Surface affordances:

- Pipeline-blocker badge per card (typed, per ROADMAP §3.3)
- Attraction-passes count (when > 0)
- Defer button + ``s`` keybinding (POSTs ``/api/triage/defer``;
  bumps the entry's ``attraction_passes`` without acting)
- Re-direct button + ``r`` keybinding (POSTs ``/api/triage/redirect``;
  persists user-supplied ``forced_context`` and quarantines the
  entry — Slice 3 wires up the pipeline re-run)
- Keyboard navigation layer (``j``/``k`` move focus, ``enter`` per-card
  submit, ``s`` defer, ``r`` revise, ``?`` help)

This module composes with the existing renderer rather than replacing
it: the verdict_review and raw_capture cards share their fundamental
layout (action pills, drag-to-reassign, namespace tags, per-source
open buttons), and Slice 1.5 layers Resolution Surface controls on
top via the ``decorateCard`` hook. Slices 3+ that introduce real
multi-record schemas can swap individual decorations for full
per-resolution-type renderers without touching this glue.

The module owns a single public entry point — ``mountResolutionSurface``
— that script_review.py calls instead of the bare ``renderTriageReview``.
All Slice 1 patterns (``user_initiated`` consent context,
success-only ``mark_reviewed`` filter, ``operation_errors`` surfacing,
raw-entry rendering, ``perGroupSubmit``, bridge-timeout handling) are
preserved by delegation: the underlying renderer enforces them and
this module only adds adjacent affordances.
"""

from __future__ import annotations


def _resolution_surface_script() -> str:
    return r"""
// ---------------------------------------------------------------------------
// Resolution Surface — Slice 1.5
// ---------------------------------------------------------------------------
//
// mountResolutionSurface(container, presentation, options)
//   options.onSubmit       (gd, reassignments) => Promise<void>  -- for verdict_review submit
//   options.onComplete     () => void                            -- after all cards cleared
//   options.onItemClick    (item, group) => void                 -- delegated to right drawer
//
// Side effects:
// - Mutates `container` to render the surface.
// - Installs a keydown listener on `container` (removed by caller via
//   the returned `dispose` fn when the tab unmounts; the dashboard
//   keeps the Review tab mounted so dispose is rarely needed).

window.mountResolutionSurface = function(container, presentation, options) {
    options = options || {};
    const pres = presentation || {};

    // Per-resolution-type placeholder copy — used when a card declares
    // a type Slice 1.5 doesn't fully render yet. The taxonomy strings
    // come from work_buddy.triage.resolution; keep aligned by editing
    // both sides.
    const PLACEHOLDER_BY_TYPE = {
        clarification: { slice: 'Slice 3',
            note: 'Clarification cards generalize the chrome-triage clarify modal; that wiring lands with the new Clarify schema.' },
        placement:     { slice: 'Slice 6',
            note: 'Reference filing surfaces live in the Reference filing pipeline.' },
        decomposition: { slice: 'Slice 7',
            note: 'Decomposition cards (sub-action editor) ship with pickup-time evaluation.' },
        plan_approval: { slice: 'Slice 4',
            note: 'Plan approval cards land with the risk-model + automation tiers.' },
        output_review: { slice: 'Slice 4',
            note: 'Output review cards land with tier-3 automation.' },
    };

    // Decoration callback — run after each card lands in the DOM.
    // Idempotent: re-runs on rerenderCard() will see existing nodes
    // and replace them, not double-stack.
    function decorateCard(cardEl, group) {
        if (!cardEl || !group) return;

        // Header right column anchors badges next to the confidence pill.
        const headerRight = cardEl.querySelector('.wv-group-header-right');

        // 1) Pipeline-blocker badge ----------------------------------
        const existingBlocker = cardEl.querySelector('.wv-blocker-badge');
        if (existingBlocker) existingBlocker.remove();
        if (group.pipeline_blocker && headerRight) {
            const blk = group.pipeline_blocker;
            const wrap = document.createElement('span');
            wrap.className = 'wv-blocker-badge tone-' + (blk.tone || 'info');
            wrap.title = blk.detail || blk.label || '';
            const icon = blk.tone === 'blocked' ? '⛔'   // ⛔
                       : blk.tone === 'deferred' ? '⏳'  // ⏳
                       : 'ℹ';                           // ℹ
            wrap.innerHTML = '<span class="wv-blocker-icon">' + icon + '</span>'
                + '<span class="wv-blocker-label">' + _resEsc(blk.label || blk.kind) + '</span>';
            // Optional deep-link affordance (e.g. setup-wizard for
            // agent_context_unmet).
            if (blk.deep_link) {
                const link = document.createElement('a');
                link.href = blk.deep_link;
                link.className = 'wv-blocker-link';
                link.textContent = blk.deep_link_label || 'Open';
                link.addEventListener('click', (e) => e.stopPropagation());
                wrap.appendChild(link);
            }
            // Insert before the confidence pill so the user reads the
            // blocker first.
            headerRight.insertBefore(wrap, headerRight.firstChild);
        }

        // 2) Attraction-passes display -------------------------------
        const existingPass = cardEl.querySelector('.wv-pass-count');
        if (existingPass) existingPass.remove();
        const passes = parseInt(group.attraction_passes || 0, 10);
        if (passes > 0 && headerRight) {
            const pc = document.createElement('span');
            pc.className = 'wv-pass-count';
            pc.title = 'You have deferred this ' + passes + ' time(s). '
                + 'Slice 8 will use this signal for resurfacing priority.';
            pc.textContent = '⏳ ' + passes;
            headerRight.appendChild(pc);
        }

        // 3) Resolution-type placeholder -----------------------------
        // For types not yet fully rendered, replace the rationale +
        // pills area with an explanatory note. verdict_review and
        // raw_capture share the existing layout — no change.
        const ph = PLACEHOLDER_BY_TYPE[group.resolution_type];
        if (ph) {
            const body = cardEl.querySelector('.wv-card-body');
            if (body) {
                const main = body.querySelector('.wv-card-main');
                if (main) {
                    // Insert a one-time placeholder note above the pills.
                    let note = main.querySelector('.wv-resolution-placeholder');
                    if (!note) {
                        note = document.createElement('div');
                        note.className = 'wv-resolution-placeholder';
                        note.innerHTML =
                            '<strong>' + _resEsc(group.resolution_type) + '</strong> '
                            + 'card type ships with ' + _resEsc(ph.slice) + '. '
                            + _resEsc(ph.note);
                        main.insertBefore(note, main.firstChild);
                    }
                }
            }
        }

        // 4) Per-card Resolution Surface footer ----------------------
        // Renders Defer / Re-direct buttons next to the existing
        // Submit (if perGroupSubmit). Replaces any prior Slice 1.5
        // footer to stay idempotent across re-renders.
        const existingResFooter = cardEl.querySelector('.wv-res-actions');
        if (existingResFooter) existingResFooter.remove();
        const item = (group.items && group.items[0]) || null;
        if (item && item.pool_run_id && item.id) {
            const resFooter = document.createElement('div');
            resFooter.className = 'wv-res-actions';

            // Defer ("Later") — non-shaming copy
            const deferBtn = document.createElement('button');
            deferBtn.type = 'button';
            deferBtn.className = 'wv-res-btn wv-res-defer';
            deferBtn.textContent = 'Later';
            deferBtn.title = 'Defer this card without acting on it (s)';
            deferBtn.addEventListener('click', () => _resDefer(cardEl, group, item, deferBtn));
            resFooter.appendChild(deferBtn);

            // Re-direct ("Revise")
            const redirectBtn = document.createElement('button');
            redirectBtn.type = 'button';
            redirectBtn.className = 'wv-res-btn wv-res-redirect';
            redirectBtn.textContent = 'Re-direct';
            redirectBtn.title = 'Provide forced context; pipeline re-runs (r)';
            redirectBtn.addEventListener('click', () => _resOpenRedirectPrompt(cardEl, group, item));
            resFooter.appendChild(redirectBtn);

            // Sit just above any per-group submit footer (or at the
            // card bottom if perGroupSubmit is off).
            const groupFooter = cardEl.querySelector('.wv-group-footer');
            if (groupFooter) cardEl.insertBefore(resFooter, groupFooter);
            else cardEl.appendChild(resFooter);
        }

        // Mark this card focusable for the keyboard layer.
        if (!cardEl.hasAttribute('tabindex')) cardEl.setAttribute('tabindex', '0');
    }

    // -- Mount the underlying renderer ------------------------------------
    renderTriageReview(container, presentation, {
        perGroupSubmit: true,
        showNamespaceTags: true,
        onSubmit: options.onSubmit || (async () => {}),
        onComplete: options.onComplete || (() => {}),
        onItemClick: options.onItemClick || null,
        decorateCard,
    });

    // -- Help affordance + keyboard layer ---------------------------------
    _resInstallKeyboardLayer(container);
    _resInstallHelpHint(container);

    // Caller can call this to detach the keyboard listener — rarely
    // needed because the Review tab is persistent in the dashboard.
    return {
        dispose: () => _resTeardownKeyboardLayer(container),
    };
};


// ---- Defer + Redirect actions ---------------------------------------------

async function _resDefer(cardEl, group, item, btn) {
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Deferring…';
    try {
        const r = await fetch('/api/triage/defer', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                pool_run_id: item.pool_run_id,
                item_id: item.id,
            }),
        });
        const data = await r.json();
        if (!data || data.status !== 'ok') {
            console.error('[resolution] defer failed:', data);
            btn.textContent = 'Defer failed';
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
            return;
        }
        // Visual ack: dim card + bump the pass-count badge in place.
        cardEl.classList.add('wv-card-deferred');
        cardEl.style.transition = 'opacity 0.25s';
        cardEl.style.opacity = '0.55';
        btn.textContent = 'Deferred';
        // Update the count display without re-fetching the whole pool.
        const pc = cardEl.querySelector('.wv-pass-count');
        const newCount = (data.attraction_passes != null) ? data.attraction_passes
                       : ((parseInt(group.attraction_passes || 0, 10) + 1));
        if (pc) {
            pc.textContent = '⏳ ' + newCount;
        } else {
            const headerRight = cardEl.querySelector('.wv-group-header-right');
            if (headerRight) {
                const np = document.createElement('span');
                np.className = 'wv-pass-count';
                np.textContent = '⏳ ' + newCount;
                headerRight.appendChild(np);
            }
        }
        group.attraction_passes = newCount;
    } catch (e) {
        console.error('[resolution] defer threw:', e);
        btn.textContent = 'Defer failed';
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
    }
}

function _resOpenRedirectPrompt(cardEl, group, item) {
    // Don't double-open if a prompt is already showing on this card.
    if (cardEl.querySelector('.wv-res-redirect-prompt')) return;

    const prompt = document.createElement('div');
    prompt.className = 'wv-res-redirect-prompt';

    const label = document.createElement('div');
    label.className = 'wv-res-redirect-label';
    label.innerHTML = 'Re-direct: tell the agent what changes about this item. '
        + '<span class="wv-res-redirect-help">'
        + 'Project, intent, who/what — anything wrong with its premise. '
        + 'Slice 3 will re-run the pipeline with this context.'
        + '</span>';
    prompt.appendChild(label);

    const textarea = document.createElement('textarea');
    textarea.className = 'wv-res-redirect-input';
    textarea.placeholder = 'e.g. "This is for personal/finance, not the work project."';
    textarea.rows = 2;
    prompt.appendChild(textarea);

    const buttons = document.createElement('div');
    buttons.className = 'wv-res-redirect-buttons';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'wv-res-btn';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => prompt.remove());
    buttons.appendChild(cancelBtn);

    const submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.className = 'wv-res-btn wv-res-redirect-submit';
    submitBtn.textContent = 'Re-direct';
    submitBtn.addEventListener('click', async () => {
        const txt = (textarea.value || '').trim();
        if (!txt) {
            textarea.focus();
            textarea.classList.add('wv-res-redirect-input-error');
            setTimeout(() => textarea.classList.remove('wv-res-redirect-input-error'), 1200);
            return;
        }
        submitBtn.disabled = true;
        cancelBtn.disabled = true;
        submitBtn.textContent = 'Re-directing…';
        try {
            const r = await fetch('/api/triage/redirect', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    pool_run_id: item.pool_run_id,
                    item_id: item.id,
                    forced_context: { freeform: txt },
                    target_step: 'clarify',
                }),
            });
            const data = await r.json();
            if (!data || data.status !== 'ok') {
                submitBtn.textContent = 'Failed';
                console.error('[resolution] redirect failed:', data);
                setTimeout(() => {
                    submitBtn.disabled = false;
                    cancelBtn.disabled = false;
                    submitBtn.textContent = 'Re-direct';
                }, 1800);
                return;
            }
            // Quarantined now — the card is stale. Visually retire it.
            cardEl.classList.add('wv-card-redirected');
            cardEl.style.transition = 'opacity 0.25s';
            cardEl.style.opacity = '0.45';
            const note = document.createElement('div');
            note.className = 'wv-res-redirect-ack';
            note.textContent = 'Re-directed. Pipeline re-run wires up with Slice 3.';
            prompt.replaceWith(note);
        } catch (e) {
            console.error('[resolution] redirect threw:', e);
            submitBtn.textContent = 'Failed';
            setTimeout(() => {
                submitBtn.disabled = false;
                cancelBtn.disabled = false;
                submitBtn.textContent = 'Re-direct';
            }, 1800);
        }
    });
    buttons.appendChild(submitBtn);
    prompt.appendChild(buttons);

    // Insert at the card bottom, just above the Resolution Surface
    // action footer if present.
    const resFooter = cardEl.querySelector('.wv-res-actions');
    if (resFooter) cardEl.insertBefore(prompt, resFooter);
    else cardEl.appendChild(prompt);

    textarea.focus();
}


// ---- Keyboard layer -------------------------------------------------------

function _resInstallKeyboardLayer(container) {
    if (container._wvResKeyboardInstalled) return;
    container._wvResKeyboardInstalled = true;

    const handler = (e) => {
        // Don't hijack when the user is typing into an input.
        const tag = (document.activeElement && document.activeElement.tagName) || '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        // Bare-key shortcuts only — let modifier combos pass through to
        // existing dashboard shortcuts (Ctrl+K command palette, etc.).
        if (e.ctrlKey || e.metaKey || e.altKey) return;

        const cards = Array.from(container.querySelectorAll('.wv-group-card'));
        if (cards.length === 0) return;

        const focused = container.querySelector('.wv-group-card.wv-card-focused');
        const idx = focused ? cards.indexOf(focused) : -1;

        function focusCard(c) {
            cards.forEach(x => x.classList.remove('wv-card-focused'));
            if (c) {
                c.classList.add('wv-card-focused');
                c.scrollIntoView({block: 'nearest', behavior: 'smooth'});
                c.focus({preventScroll: true});
            }
        }

        if (e.key === 'k') {
            // Inverted from vim default — user preference.
            e.preventDefault();
            const next = cards[Math.min(cards.length - 1, idx + 1)] || cards[0];
            focusCard(next);
        } else if (e.key === 'j') {
            e.preventDefault();
            const prev = cards[Math.max(0, idx - 1)] || cards[0];
            focusCard(prev);
        } else if (e.key === 'Enter') {
            // Per-card submit on the focused card.
            const card = focused || cards[0];
            const submitBtn = card && card.querySelector('.wv-group-submit');
            if (submitBtn && !submitBtn.disabled) {
                e.preventDefault();
                submitBtn.click();
            }
        } else if (e.key === 's') {
            const card = focused || cards[0];
            const deferBtn = card && card.querySelector('.wv-res-defer');
            if (deferBtn && !deferBtn.disabled) {
                e.preventDefault();
                deferBtn.click();
            }
        } else if (e.key === 'r') {
            const card = focused || cards[0];
            const redirectBtn = card && card.querySelector('.wv-res-redirect');
            if (redirectBtn) {
                e.preventDefault();
                redirectBtn.click();
            }
        } else if (e.key === '?') {
            e.preventDefault();
            _resToggleHelpOverlay(container);
        }
    };

    document.addEventListener('keydown', handler);
    container._wvResKeyboardHandler = handler;
}

function _resTeardownKeyboardLayer(container) {
    if (container._wvResKeyboardHandler) {
        document.removeEventListener('keydown', container._wvResKeyboardHandler);
        delete container._wvResKeyboardHandler;
    }
    container._wvResKeyboardInstalled = false;
}


// ---- Help hint + overlay --------------------------------------------------

function _resInstallHelpHint(container) {
    if (container.querySelector('.wv-res-help-hint')) return;
    const hint = document.createElement('div');
    hint.className = 'wv-res-help-hint';
    hint.innerHTML =
        '<kbd>k</kbd>/<kbd>j</kbd> nav '
        + '· <kbd>enter</kbd> submit '
        + '· <kbd>s</kbd> later '
        + '· <kbd>r</kbd> re-direct '
        + '· <kbd>?</kbd> help';
    // Insert just under the renderer's own header.
    const header = container.querySelector('.wv-header');
    if (header && header.parentElement === container) {
        container.insertBefore(hint, header.nextSibling);
    } else {
        container.insertBefore(hint, container.firstChild);
    }
}

function _resToggleHelpOverlay(container) {
    let overlay = document.getElementById('wv-res-help-overlay');
    if (overlay) { overlay.remove(); return; }
    overlay = document.createElement('div');
    overlay.id = 'wv-res-help-overlay';
    overlay.className = 'wv-res-help-overlay';
    overlay.innerHTML =
        '<div class="wv-res-help-panel">'
        + '<h3>Resolution Surface — keyboard shortcuts</h3>'
        + '<table class="wv-res-help-table">'
        + '<tr><td><kbd>k</kbd></td><td>Next card (down)</td></tr>'
        + '<tr><td><kbd>j</kbd></td><td>Previous card (up)</td></tr>'
        + '<tr><td><kbd>enter</kbd></td><td>Submit focused card</td></tr>'
        + '<tr><td><kbd>s</kbd></td><td>Later (defer; bumps attraction count)</td></tr>'
        + '<tr><td><kbd>r</kbd></td><td>Re-direct (provide forced context; re-queues)</td></tr>'
        + '<tr><td><kbd>?</kbd></td><td>Toggle this help</td></tr>'
        + '</table>'
        + '<button class="wv-res-help-close">Close</button>'
        + '</div>';
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.remove();
    });
    overlay.querySelector('.wv-res-help-close').addEventListener('click', () => overlay.remove());
    document.body.appendChild(overlay);
}


// ---- Helpers --------------------------------------------------------------

function _resEsc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}
"""


def _resolution_surface_styles() -> str:
    return r"""
/* Resolution Surface (Slice 1.5) -- visual layer for the new affordances. */

.wv-blocker-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 500;
    margin-right: 6px;
    line-height: 1.4;
}
.wv-blocker-badge.tone-blocked   { background: #fde2e2; color: #8a1f1f; }
.wv-blocker-badge.tone-deferred  { background: #fff4d6; color: #6a4b00; }
.wv-blocker-badge.tone-info      { background: #e2ecfd; color: #1f3f8a; }
.wv-blocker-badge .wv-blocker-icon { font-size: 12px; }
.wv-blocker-link {
    margin-left: 6px;
    font-size: 10px;
    text-decoration: underline;
    color: inherit;
}

.wv-pass-count {
    font-size: 10px;
    color: var(--text-muted, #888);
    margin-left: 6px;
    cursor: help;
}

.wv-resolution-placeholder {
    background: #fff8e6;
    border: 1px solid #f0d878;
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 12px;
    color: #5a4500;
    margin-bottom: 8px;
}

.wv-res-actions {
    display: flex;
    gap: 6px;
    padding: 6px 12px 0 12px;
    border-top: 1px dashed var(--border-muted, #e6e6e6);
    margin-top: 6px;
    justify-content: flex-end;
}
.wv-res-btn {
    border: 1px solid var(--border-muted, #ccc);
    background: var(--bg-secondary, #f5f5f5);
    color: var(--text-primary, #222);
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 12px;
    cursor: pointer;
}
.wv-res-btn:hover { background: var(--bg-hover, #ececec); }
.wv-res-btn:disabled { opacity: 0.5; cursor: default; }
.wv-res-defer { }
.wv-res-redirect { }
.wv-res-redirect-submit { background: var(--accent, #4a6fa5); color: #fff; border-color: transparent; }

.wv-res-redirect-prompt {
    border: 1px solid var(--border-muted, #ddd);
    background: var(--bg-secondary, #fafafa);
    border-radius: 6px;
    padding: 10px 12px;
    margin: 8px 12px 0 12px;
}
.wv-res-redirect-label { font-size: 12px; margin-bottom: 6px; color: var(--text-primary, #222); }
.wv-res-redirect-help { color: var(--text-muted, #888); font-weight: normal; }
.wv-res-redirect-input {
    width: 100%;
    box-sizing: border-box;
    padding: 6px 8px;
    border: 1px solid var(--border-muted, #ccc);
    border-radius: 4px;
    font-family: inherit;
    font-size: 13px;
    resize: vertical;
}
.wv-res-redirect-input-error { border-color: #c33; box-shadow: 0 0 0 2px rgba(204,51,51,0.15); }
.wv-res-redirect-buttons { display: flex; gap: 6px; justify-content: flex-end; margin-top: 6px; }
.wv-res-redirect-ack {
    background: #e6f5e6;
    border: 1px solid #b6dcb6;
    color: #2d5a2d;
    border-radius: 6px;
    padding: 8px 12px;
    margin: 8px 12px 0 12px;
    font-size: 12px;
}

.wv-card-deferred, .wv-card-redirected { pointer-events: none; }
.wv-card-deferred .wv-res-actions, .wv-card-redirected .wv-res-actions { pointer-events: auto; }

.wv-card-focused {
    outline: 2px solid var(--accent, #4a6fa5);
    outline-offset: 2px;
}

.wv-res-help-hint {
    font-size: 11px;
    color: var(--text-muted, #888);
    padding: 4px 12px 0 12px;
    margin-bottom: 4px;
}
.wv-res-help-hint kbd {
    background: var(--bg-tertiary, #eee);
    border: 1px solid var(--border-muted, #ccc);
    border-bottom-width: 2px;
    border-radius: 3px;
    padding: 0 4px;
    font-family: monospace;
    font-size: 10px;
}

.wv-res-help-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.4);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 9999;
}
.wv-res-help-panel {
    background: var(--bg-primary, #fff);
    color: var(--text-primary, #222);
    border-radius: 8px;
    padding: 20px 24px;
    min-width: 360px;
    max-width: 90vw;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
}
.wv-res-help-panel h3 { margin: 0 0 12px 0; }
.wv-res-help-table { width: 100%; border-collapse: collapse; }
.wv-res-help-table td { padding: 4px 8px; }
.wv-res-help-table td:first-child { width: 60px; text-align: right; }
.wv-res-help-table kbd {
    background: var(--bg-tertiary, #eee);
    border: 1px solid var(--border-muted, #ccc);
    border-bottom-width: 2px;
    border-radius: 3px;
    padding: 1px 6px;
    font-family: monospace;
    font-size: 11px;
}
.wv-res-help-close {
    margin-top: 14px;
    padding: 6px 14px;
    border-radius: 4px;
    border: 1px solid var(--border-muted, #ccc);
    background: var(--bg-secondary, #f5f5f5);
    cursor: pointer;
}
"""
