// Embed a single text string. Returns {vec, tokens} or {error}.
// Param: __TEXT__ (replaced by Python)
return (() => {
    const em = window.smart_env?.smart_sources?.embed_model;
    if (!em) return {error: 'No embed_model on smart_env'};

    // Clear stale queue
    for (const k of Object.keys(em.message_queue || {})) {
        em.message_queue[k]?.reject?.(new Error('stale'));
        delete em.message_queue[k];
    }

    return em.embed('__TEXT__').then(r => {
        if (!r?.vec) return {error: 'embed returned no vec'};
        return {vec: Array.from(r.vec), tokens: r.tokens};
    }).catch(e => ({error: e.message}));
})()
