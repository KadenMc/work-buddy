// Search tasks by text content (case-insensitive).
// Placeholder: __QUERY__
return (async () => {
    const cache = app.plugins.plugins['obsidian-tasks-plugin'].cache;
    if (!cache || cache.state !== 'Warm') return {error: 'Cache not warm', state: cache?.state};

    const query = '__QUERY__'.toLowerCase();
    const matched = [];

    for (const t of cache.tasks) {
        if (!t.description.toLowerCase().includes(query)) continue;
        const r = {
            description: t.description,
            status_type: t.status ? t.status.type : null,
            priority: t.priority,
            tags: t.tags || [],
            file: t.taskLocation?._tasksFile?._path || null,
            line: t.taskLocation?._lineNumber || null,
        };
        try { r.due_date = t._dueDate ? t._dueDate.format('YYYY-MM-DD') : null; } catch(e) { r.due_date = null; }
        try { r.done_date = t._doneDate ? t._doneDate.format('YYYY-MM-DD') : null; } catch(e) { r.done_date = null; }
        matched.push(r);
    }

    return {query: '__QUERY__', count: matched.length, tasks: matched};
})()
