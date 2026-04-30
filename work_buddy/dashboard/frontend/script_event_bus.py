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


def _event_bus_script() -> str:
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

    // -- Smart per-panel refresh --------------------------------------
    //
    // The event bus's job, from the user's perspective, is to update
    // panels in real time WITHOUT destroying in-progress UI state
    // (textareas, scroll, drawers). The minimal policy that achieves
    // both:
    //
    //   1. Event arrives that affects panel X.
    //   2. If X is the active tab AND no input/textarea inside X is
    //      focused, run X's loader now.
    //   3. If X is active but an input is focused, defer the refresh
    //      until the user blurs it (focusout, debounced 200ms).
    //   4. If X is not the active tab, do nothing — switchTab(X) will
    //      run X's loader fresh on next visit.
    //
    // This is conservative compared to true per-card incremental DOM
    // mutations (which would require lifting renderGroupCard out of
    // the renderTriageReview closure). But it removes the chronic
    // refresh bug (no global panel rewrite while user types) and gives
    // the dashboard real-time updates everywhere except inside an
    // actively-edited form. Future work can swap loaders for per-card
    // mutators.
    const pendingPanels = new Set();

    function _activeTabName() {
        const t = document.querySelector('.tab-btn.active');
        return t ? t.dataset.tab : null;
    }

    function _focusedInsidePanel(panelName) {
        const panel = document.getElementById('panel-' + panelName);
        if (!panel) return false;
        const a = document.activeElement;
        if (!a || !panel.contains(a)) return false;
        const tag = a.tagName;
        return tag === 'INPUT' || tag === 'TEXTAREA' || a.isContentEditable;
    }

    function _smartRefresh(panelName) {
        if (_activeTabName() !== panelName) return;  // inactive: switchTab refreshes
        if (_focusedInsidePanel(panelName)) {
            pendingPanels.add(panelName);
            return;
        }
        pendingPanels.delete(panelName);
        const loaders = (typeof staticLoaders === 'object') ? staticLoaders : null;
        const loader = loaders && loaders[panelName];
        if (typeof loader === 'function') {
            try { loader(); }
            catch (e) { console.error('[event-bus] smart-refresh', panelName, 'threw:', e); }
        }
    }

    // Drain pending refreshes shortly after focus moves out of an
    // input. 200ms debounce gives focus shifts time to settle (e.g.
    // user moving from one textarea to another).
    let _focusoutTimer = null;
    document.addEventListener('focusout', () => {
        if (_focusoutTimer) clearTimeout(_focusoutTimer);
        _focusoutTimer = setTimeout(() => {
            for (const p of Array.from(pendingPanels)) {
                if (_activeTabName() === p && !_focusedInsidePanel(p)) {
                    _smartRefresh(p);
                }
            }
        }, 200);
    });

    const PANEL_FOR_EVENT = {
        'pool.entry_added':              'review',
        'pool.entry_state_changed':      'review',
        'pool.attraction_passes_bumped': 'review',
        'pool.forced_context_stored':    'review',
        'task.created':                  'tasks',
        'task.state_changed':            'tasks',
        'task.description_changed':      'tasks',
        'component.health_changed':      'settings',
        'component.preference_changed':  'settings',
        'llm.call_logged':               'costs',
    };
    for (const [eventType, panelName] of Object.entries(PANEL_FOR_EVENT)) {
        window.eventBus.on(eventType, () => _smartRefresh(panelName));
    }

    // Diagnostics handles for tests.
    window.eventBus._pendingPanels = () => new Set(pendingPanels);
    window.eventBus._smartRefresh = _smartRefresh;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _connect);
    } else {
        _connect();
    }
})();
"""
