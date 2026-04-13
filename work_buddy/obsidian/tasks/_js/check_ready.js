// Check if the Tasks plugin cache is loaded and warm.
return (async () => {
    const plugin = app.plugins.plugins['obsidian-tasks-plugin'];
    if (!plugin) return {ready: false, reason: 'Tasks plugin not found'};

    const cache = plugin.cache;
    if (!cache) return {ready: false, reason: 'No cache object'};

    return {
        ready: cache.state === 'Warm',
        state: cache.state,
        task_count: cache.tasks ? cache.tasks.length : 0,
        loaded_after_first_resolve: cache.loadedAfterFirstResolve || false,
        version: plugin.manifest.version
    };
})()
