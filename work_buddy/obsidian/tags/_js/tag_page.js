// Get or create a "tag page" — an Obsidian note associated with a tag via alias.
// __TAG__ = tag name (without #)
// __ACTION__ = 'get' (return existing) or 'create' (create if missing)
return (async () => {
    const plugin = app.plugins.plugins['tag-wrangler'];
    if (!plugin) return {error: 'Tag Wrangler plugin not found'};

    const tag = '__TAG__';
    const action = '__ACTION__';

    // Check for existing tag page
    const existing = plugin.tagPage(tag);
    if (existing) {
        return {
            exists: true,
            path: existing.path,
            tag: '#' + tag
        };
    }

    if (action === 'get') {
        return {exists: false, tag: '#' + tag, path: null};
    }

    // Create a new tag page
    try {
        await plugin.createTagPage(tag, false);
        // After creation, find the new file
        const created = plugin.tagPage(tag);
        return {
            exists: true,
            created: true,
            path: created ? created.path : null,
            tag: '#' + tag
        };
    } catch (e) {
        return {error: 'Failed to create tag page: ' + e.message};
    }
})()
