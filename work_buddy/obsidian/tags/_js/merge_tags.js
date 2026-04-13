// Merge one tag into another by renaming source -> target.
// This is effectively a rename that consolidates two tags into one.
// __SOURCE_TAG__ = tag to merge FROM (without #) — will be removed
// __TARGET_TAG__ = tag to merge INTO (without #) — will remain
return (async () => {
    const plugin = app.plugins.plugins['tag-wrangler'];
    if (!plugin) return {error: 'Tag Wrangler plugin not found'};

    const sourceTag = '__SOURCE_TAG__';
    const targetTag = '__TARGET_TAG__';

    if (!sourceTag || !targetTag) return {error: 'Both source and target tag names required'};
    if (sourceTag === targetTag) return {error: 'Source and target tags are identical'};

    // Count affected files before merge
    const allTags = app.metadataCache.getTags();
    const sourceHash = '#' + sourceTag;
    const sourceCount = allTags[sourceHash] || 0;
    const targetHash = '#' + targetTag;
    const targetCount = allTags[targetHash] || 0;

    if (sourceCount === 0) return {error: 'Source tag not found: ' + sourceHash};

    try {
        await plugin.rename(sourceTag, targetTag);
        return {
            success: true,
            source_tag: sourceHash,
            target_tag: targetHash,
            source_occurrences_merged: sourceCount,
            target_original_count: targetCount
        };
    } catch (e) {
        return {error: 'Merge failed: ' + e.message};
    }
})()
