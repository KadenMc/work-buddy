"""Frontend half of the agent ↔ dashboard form bridge.

Exposes ``window.wbFormBridge``. Each form (Jobs Add-job today;
contracts / projects / etc. tomorrow) calls ``register(formId, ...)``
once with its handlers; the bridge listens for ``dashboard.form.*``
events on the SSE bus and dispatches to the matching handler.

The point of this module is that tab-specific code never directly
subscribes to the bus for form events, and the brief never names
specific input ids. Both flow through the schema declared in
``work_buddy/dashboard/forms_jobs.py`` (and siblings).

Step 3 of the bridge build wires ``field_set`` and ``open``. Step 4
will add ``submit`` (rendezvous-backed) and ``get_state``; the bridge
already POSTs results to ``/api/dashboard/interact/result/<id>`` for
those actions when they arrive.
"""

from __future__ import annotations


def script() -> str:
    return r"""
// ---- Form bridge — agent-driven form interaction ----
//
// Per-form registrations live in window.wbFormBridge. Each form
// supplies handlers for the actions it supports; the bridge subscribes
// to the corresponding event types once and routes by form_id.
(function () {
    const _registry = {};  // form_id -> { fieldHandlers, submitHandler, openHandler, getStateHandler }

    window.wbFormBridge = {
        register(formId, handlers) {
            if (!formId) {
                console.warn('wbFormBridge.register: form_id required');
                return;
            }
            _registry[formId] = handlers || {};
        },
        registered(formId) { return !!_registry[formId]; },
    };

    function _entry(formId) {
        const e = _registry[formId];
        if (!e) {
            console.warn('wbFormBridge: no handlers registered for form_id', formId);
        }
        return e;
    }

    // Postback for rendezvous-backed actions (form_submit, form_get_state).
    // Step 4 of the bridge build introduces these on the server side; the
    // frontend already supports them so step 4 is purely additive.
    async function _postResult(requestId, payload) {
        if (!requestId) return;
        try {
            await fetch('/api/dashboard/interact/result/' + encodeURIComponent(requestId), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload || {}),
            });
        } catch (err) {
            console.warn('wbFormBridge: result POST failed', err);
        }
    }

    function _wireWhenBusReady() {
        if (!window.eventBus || typeof window.eventBus.on !== 'function') {
            // event_bus.py runs first per the load order, but be defensive
            // in case load order ever shifts.
            return setTimeout(_wireWhenBusReady, 50);
        }

        // dashboard.form.field_set { form_id, field, value }
        window.eventBus.on('dashboard.form.field_set', (payload) => {
            if (!payload) return;
            const entry = _entry(payload.form_id);
            if (!entry || !entry.fieldHandlers) return;
            const handler = entry.fieldHandlers[payload.field];
            if (!handler) {
                console.warn(
                    'wbFormBridge: no field handler for',
                    payload.form_id, '.', payload.field
                );
                return;
            }
            try {
                handler(payload.value);
            } catch (err) {
                console.error('wbFormBridge field handler threw:', err);
            }
        });

        // dashboard.form.open { form_id }
        window.eventBus.on('dashboard.form.open', (payload) => {
            if (!payload) return;
            const entry = _entry(payload.form_id);
            if (!entry || !entry.openHandler) return;
            try { entry.openHandler(); }
            catch (err) { console.error('wbFormBridge open handler threw:', err); }
        });

        // dashboard.form.cancel { form_id } — agent-driven abort, used
        // when the user explicitly opts out of the chat-walkthrough.
        window.eventBus.on('dashboard.form.cancel', (payload) => {
            if (!payload) return;
            const entry = _entry(payload.form_id);
            if (!entry || !entry.cancelHandler) return;
            try { entry.cancelHandler(); }
            catch (err) { console.error('wbFormBridge cancel handler threw:', err); }
        });

        // dashboard.form.submit { form_id, request_id }
        window.eventBus.on('dashboard.form.submit', async (payload) => {
            if (!payload) return;
            const entry = _entry(payload.form_id);
            if (!entry || !entry.submitHandler) {
                _postResult(payload.request_id, {
                    ok: false,
                    error: 'no submit handler registered for ' + payload.form_id,
                });
                return;
            }
            let result;
            try {
                result = await entry.submitHandler();
            } catch (err) {
                result = { ok: false, error: 'submit handler threw: ' + err };
            }
            // Normalize to { ok, error?, errors_by_field? } so the
            // capability's caller always receives the same shape.
            if (result && typeof result === 'object' && 'ok' in result) {
                _postResult(payload.request_id, result);
            } else if (result && result.success != null) {
                // Allow handlers to return the existing /api/user_jobs
                // shape (success, error, ...) without forcing them to
                // rewrite. Translate here.
                _postResult(payload.request_id, {
                    ok: !!result.success,
                    error: result.error || '',
                    raw: result,
                });
            } else {
                _postResult(payload.request_id, { ok: true });
            }
        });

        // dashboard.form.get_state { form_id, request_id }
        window.eventBus.on('dashboard.form.get_state', async (payload) => {
            if (!payload) return;
            const entry = _entry(payload.form_id);
            if (!entry || !entry.getStateHandler) {
                _postResult(payload.request_id, {
                    ok: false,
                    error: 'no get_state handler registered for ' + payload.form_id,
                });
                return;
            }
            let fields;
            try { fields = await entry.getStateHandler(); }
            catch (err) {
                _postResult(payload.request_id, {
                    ok: false, error: 'get_state handler threw: ' + err,
                });
                return;
            }
            _postResult(payload.request_id, { ok: true, fields: fields || {} });
        });
    }

    _wireWhenBusReady();
})();
"""
