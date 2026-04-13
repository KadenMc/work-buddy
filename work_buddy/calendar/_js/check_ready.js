// Check if the Google Calendar plugin is loaded and authenticated.
return (async () => {
    const plugin = app.plugins.plugins['google-calendar'];
    if (!plugin) return {ready: false, reason: 'Google Calendar plugin not found'};

    const api = plugin.api;
    if (!api) return {ready: false, reason: 'No API object on plugin'};

    // Try a lightweight call to verify authentication
    try {
        const calendars = await api.getCalendars();
        return {
            ready: true,
            calendar_count: calendars ? calendars.length : 0,
            version: plugin.manifest.version,
            has_refresh_token: !!plugin.settings?.googleRefreshToken,
            default_calendar: plugin.settings?.defaultCalendar || null
        };
    } catch (e) {
        return {
            ready: false,
            reason: 'getCalendars failed: ' + e.message,
            version: plugin.manifest.version
        };
    }
})()
