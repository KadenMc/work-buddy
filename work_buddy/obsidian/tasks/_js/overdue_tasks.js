// Find all overdue incomplete tasks.
return (async () => {
    const cache = app.plugins.plugins['obsidian-tasks-plugin'].cache;
    if (!cache || cache.state !== 'Warm') return {error: 'Cache not warm', state: cache?.state};

    const now = new Date();
    const todayStr = now.getFullYear() + '-' +
        String(now.getMonth() + 1).padStart(2, '0') + '-' +
        String(now.getDate()).padStart(2, '0');

    const overdue = [];
    for (const t of cache.tasks) {
        if (t.status?.type !== 'TODO') continue;
        if (!t._dueDate) continue;
        try {
            const dueStr = t._dueDate.format('YYYY-MM-DD');
            if (dueStr < todayStr) {
                overdue.push({
                    description: t.description,
                    due_date: dueStr,
                    priority: t.priority,
                    tags: t.tags || [],
                    file: t.taskLocation?._tasksFile?._path || null,
                    line: t.taskLocation?._lineNumber || null,
                    days_overdue: Math.floor((now - new Date(dueStr)) / 86400000),
                });
            }
        } catch(e) {}
    }

    // Sort by most overdue first
    overdue.sort((a, b) => a.due_date.localeCompare(b.due_date));

    return {count: overdue.length, tasks: overdue};
})()
