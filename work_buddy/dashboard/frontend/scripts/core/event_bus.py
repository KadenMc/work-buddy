"""Browser-side EventSource client + per-event-type dispatcher.

Opens a single ``EventSource('/api/events')`` connection on page load
and routes incoming events to handlers registered by other frontend
modules:

    window.eventBus.on('pool.entry_added', (payload, evt) => { ... });
    window.eventBus.on('task.created', renderSingleTaskRow);
    window.eventBus.off('pool.entry_added', myHandler);

The connection-status indicator (``#event-bus-status`` in the header)
turns green on connect and red on disconnect. ``EventSource`` reconnects
automatically with exponential backoff; the bus does not replay events
from before the reconnect, so handlers should be idempotent (e.g.
``decorateCard`` is, by design).

Server-side counterpart: ``work_buddy/dashboard/events.py`` (in-process
bus) and the SSE generator in ``service.py`` (``_sse_stream``).
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Real-time event bus (server-sent events) ----
//
// Single EventSource connection per tab. Handlers are registered by
// per-domain modules (script_review, script_main task list, etc.) and
// are looked up by event_type. Handlers receive (payload, full_event).
//
// The bus does not replay events from before a reconnect; handlers
// must be idempotent and per-tab visibility refresh is handled by the
// `visibilitychange` listener in script_main.py.
(function() {
    // Global morphdom-replace helper. Used by every panel renderer
    // that needs to refresh content without destroying user state
    // (focused inputs, scroll, drilled-in <details>). Same semantics
    // as Phoenix LiveView / Hotwire: render fresh HTML, then diff
    // against the live DOM in place.
    //
    // Usage: ``window._wbMorphReplace(el, htmlString)``. Behaves like
    // ``el.innerHTML = htmlString`` when morphdom is unavailable.
    //
    // Used to obsolete each panel's wholesale ``container.innerHTML =``
    // rewrite — the user's typing, scroll, and <details> open state
    // survive automatically.
    window._wbMorphReplace = function(el, html) {
        if (!el) return;
        if (typeof window.morphdom !== 'function') {
            el.innerHTML = html;
            return;
        }
        const stage = document.createElement(el.tagName);
        stage.innerHTML = html;
        try {
            window.morphdom(el, stage, {
                childrenOnly: true,
                onBeforeElUpdated(fromEl, toEl) {
                    if (fromEl.tagName === 'INPUT' || fromEl.tagName === 'TEXTAREA') {
                        if (document.activeElement === fromEl) return false;
                        const v = (fromEl.value || '').trim();
                        if (v && (toEl.value || '').trim() === '') return false;
                    }
                    return !fromEl.isEqualNode(toEl);
                },
            });
        } catch (e) {
            console.error('[wbMorphReplace] threw, falling back to innerHTML:', e);
            el.innerHTML = html;
        }
    };

    const handlers = new Map();   // event_type -> Set<handler>
    let es = null;
    let reconnectCount = 0;

    function _setStatus(state, label) {
        const el = document.getElementById('event-bus-status');
        if (!el) return;
        el.classList.remove('connecting', 'connected', 'disconnected');
        el.classList.add(state);
        const dot = el.querySelector('.status-dot');
        if (dot) {
            dot.classList.remove('healthy', 'stopped', 'unhealthy');
            if (state === 'connected') dot.classList.add('healthy');
            else if (state === 'disconnected') dot.classList.add('unhealthy');
            else dot.classList.add('stopped');
        }
        el.lastChild.textContent = ' ' + (label || state);
    }

    function _dispatch(event) {
        const set = handlers.get(event.event_type);
        if (!set || set.size === 0) return;
        for (const fn of set) {
            try {
                fn(event.payload, event);
            } catch (err) {
                console.error('[event-bus] handler for', event.event_type, 'threw:', err);
            }
        }
    }

    function _connect() {
        if (es) try { es.close(); } catch (e) {}
        es = new EventSource('/api/events');

        es.addEventListener('open', () => {
            reconnectCount = 0;
            _setStatus('connected', 'live');
            console.log('[event-bus] connected');
        });

        es.addEventListener('error', (e) => {
            // EventSource flips readyState to CONNECTING and reconnects
            // automatically with browser-managed backoff. We just log
            // and update the indicator; do not call _connect() here.
            reconnectCount++;
            _setStatus('disconnected', 'reconnecting');
            if (reconnectCount === 1 || reconnectCount % 10 === 0) {
                console.warn('[event-bus] connection error (attempt', reconnectCount + ')');
            }
        });

        es.addEventListener('message', (ev) => {
            let evt;
            try {
                evt = JSON.parse(ev.data);
            } catch (err) {
                console.warn('[event-bus] non-JSON frame:', ev.data);
                return;
            }
            if (!evt || typeof evt.event_type !== 'string') return;
            _dispatch(evt);
        });
    }

    let lastHeartbeatTs = null;

    window.eventBus = {
        on(eventType, handler) {
            if (typeof handler !== 'function') {
                throw new TypeError('eventBus.on: handler must be a function');
            }
            if (!handlers.has(eventType)) handlers.set(eventType, new Set());
            handlers.get(eventType).add(handler);
            // Return an unsubscribe fn for convenience.
            return () => this.off(eventType, handler);
        },
        off(eventType, handler) {
            const set = handlers.get(eventType);
            if (set) set.delete(handler);
        },
        isConnected() {
            return es && es.readyState === EventSource.OPEN;
        },
        lastHeartbeat() { return lastHeartbeatTs; },
        // For diagnostics / heartbeat smoke-test.
        _eventSource: () => es,
        _handlers: () => handlers,
    };

    // Default heartbeat handler: silently track the last heartbeat
    // timestamp. The SSE endpoint also emits keepalive comments, but
    // those don't surface to JS. This handler gives the page a JS-level
    // liveness signal it can read via window.eventBus.lastHeartbeat().
    window.eventBus.on('bus.heartbeat', (payload, evt) => {
        lastHeartbeatTs = evt.ts;
    });

    // -- Per-card mutation contract ----------------------------------
    //
    // SSE handlers MUST mutate single rows. Calling a panel-wide
    // loader (loadReview, loadTasks, loadSettings, loadCosts) from
    // any handler is a regression — see architecture/event-bus and
    // the regression test ``test_no_wholesale_loader_calls_in_event_handlers``.
    //
    // Each panel exposes a handle on ``window.<name>Surface`` with
    // per-row mutators (appendCard / removeCard / updateCard etc.).
    // morphdom is the surgical-update primitive; per-card animation
    // cancellation, focus capture, drag-state nulling, and state-dict
    // pruning are responsibilities of the handle, not the dispatcher.
    //
    // Ordering protection: in-process events have ~0ms delivery
    // latency; cross-process events flow through the messaging-bridge
    // poll (~500ms). A pool.entry_state_changed for a just-submitted
    // entry can therefore arrive before the corresponding
    // pool.entry_added (state change in-process; add cross-process
    // through the bridge). The handles' removeCard records unknown
    // keys in their own ``_pendingRemovals`` Set; appendCard checks
    // that set first and discards a late add for an already-resolved
    // entry. The dispatcher just routes events; it does NOT keep its
    // own pending state.

    function _withSurface(name, fn) {
        // Defensive helper: call fn(surface) only when the surface is
        // mounted. Unmounted surfaces drop events; switchTab will
        // rebuild fresh state on next visit.
        const s = window[name];
        if (!s || typeof s.isMounted !== 'function' || !s.isMounted()) return;
        try { fn(s); }
        catch (e) { console.error('[event-bus] surface', name, 'mutator threw:', e); }
    }

    const TERMINAL_POOL_STATES = ['reviewed', 'quarantined', 'stale', 'dropped'];

    window.eventBus.on('pool.entry_added', (p) => {
        if (!p || !p.run_id || !p.item_id) return;
        _withSurface('reviewSurface', (s) => {
            if (p.group && typeof s.appendCard === 'function') {
                s.appendCard(p.group);
            } else if (typeof s.removeCard === 'function') {
                // Skinny add (no group composed): record in
                // _pendingRemovals via removeCard's key path so a
                // subsequent state-change can no-op cleanly.
                // Otherwise this is a silent drop — appropriate when
                // the server couldn't compose the rendered group.
            }
        });
    });
    window.eventBus.on('pool.entry_state_changed', (p) => {
        if (!p || !p.run_id || !p.item_id) return;
        _withSurface('reviewSurface', (s) => {
            if (TERMINAL_POOL_STATES.includes(p.state)) {
                if (typeof s.removeCard === 'function') s.removeCard(p.run_id, p.item_id);
            } else if (typeof s.updateCard === 'function' && p.group) {
                s.updateCard(p.run_id, p.item_id, p.group);
            }
        });
    });
    window.eventBus.on('pool.attraction_passes_bumped', (p) => {
        if (!p || !p.run_id || !p.item_id) return;
        _withSurface('reviewSurface', (s) => {
            if (typeof s.bumpAttractionPasses === 'function') {
                s.bumpAttractionPasses(p.run_id, p.item_id, p.count);
            }
        });
    });
    window.eventBus.on('pool.forced_context_stored', (p) => {
        if (!p || !p.run_id || !p.item_id) return;
        _withSurface('reviewSurface', (s) => {
            if (typeof s.setForcedContextStored === 'function') {
                s.setForcedContextStored(p.run_id, p.item_id);
            }
        });
    });

    // Tasks / Settings / Costs surfaces use a morphdom-merge refresh
    // pattern (Phoenix LiveView convention): the panel's own
    // ``surface.refresh()`` re-fetches its API endpoint and merges
    // fresh HTML into the live container via ``window._wbMorphReplace``.
    // User state (focused inputs, scroll, drilled-in <details>) is
    // preserved natively by morphdom — no panel-wide wipe ever occurs.
    //
    // Multiple events arriving in a burst are coalesced via a 250 ms
    // debounce per surface so a probe_all triggering 8+
    // ``component.health_changed`` events maps to ONE refresh.
    const _refreshTimers = new Map();
    function _refreshSoon(surfaceName) {
        if (_refreshTimers.has(surfaceName)) {
            clearTimeout(_refreshTimers.get(surfaceName));
        }
        const t = setTimeout(() => {
            _refreshTimers.delete(surfaceName);
            _withSurface(surfaceName, (s) => {
                if (typeof s.refresh === 'function') s.refresh();
            });
        }, 250);
        _refreshTimers.set(surfaceName, t);
    }

    window.eventBus.on('task.created',             () => _refreshSoon('tasksSurface'));
    window.eventBus.on('task.state_changed',       () => _refreshSoon('tasksSurface'));
    window.eventBus.on('task.description_changed', () => _refreshSoon('tasksSurface'));

    window.eventBus.on('component.health_changed',     () => _refreshSoon('settingsSurface'));
    window.eventBus.on('component.preference_changed', () => _refreshSoon('settingsSurface'));

    window.eventBus.on('llm.call_logged', () => _refreshSoon('costsSurface'));

    // Jobs tab: refresh on dashboard-side create (immediate, repaints the
    // pending banner) and on sidecar hot-reload (jobs appear in /api/state
    // for the first time, banner auto-clears).
    window.eventBus.on('user_job.created',  () => _refreshSoon('jobsSurface'));
    window.eventBus.on('cron.hot_reload',   () => _refreshSoon('jobsSurface'));

    // Diagnostics handles for tests.
    window.eventBus._panelHandlers = () => ({
        'pool.entry_added':              'reviewSurface.appendCard',
        'pool.entry_state_changed':      'reviewSurface.removeCard|updateCard',
        'pool.attraction_passes_bumped': 'reviewSurface.bumpAttractionPasses',
        'pool.forced_context_stored':    'reviewSurface.setForcedContextStored',
        'task.created':                  'tasksSurface.refresh (morphdom)',
        'task.state_changed':            'tasksSurface.refresh (morphdom)',
        'task.description_changed':      'tasksSurface.refresh (morphdom)',
        'component.health_changed':      'settingsSurface.refresh (morphdom)',
        'component.preference_changed':  'settingsSurface.refresh (morphdom)',
        'llm.call_logged':               'costsSurface.refresh (morphdom)',
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _connect);
    } else {
        _connect();
    }
})();
"""
