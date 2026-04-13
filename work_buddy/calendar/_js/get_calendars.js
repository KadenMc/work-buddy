// List all Google Calendar calendars the user has access to.
return (async () => {
    const plugin = app.plugins.plugins['google-calendar'];
    if (!plugin?.api) return {error: 'Google Calendar plugin not available'};

    try {
        const calendars = await plugin.api.getCalendars();
        if (!calendars) return {error: 'getCalendars returned null'};

        return {
            count: calendars.length,
            calendars: calendars.map(c => ({
                id: c.id,
                summary: c.summary || c.summaryOverride || '(unnamed)',
                summaryOverride: c.summaryOverride || null,
                primary: !!c.primary,
                backgroundColor: c.backgroundColor || null,
                accessRole: c.accessRole || null,
                selected: c.selected !== false
            }))
        };
    } catch (e) {
        return {error: 'getCalendars failed: ' + e.message};
    }
})()
