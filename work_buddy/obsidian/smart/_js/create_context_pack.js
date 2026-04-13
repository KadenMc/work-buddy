// Create a SmartContext pack from item keys.
// Param: __ITEM_KEYS_JSON__ (replaced by Python)
return (async () => {
    const env = window.smart_env;
    if (!env?.smart_contexts) return {error: 'No smart_contexts on env'};

    try {
        const ctx = env.smart_contexts.new_context({}, {add_items: __ITEM_KEYS_JSON__});
        if (!ctx) return {error: 'new_context returned null'};
        return {
            context_key: ctx.key,
            item_count: Object.keys(ctx.data?.context_items || {}).length
        };
    } catch (e) {
        return {error: 'create_context_pack failed: ' + e.message};
    }
})()
