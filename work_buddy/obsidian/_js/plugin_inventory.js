// List all loaded Obsidian plugins with version info.
return (() => {
    const plugins = app?.plugins?.plugins;
    if (!plugins) return {error: 'No app.plugins.plugins'};

    const result = [];
    for (const [id, plugin] of Object.entries(plugins)) {
        const m = plugin.manifest || {};
        result.push({
            id: id,
            name: m.name || id,
            version: m.version || 'unknown',
            author: m.author || 'unknown'
        });
    }
    result.sort((a, b) => a.id.localeCompare(b.id));
    return {plugins: result, count: result.length};
})()
