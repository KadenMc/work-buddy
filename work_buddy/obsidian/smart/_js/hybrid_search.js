// Hybrid search: combine semantic (Smart Connections) and lexical (Omnisearch).
// Params: __TEXT__, __LIMIT__ (replaced by Python)
// Returns results with reciprocal rank fusion scoring.
return (async () => {
    const env = window.smart_env;
    if (!env) return {error: 'No smart_env'};

    const em = env.smart_sources?.embed_model;
    if (!em) return {error: 'No embed_model'};

    const omni = globalThis.omnisearch || app?.plugins?.plugins?.omnisearch?.api;

    // Clear embed queue
    for (const k of Object.keys(em.message_queue || {})) {
        em.message_queue[k]?.reject?.(new Error('stale'));
        delete em.message_queue[k];
    }

    // Run semantic and lexical in parallel
    const query = '__TEXT__';
    const limit = __LIMIT__;
    const K = 60; // RRF constant

    const [semanticResults, lexicalResults] = await Promise.all([
        (async () => {
            try {
                const embedded = await em.embed(query);
                if (!embedded?.vec) return [];
                const results = await env.smart_blocks.entities_vector_adapter.nearest(embedded.vec, {});
                return results.slice(0, limit * 3);
            } catch (e) { return []; }
        })(),
        (async () => {
            if (!omni?.search) return [];
            try {
                const results = await omni.search(query);
                return results.slice(0, limit * 3);
            } catch (e) { return []; }
        })()
    ]);

    // Reciprocal Rank Fusion
    const scores = {};
    semanticResults.forEach((r, i) => {
        const key = r.item?.key || r.key;
        // Normalize block keys to source paths for dedup
        const path = key.split('#')[0];
        if (!scores[path]) scores[path] = {path, semantic_rank: i + 1, semantic_score: r.score, rrf: 0, block_key: key};
        scores[path].rrf += 1 / (K + i + 1);
    });
    lexicalResults.forEach((r, i) => {
        const path = r.path;
        if (!scores[path]) scores[path] = {path, rrf: 0};
        scores[path].lexical_rank = i + 1;
        scores[path].lexical_score = r.score;
        scores[path].excerpt = (r.excerpt || '').slice(0, 200);
        scores[path].rrf += 1 / (K + i + 1);
    });

    const fused = Object.values(scores).sort((a, b) => b.rrf - a.rrf).slice(0, limit);
    return {
        results: fused,
        semantic_count: semanticResults.length,
        lexical_count: lexicalResults.length
    };
})()
