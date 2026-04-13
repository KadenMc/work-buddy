// Create an Obsidian note for a Google Calendar event.
// Params: __EVENT_JSON__, __CALENDAR_ID__, __CONFIRM__
return (async () => {
    const plugin = app.plugins.plugins['google-calendar'];
    if (!plugin?.api) return {error: 'Google Calendar plugin not available'};

    let event;
    try {
        event = JSON.parse('__EVENT_JSON__');
    } catch (e) {
        return {error: 'Invalid event JSON: ' + e.message};
    }

    if (!event.id) return {error: 'Event must have an id field'};

    const calendarId = '__CALENDAR_ID__' || event.parent?.id || '';
    const confirm = '__CONFIRM__' === 'true';

    try {
        await plugin.api.createEventNote(event, calendarId, confirm);
        return {
            success: true,
            event_id: event.id,
            summary: event.summary || '(unknown)',
            calendar_id: calendarId,
            confirmed: confirm
        };
    } catch (e) {
        return {error: 'createEventNote failed: ' + e.message};
    }
})()
