// Find a task in the Tasks plugin cache by ID or description match.
// Placeholders: __TASK_ID__, __DESC_MATCH__
return (async () => {
    const cache = app.plugins.plugins['obsidian-tasks-plugin'].cache;
    if (!cache || cache.state !== 'Warm') return {error: 'Cache not warm', state: cache?.state};

    const taskId = '__TASK_ID__';
    const descMatch = '__DESC_MATCH__';
    let found = null;

    // Try ID match first (looks for 🆔 <id> in the task line)
    if (taskId) {
        const idPattern = '\u{1F194} ' + taskId;
        found = cache.tasks.find(t => t.description.includes(idPattern) || t.originalMarkdown.includes(idPattern));
    }

    // Fall back to description substring match
    if (!found && descMatch) {
        const lower = descMatch.toLowerCase();
        const matches = cache.tasks.filter(t => t.description.toLowerCase().includes(lower));
        if (matches.length === 1) {
            found = matches[0];
        } else if (matches.length > 1) {
            return {
                error: 'ambiguous_match',
                match_count: matches.length,
                previews: matches.slice(0, 5).map(t => t.description.substring(0, 80))
            };
        }
    }

    if (!found) return {found: false};

    // Extract state tag
    let stateTag = null;
    const stateMatch = found.description.match(/#tasker\/state\/(\w+)/);
    if (stateMatch) stateTag = stateMatch[1];

    // Extract urgency tag
    let urgencyTag = null;
    const urgencyMatch = found.description.match(/#tasker\/urgency\/(\w+)/);
    if (urgencyMatch) urgencyTag = urgencyMatch[1];

    return {
        found: true,
        description: found.description,
        original_markdown: found.originalMarkdown,
        line_number: found.taskLocation?._lineNumber || null,
        file: found.taskLocation?._tasksFile?._path || null,
        status_type: found.status?.type || null,
        status_symbol: found.statusCharacter,
        state_tag: stateTag,
        urgency_tag: urgencyTag,
        priority: found.priority,
        has_id: found.description.includes('\u{1F194}') || found.originalMarkdown.includes('\u{1F194}'),
    };
})()
