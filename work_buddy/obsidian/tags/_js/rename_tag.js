// Rename a tag across the entire vault using Tag Wrangler's rename method.
// This modifies files! The plugin handles all occurrences in both inline and frontmatter.
// __OLD_TAG__ = tag to rename (without #)
// __NEW_TAG__ = new tag name (without #)
return (async () => {
    const plugin = app.plugins.plugins['tag-wrangler'];
    if (!plugin) return {error: 'Tag Wrangler plugin not found'};

    const oldTag = '__OLD_TAG__';
    const newTag = '__NEW_TAG__';

    if (!oldTag || !newTag) return {error: 'Both old and new tag names required'};
    if (oldTag === newTag) return {error: 'Old and new tag names are identical'};

    // Verify old tag exists
    const allTags = app.metadataCache.getTags();
    const oldTagHash = '#' + oldTag;
    const found = Object.keys(allTags).some(t => t.toLowerCase() === oldTagHash.toLowerCase());
    if (!found) return {error: 'Tag not found: #' + oldTag};

    try {
        await plugin.rename(oldTag, newTag);
        return {success: true, old_tag: '#' + oldTag, new_tag: '#' + newTag};
    } catch (e) {
        return {error: 'Rename failed: ' + e.message};
    }
})()
