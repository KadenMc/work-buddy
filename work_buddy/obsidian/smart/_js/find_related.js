// Find items semantically similar to a specific vault file.
// Params: __KEY__, __LIMIT__, __COLLECTION__ (replaced by Python)
return (async () => {
    const env = window.smart_env;
    if (!env) return {error: 'No smart_env'};

    const key = '__KEY__';
    const item = env.smart_sources?.get?.(key) || env.smart_sources?.items?.[key];
    if (!item) return {error: 'Source not found: ' + key};

    const vec = item.data?.embeddings?.['TaylorAI/bge-micro-v2']?.vec;
    if (!vec) return {error: 'No embedding for: ' + key};

    const collection = env['__COLLECTION__'];
    if (!collection?.entities_vector_adapter?.nearest) {
        return {error: 'Collection __COLLECTION__ has no nearest method'};
    }

    try {
        const results = await collection.entities_vector_adapter.nearest(vec, {});
        // Filter out the query item itself
        const filtered = results.filter(r => (r.item?.key || r.key) !== key);
        return {
            results: filtered.slice(0, __LIMIT__).map(r => ({
                key: r.item?.key || r.key,
                score: r.score
            }))
        };
    } catch (e) {
        return {error: 'find_related failed: ' + e.message};
    }
})()
