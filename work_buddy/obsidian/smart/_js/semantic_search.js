// Semantic search: embed query, find nearest items.
// Params: __TEXT__, __LIMIT__, __COLLECTION__ (replaced by Python)
return (async () => {
    const env = window.smart_env;
    if (!env) return {error: 'No smart_env'};

    const em = env.smart_sources?.embed_model;
    if (!em) return {error: 'No embed_model'};

    // Clear stale queue
    for (const k of Object.keys(em.message_queue || {})) {
        em.message_queue[k]?.reject?.(new Error('stale'));
        delete em.message_queue[k];
    }

    // Embed the query
    let embedded;
    try {
        embedded = await em.embed('__TEXT__');
    } catch (e) {
        return {error: 'Embed failed: ' + e.message};
    }
    if (!embedded?.vec) return {error: 'embed returned no vec'};

    // Find nearest
    const collection = env['__COLLECTION__'];
    if (!collection?.entities_vector_adapter?.nearest) {
        return {error: 'Collection __COLLECTION__ has no nearest method'};
    }

    try {
        const results = await collection.entities_vector_adapter.nearest(embedded.vec, {});
        const limited = results.slice(0, __LIMIT__);
        return {
            results: limited.map(r => ({
                key: r.item?.key || r.key,
                score: r.score
            })),
            query_tokens: embedded.tokens
        };
    } catch (e) {
        return {error: 'Nearest failed: ' + e.message};
    }
})()
