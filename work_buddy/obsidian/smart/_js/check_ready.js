// Check if SmartEnv is fully loaded and ready for use.
// Also reports memory stats when available.
return (async () => {
    const env = window.smart_env;
    if (!env) return {ready: false, reason: 'no smart_env on window'};

    const result = {
        ready: env.state === 'loaded',
        state: env.state,
        plugins_loaded: Object.entries(env.plugin_states || {}).filter(([k,v]) => v === 'loaded').length,
        plugins_total: Object.keys(env.plugin_states || {}).length,
        sources_count: Object.keys(env.smart_sources?.items || {}).length,
        blocks_count: Object.keys(env.smart_blocks?.items || {}).length,
        has_embed_model: !!env.smart_sources?.embed_model,
        iframe_ready: !!document.getElementById('smart_embed_iframe')?.contentWindow,
        is_pro: env.is_pro
    };

    // Memory stats (Electron/Node.js)
    try {
        if (typeof process !== 'undefined' && process.memoryUsage) {
            const mem = process.memoryUsage();
            result.memory = {
                heap_used_mb: Math.round(mem.heapUsed / 1048576),
                heap_total_mb: Math.round(mem.heapTotal / 1048576),
                rss_mb: Math.round(mem.rss / 1048576),
                external_mb: Math.round(mem.external / 1048576)
            };
        }
    } catch (e) {}

    // Performance memory (Chrome/Electron)
    try {
        if (performance?.memory) {
            result.performance_memory = {
                js_heap_used_mb: Math.round(performance.memory.usedJSHeapSize / 1048576),
                js_heap_total_mb: Math.round(performance.memory.totalJSHeapSize / 1048576),
                js_heap_limit_mb: Math.round(performance.memory.jsHeapSizeLimit / 1048576)
            };
        }
    } catch (e) {}

    return result;
})()
