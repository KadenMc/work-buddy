// Get all tags for a specific file, from both inline tags and frontmatter.
// __FILE_PATH__ = vault-relative file path
return (async () => {
    const filePath = '__FILE_PATH__';
    const file = app.vault.getAbstractFileByPath(filePath);
    if (!file) return {error: 'File not found: ' + filePath};

    const fc = app.metadataCache.getFileCache(file);
    if (!fc) return {error: 'No cache for file: ' + filePath};

    const tags = [];

    // Inline tags (with position info)
    if (fc.tags) {
        for (const t of fc.tags) {
            tags.push({
                tag: t.tag,
                source: 'inline',
                line: t.position.start.line,
                col: t.position.start.col
            });
        }
    }

    // Frontmatter tags
    if (fc.frontmatter?.tags) {
        for (const t of fc.frontmatter.tags) {
            tags.push({
                tag: t.startsWith('#') ? t : '#' + t,
                source: 'frontmatter',
                line: null,
                col: null
            });
        }
    }

    // Frontmatter tag (singular)
    if (fc.frontmatter?.tag && typeof fc.frontmatter.tag === 'string') {
        const t = fc.frontmatter.tag;
        tags.push({
            tag: t.startsWith('#') ? t : '#' + t,
            source: 'frontmatter',
            line: null,
            col: null
        });
    }

    return {path: filePath, tags};
})()
