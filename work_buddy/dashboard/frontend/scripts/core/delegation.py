"""Event-delegation dispatcher — the structural fix for FM-1.

The dashboard historically wired interactivity through inline
``onclick="fn('id')"`` attributes built by JS string concatenation. When a
handler arg carried a quote that collided with the attribute quotes, the
browser truncated the handler and the button died at click time with
``Uncaught SyntaxError: Unexpected end of input``. It was invisible to
``node --check`` (the string-building JS is valid) and invisible on page
load (the button renders fine), surfacing only on click.

This module replaces that pattern. A renderer emits a plain ``data-on-<event>``
attribute naming a registered action plus ``data-*`` args, and one delegated
listener per event type (bound to ``document``) looks the action up and calls
it with the element. Because the dynamic value now lives in a DOM attribute
(stored as data, never as a fragment of executable JS source), there is no
quoting context to collide with. A value containing ``'`` or ``"`` is just a
string the browser hands back verbatim via ``el.dataset``.

Binding to ``document`` (not to individual elements) is deliberate: the
SSE-driven refresh uses ``morphdom(el, stage, {childrenOnly: true})``
(see ``core/event_bus.py``), which swaps inner content but preserves ancestor
nodes. A document-level delegated listener therefore survives every refresh,
making delegation strictly more robust than per-child ``addEventListener``
under this model.

API (all on ``window``):

- ``wbAction(name, fn)`` — register an action. ``fn`` receives ``(el, event)``;
  read args from ``el.dataset`` (camelCase of the ``data-*`` keys).
- ``wbActAttrs(name, data, events)`` — build the safe attribute string for a
  renderer. ``data`` is an optional object of args (values escaped via the
  canonical ``escapeHtml``); ``events`` is a string or array (default
  ``'click'``). Example:
  ``'<button ' + wbActAttrs('threadsCancelDraft', {threadId: id}) + '>'``.

Concatenation order: registered after ``helpers`` (uses ``escapeHtml``) and
before ``page`` (which runs init at load). Declares no module-scope
let/const/var — everything lives inside the IIFE — so it is page-LAST safe.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Event-delegation dispatcher (replaces inline on*= handlers) ----
(function () {
    // Idempotent: never bind the document listeners twice.
    if (window.wbActions) return;
    window.wbActions = {};

    // Register an action. fn receives (el, event); read args from el.dataset.
    window.wbAction = function (name, fn) {
        if (typeof fn !== 'function') {
            throw new TypeError('wbAction: handler must be a function');
        }
        window.wbActions[name] = fn;
    };

    // Shared no-op action. Put data-on-click="wbNoop" on an element that
    // used to carry onclick="event.stopPropagation()" purely to keep a click
    // from triggering an ancestor's handler. Because the dispatcher below
    // resolves the CLOSEST data-on-click ancestor, a wbNoop child wins and
    // the ancestor action never fires — same effect, no inline handler.
    window.wbAction('wbNoop', function () {});

    function _camelToKebab(s) {
        return String(s).replace(/[A-Z]/g, function (m) {
            return '-' + m.toLowerCase();
        });
    }

    // Build the delegated-handler attribute string for a renderer. Every
    // value is escaped through the canonical escapeHtml, so a quote in an
    // arg can never break out of the attribute (the FM-1 fix, by
    // construction). ``events`` defaults to 'click'; pass an array to bind
    // one action to several events (e.g. ['input','change']).
    window.wbActAttrs = function (name, data, events) {
        var evs = events ? (Array.isArray(events) ? events : [events]) : ['click'];
        var parts = [];
        for (var i = 0; i < evs.length; i++) {
            parts.push('data-on-' + evs[i] + '="' + escapeHtml(name) + '"');
        }
        if (data) {
            for (var k in data) {
                if (!Object.prototype.hasOwnProperty.call(data, k)) continue;
                parts.push('data-' + _camelToKebab(k) + '="' + escapeHtml(data[k]) + '"');
            }
        }
        return parts.join(' ');
    };

    function _makeHandler(eventType) {
        var attr = 'data-on-' + eventType;
        return function (e) {
            var start = e.target;
            if (!start || !start.closest) return;
            var el = start.closest('[' + attr + ']');
            if (!el) return;
            var name = el.getAttribute(attr);
            var fn = window.wbActions[name];
            if (typeof fn !== 'function') {
                console.warn('[wb-actions] no handler for', name, '(' + eventType + ')');
                return;
            }
            try {
                fn(el, e);
            } catch (err) {
                console.error('[wb-actions] handler', name, 'threw:', err);
            }
        };
    }

    // One delegated listener per event type, bound to document so it
    // survives every morphdom childrenOnly refresh. All of these bubble
    // (focusout is the bubbling counterpart of the non-bubbling blur), so
    // the closest()-based dispatch works uniformly. mousedown/contextmenu
    // and the drag events are included so those handlers can be delegated
    // too; each listener early-returns unless a data-on-<event> ancestor
    // exists, so the added listeners are cheap.
    ['click', 'input', 'change', 'keydown', 'submit',
     'contextmenu', 'mousedown', 'focusout',
     'dragstart', 'dragend', 'dragover', 'dragleave', 'drop'
    ].forEach(function (ev) {
        document.addEventListener(ev, _makeHandler(ev));
    });

    // Enter/Space activation: any data-on-click element behaves like a
    // native button under the keyboard. This replaces the
    // onkeydown="if(event.key==='Enter'||event.key===' ')...same-as-click..."
    // accessibility conditionals — a converted element just needs
    // data-on-click and this handler fires it on Enter/Space.
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
        var start = e.target;
        if (!start || !start.closest) return;
        // Don't hijack typing inside form controls.
        var t = (start.tagName || '').toUpperCase();
        if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return;
        var el = start.closest('[data-on-click]');
        if (!el) return;
        // Native <button>/<a> already fire a click on Enter/Space (which the
        // click listener above catches); don't double-fire for them.
        var tag = (el.tagName || '').toUpperCase();
        if (tag === 'BUTTON' || tag === 'A') return;
        // If the element declares an explicit keydown action, let it own the key.
        if (el.getAttribute('data-on-keydown')) return;
        var fn = window.wbActions[el.getAttribute('data-on-click')];
        if (typeof fn !== 'function') return;
        if (e.key === ' ' || e.key === 'Spacebar') e.preventDefault();
        try {
            fn(el, e);
        } catch (err) {
            console.error('[wb-actions] keyboard-activate threw:', err);
        }
    });
})();
"""
