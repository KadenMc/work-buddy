// Invoke the lookup_context Smart Action.
// Params: __HYPOTHETICALS_JSON__, __LIMIT__ (replaced by Python)
return (async () => {
    const env = window.smart_env;
    if (!env) return {error: 'No smart_env'};

    // Clear stale embed queue
    const em = env.smart_sources?.embed_model;
    for (const k of Object.keys(em?.message_queue || {})) {
        em.message_queue[k]?.reject?.(new Error('stale'));
        delete em.message_queue[k];
    }

    // Load action module
    const action = env.smart_actions?.items?.['lookup_context'];
    if (!action?._action_adapter) return {error: 'No lookup_context action adapter'};
    if (!action._action_adapter.module) {
        try { await action._action_adapter.load(); }
        catch (e) { return {error: 'Module load failed: ' + e.message}; }
    }
    const fn = action._action_adapter.module?.lookup_context;
    if (typeof fn !== 'function') return {error: 'lookup_context function not found in module'};

    // Invoke
    try {
        const contextKey = await fn.call(action, {
            hypotheticals: __HYPOTHETICALS_JSON__,
            limit: __LIMIT__,
            env: env
        });

        if (typeof contextKey !== 'string') {
            return {error: 'Unexpected return type: ' + typeof contextKey, value: String(contextKey)};
        }

        // Read the created context
        const ctx = env.smart_contexts.items[contextKey];
        if (!ctx) return {context_key: contextKey, items: [], note: 'context created but not found in items'};

        const items = ctx.data?.context_items || {};
        return {
            context_key: contextKey,
            items: Object.entries(items).map(([k, v]) => ({key: k, score: v?.score}))
        };
    } catch (e) {
        return {error: 'lookup_context failed: ' + e.message};
    }
})()
