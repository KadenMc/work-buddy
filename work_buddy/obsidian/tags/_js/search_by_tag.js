// Find all files containing a specific tag (exact or prefix match).
// __TAG__ = tag to search for (with or without #)
// __MODE__ = 'exact' or 'prefix'
// __LIMIT__ = max results
return (async () => {
    const tag = '__TAG__'.startsWith('#') ? '__TAG__' : '#__TAG__';
    const mode = '__MODE__';
    const limit = __LIMIT__;
    const results = [];

    for (const f of app.vault.getMarkdownFiles()) {
        if (results.length >= limit) break;
        const fc = app.metadataCache.getFileCache(f);
        if (!fc) continue;

        let matched = false;
        const matchedTags = [];

        // Check inline tags
        if (fc.tags) {
            for (const t of fc.tags) {
                const matches = mode === 'exact'
                    ? t.tag.toLowerCase() === tag.toLowerCase()
                    : t.tag.toLowerCase().startsWith(tag.toLowerCase());
                if (matches) {
                    matched = true;
                    if (!matchedTags.includes(t.tag)) matchedTags.push(t.tag);
                }
            }
        }

        // Check frontmatter tags
        if (fc.frontmatter?.tags) {
            for (const t of fc.frontmatter.tags) {
                const normalized = t.startsWith('#') ? t : '#' + t;
                const matches = mode === 'exact'
                    ? normalized.toLowerCase() === tag.toLowerCase()
                    : normalized.toLowerCase().startsWith(tag.toLowerCase());
                if (matches) {
                    matched = true;
                    if (!matchedTags.includes(normalized)) matchedTags.push(normalized);
                }
            }
        }

        if (matched) {
            results.push({path: f.path, matched_tags: matchedTags});
        }
    }

    return {query: tag, mode, count: results.length, files: results};
})()
