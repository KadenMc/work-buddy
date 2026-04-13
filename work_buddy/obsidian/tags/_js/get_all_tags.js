// Get all vault tags with counts, hierarchy info, and file associations.
// __INCLUDE_FILES__ = 'true' or 'false'
// __LIMIT__ = max files per tag when include_files is true
return (async () => {
    const includeFiles = __INCLUDE_FILES__;
    const fileLimit = __LIMIT__;
    const rawTags = app.metadataCache.getTags();
    const result = [];

    for (const [tag, count] of Object.entries(rawTags)) {
        const entry = {tag, count};
        const parts = tag.replace(/^#/, '').split('/');
        entry.depth = parts.length;
        entry.parent = parts.length > 1 ? '#' + parts.slice(0, -1).join('/') : null;

        if (includeFiles) {
            const files = [];
            for (const f of app.vault.getMarkdownFiles()) {
                if (files.length >= fileLimit) break;
                const fc = app.metadataCache.getFileCache(f);
                if (!fc) continue;
                let found = false;
                // Inline tags
                if (fc.tags) {
                    for (const t of fc.tags) {
                        if (t.tag.toLowerCase() === tag.toLowerCase()) { found = true; break; }
                    }
                }
                // Frontmatter tags
                if (!found && fc.frontmatter?.tags) {
                    for (const t of fc.frontmatter.tags) {
                        const normalized = t.startsWith('#') ? t : '#' + t;
                        if (normalized.toLowerCase() === tag.toLowerCase()) { found = true; break; }
                    }
                }
                if (found) files.push(f.path);
            }
            entry.files = files;
        }
        result.push(entry);
    }

    return result;
})()
