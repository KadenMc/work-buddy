// Fetch events for a date range using Moment.js (required by plugin).
// Params: __START_DATE__, __END_DATE__ (ISO date strings, e.g. "2026-04-04")
return (async () => {
    const plugin = app.plugins.plugins['google-calendar'];
    if (!plugin?.api) return {error: 'Google Calendar plugin not available'};

    const moment = window.moment;
    if (!moment) return {error: 'moment.js not available in Obsidian'};

    const startDate = moment('__START_DATE__').startOf('day');
    const endDate = moment('__END_DATE__').endOf('day');

    if (!startDate.isValid() || !endDate.isValid()) {
        return {error: 'Invalid date(s): __START_DATE__ to __END_DATE__'};
    }

    try {
        const events = await plugin.api.getEvents({
            startDate: startDate,
            endDate: endDate
        });
        if (!events) return {count: 0, events: []};

        return {
            count: events.length,
            range: {
                start: startDate.format('YYYY-MM-DD'),
                end: endDate.format('YYYY-MM-DD')
            },
            events: events.map(e => ({
                id: e.id,
                summary: e.summary || '(no title)',
                status: e.status || 'confirmed',
                start: e.start || null,
                end: e.end || null,
                location: e.location || null,
                description: e.description ? e.description.substring(0, 500) : null,
                htmlLink: e.htmlLink || null,
                calendarId: e.parent?.id || e.organizer?.email || null,
                calendarName: e.parent?.summaryOverride || e.parent?.summary || null,
                eventType: e.eventType || 'default',
                isAllDay: !!(e.start?.date && !e.start?.dateTime),
                colorId: e.colorId || null,
                transparency: e.transparency || null
            }))
        };
    } catch (e) {
        return {error: 'getEvents failed: ' + e.message};
    }
})()
