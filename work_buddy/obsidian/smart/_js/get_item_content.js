// Get content for a SmartSource or SmartBlock item.
// Param: __KEY__ (replaced by Python)
return (async () => {
    const env = window.smart_env;
    if (!env) return {error: 'No smart_env'};

    const key = '__KEY__';

    // Try smart_blocks first (more specific), then smart_sources
    let item = env.smart_blocks?.get?.(key);
    let collection = 'smart_blocks';
    if (!item) {
        item = env.smart_sources?.get?.(key);
        collection = 'smart_sources';
    }
    if (!item) return {error: 'Item not found: ' + key};

    // Try to get content via various methods
    let content = null;
    if (typeof item.get_as_context === 'function') {
        try { content = await item.get_as_context(); } catch (e) {}
    }
    if (!content && typeof item.read === 'function') {
        try { content = await item.read(); } catch (e) {}
    }
    if (!content && item.data?.content) {
        content = item.data.content;
    }
    // Fallback: read file from vault for sources
    if (!content && item.data?.path) {
        try {
            const file = app.vault.getAbstractFileByPath(item.data.path);
            if (file) content = await app.vault.cachedRead(file);
        } catch (e) {}
    }

    return {
        key: key,
        collection: collection,
        path: item.data?.path || item.path || key,
        has_content: !!content,
        content: content ? content.slice(0, 50000) : null,
        blocks: item.data?.blocks
    };
})()
