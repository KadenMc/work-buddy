// Report the current embedding model configuration.
return (async () => {
    const env = window.smart_env;
    if (!env) return {error: 'No smart_env'};

    const em = env.smart_sources?.embed_model;
    if (!em) return {error: 'No embed_model'};

    const models = env.embedding_models;
    const itemKeys = Object.keys(models?.items || {});

    return {
        model_key: em.model_key || em.settings?.model_key,
        provider_key: em.provider_key || em.settings?.provider_key,
        dims: em.dims || em.settings?.dims,
        max_tokens: em.max_tokens || em.settings?.max_tokens,
        adapter_type: em.constructor?.name,
        iframe_id: em.iframe_id,
        state: em.state,
        model_loaded: em.model_loaded,
        model_count: itemKeys.length,
        sources_count: Object.keys(env.smart_sources?.items || {}).length,
        blocks_count: Object.keys(env.smart_blocks?.items || {}).length,
        is_pro: env.is_pro,
        plugin_states: env.plugin_states
    };
})()
