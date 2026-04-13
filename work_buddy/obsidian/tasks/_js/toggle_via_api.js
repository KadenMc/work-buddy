// Toggle task completion using the Tasks plugin's own apiV1.
// This handles recurring tasks, done dates, and status transitions correctly.
// Placeholders: __TASK_LINE__, __FILE_PATH__
return (async () => {
    const plugin = app.plugins.plugins['obsidian-tasks-plugin'];
    if (!plugin) return {error: 'Tasks plugin not found'};
    if (!plugin.apiV1) return {error: 'Tasks apiV1 not available'};

    const line = `__TASK_LINE__`;
    const path = '__FILE_PATH__';

    try {
        const toggled = plugin.apiV1.executeToggleTaskDoneCommand(line, path);
        return {
            success: true,
            original: line,
            toggled: toggled,
        };
    } catch(e) {
        return {error: 'Toggle failed: ' + e.message};
    }
})()
