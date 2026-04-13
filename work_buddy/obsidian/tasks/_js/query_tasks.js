// Query tasks with optional filters.
// Placeholders: __STATUS_TYPE__, __FILE_PATH__, __TAG_FILTER__, __TEXT_FILTER__, __LIMIT__
return (async () => {
    const cache = app.plugins.plugins['obsidian-tasks-plugin'].cache;
    if (!cache || cache.state !== 'Warm') return {error: 'Cache not warm', state: cache?.state};

    let tasks = cache.tasks;
    const statusType = '__STATUS_TYPE__';
    const filePath = '__FILE_PATH__';
    const tagFilter = '__TAG_FILTER__';
    const textFilter = '__TEXT_FILTER__';
    const limit = parseInt('__LIMIT__') || 500;

    // Filter by status type (TODO, DONE, or empty for all)
    if (statusType) {
        tasks = tasks.filter(t => t.status && t.status.type === statusType);
    }

    // Filter by file path (prefix match)
    if (filePath) {
        tasks = tasks.filter(t => {
            const p = t.taskLocation?._tasksFile?._path || '';
            return p.startsWith(filePath);
        });
    }

    // Filter by tag (exact match on any tag)
    if (tagFilter) {
        tasks = tasks.filter(t => t.tags && t.tags.includes(tagFilter));
    }

    // Filter by text in description (case-insensitive)
    if (textFilter) {
        const lower = textFilter.toLowerCase();
        tasks = tasks.filter(t => t.description.toLowerCase().includes(lower));
    }

    // Serialize
    const results = tasks.slice(0, limit).map(t => {
        const r = {
            description: t.description,
            status_type: t.status ? t.status.type : null,
            status_symbol: t.statusCharacter,
            priority: t.priority,
            tags: t.tags || [],
            file: t.taskLocation?._tasksFile?._path || null,
            line: t.taskLocation?._lineNumber || null,
            section: t.taskLocation?._precedingHeader || null,
            has_children: (t.children || []).length > 0,
            id: t.id || null,
            block_link: t.blockLink || null,
        };

        // Dates — extract as YYYY-MM-DD strings
        try { r.due_date = t._dueDate ? t._dueDate.format('YYYY-MM-DD') : null; } catch(e) { r.due_date = null; }
        try { r.done_date = t._doneDate ? t._doneDate.format('YYYY-MM-DD') : null; } catch(e) { r.done_date = null; }
        try { r.created_date = t._createdDate ? t._createdDate.format('YYYY-MM-DD') : null; } catch(e) { r.created_date = null; }
        try { r.scheduled_date = t._scheduledDate ? t._scheduledDate.format('YYYY-MM-DD') : null; } catch(e) { r.scheduled_date = null; }
        try { r.start_date = t._startDate ? t._startDate.format('YYYY-MM-DD') : null; } catch(e) { r.start_date = null; }
        try { r.cancelled_date = t._cancelledDate ? t._cancelledDate.format('YYYY-MM-DD') : null; } catch(e) { r.cancelled_date = null; }

        return r;
    });

    return {total_matched: tasks.length, returned: results.length, tasks: results};
})()
