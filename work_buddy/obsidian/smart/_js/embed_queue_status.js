// Check embedding queue state on smart_sources and smart_blocks.
return (() => {
    const env = window.smart_env;
    if (!env) return {error: 'No smart_env'};
    const src = env.smart_sources;
    const blk = env.smart_blocks;
    return {
        sources_queue_size: src?._embed_queue?.length || 0,
        blocks_queue_size: blk?._embed_queue?.length || 0,
        total_queued: (src?._embed_queue?.length || 0) + (blk?._embed_queue?.length || 0),
        process_available: typeof src?.process_embed_queue === 'function',
        model_state: src?.embed_model?.state || 'unknown',
        model_loaded: !!src?.embed_model?.model_loaded
    };
})()
