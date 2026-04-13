// Embed multiple texts. Returns [{vec, tokens}, ...] or {error}.
// Param: __INPUTS_JSON__ (replaced by Python — JSON array of strings)
return (() => {
    const em = window.smart_env?.smart_sources?.embed_model;
    if (!em) return {error: 'No embed_model on smart_env'};

    // Clear stale queue
    for (const k of Object.keys(em.message_queue || {})) {
        em.message_queue[k]?.reject?.(new Error('stale'));
        delete em.message_queue[k];
    }

    const texts = __INPUTS_JSON__;
    const inputs = texts.map(t => ({embed_input: t}));
    return em.embed_batch(inputs).then(results =>
        results.map(r => ({vec: Array.from(r.vec), tokens: r.tokens}))
    ).catch(e => ({error: e.message}));
})()
