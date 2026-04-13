// Get summary counts of tasks across the vault.
return (async () => {
    const cache = app.plugins.plugins['obsidian-tasks-plugin'].cache;
    if (!cache || cache.state !== 'Warm') return {error: 'Cache not warm', state: cache?.state};

    const tasks = cache.tasks;
    const now = new Date();
    const todayStr = now.getFullYear() + '-' +
        String(now.getMonth() + 1).padStart(2, '0') + '-' +
        String(now.getDate()).padStart(2, '0');

    let total = 0, todo = 0, done = 0, overdue = 0, dueSoon = 0;
    const byTag = {};
    const byFile = {};
    const byPriority = {1: 0, 2: 0, 3: 0};

    for (const t of tasks) {
        total++;
        const st = t.status ? t.status.type : '';
        if (st === 'TODO') {
            todo++;
            // Check overdue
            if (t._dueDate) {
                try {
                    const dueStr = t._dueDate.format('YYYY-MM-DD');
                    if (dueStr < todayStr) overdue++;
                    else if (dueStr <= todayStr) dueSoon++;
                } catch(e) {}
            }
        } else if (st === 'DONE') {
            done++;
        }

        // Count by tags
        for (const tag of (t.tags || [])) {
            byTag[tag] = (byTag[tag] || 0) + 1;
        }

        // Count by file
        const fp = t.taskLocation?._tasksFile?._path || 'unknown';
        byFile[fp] = (byFile[fp] || 0) + 1;

        // Count by priority
        if (byPriority[t.priority] !== undefined) byPriority[t.priority]++;
    }

    return {
        total, todo, done, overdue, due_soon: dueSoon,
        by_priority: byPriority,
        by_tag: byTag,
        by_file: byFile
    };
})()
