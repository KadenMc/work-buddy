// Check if Tag Wrangler plugin is loaded and functional.
return (async () => {
    const plugin = app.plugins.plugins['tag-wrangler'];
    if (!plugin) {
        return {ready: false, reason: 'Tag Wrangler plugin not found'};
    }
    if (!plugin._loaded) {
        return {ready: false, reason: 'Tag Wrangler plugin not loaded'};
    }
    const proto = Object.getPrototypeOf(plugin);
    const hasRename = typeof proto.rename === 'function';
    const tagCount = Object.keys(app.metadataCache.getTags()).length;
    return {
        ready: true,
        version: plugin.manifest.version,
        has_rename: hasRename,
        tag_count: tagCount
    };
})()
