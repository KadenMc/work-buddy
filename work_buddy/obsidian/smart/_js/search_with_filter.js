// Semantic search with folder/tag/exclusion filters.
// Params: __TEXT__, __LIMIT__, __COLLECTION__, __FILTER_JSON__ (replaced by Python)
// __FILTER_JSON__ = {folders: [...], tags: [...], exclude_folders: [...], exclude_keys: [...]}
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

    const collection = env['__COLLECTION__'];
    if (!collection?.entities_vector_adapter?.nearest) {
        return {error: 'Collection __COLLECTION__ has no nearest method'};
    }

    // Get all nearest (unfiltered — we filter after)
    let results;
    try {
        results = await collection.entities_vector_adapter.nearest(embedded.vec, {});
    } catch (e) {
        return {error: 'Nearest failed: ' + e.message};
    }

    // Apply filters
    const filter = __FILTER_JSON__;
    const filtered = results.filter(r => {
        const key = r.item?.key || r.key || '';
        const path = key.split('#')[0];

        if (filter.folders?.length) {
            if (!filter.folders.some(f => path.startsWith(f))) return false;
        }
        if (filter.exclude_folders?.length) {
            if (filter.exclude_folders.some(f => path.startsWith(f))) return false;
        }
        if (filter.exclude_keys?.length) {
            if (filter.exclude_keys.includes(key)) return false;
        }
        // Tag filtering requires item metadata
        if (filter.tags?.length) {
            const tags = r.item?.data?.tags || [];
            if (!filter.tags.some(t => tags.includes(t))) return false;
        }
        return true;
    });

    return {
        results: filtered.slice(0, __LIMIT__).map(r => ({
            key: r.item?.key || r.key,
            score: r.score
        })),
        total_before_filter: results.length,
        total_after_filter: filtered.length,
        query_tokens: embedded.tokens
    };
})()
