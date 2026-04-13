// Fetch today's events, sorted by start time, with schedule metadata.
return (async () => {
    const plugin = app.plugins.plugins['google-calendar'];
    if (!plugin?.api) return {error: 'Google Calendar plugin not available'};

    const moment = window.moment;
    if (!moment) return {error: 'moment.js not available'};

    const now = moment();
    const startOfDay = moment().startOf('day');
    const endOfDay = moment().endOf('day');

    try {
        const events = await plugin.api.getEvents({
            startDate: startOfDay,
            endDate: endOfDay
        });
        if (!events) return {count: 0, events: [], date: now.format('YYYY-MM-DD')};

        // Sort: timed events by start time, all-day events first
        const sorted = events.sort((a, b) => {
            const aAllDay = !!(a.start?.date && !a.start?.dateTime);
            const bAllDay = !!(b.start?.date && !b.start?.dateTime);
            if (aAllDay && !bAllDay) return -1;
            if (!aAllDay && bAllDay) return 1;
            const aTime = a.start?.dateTime || a.start?.date || '';
            const bTime = b.start?.dateTime || b.start?.date || '';
            return aTime.localeCompare(bTime);
        });

        // Classify events relative to now
        const classified = sorted.map(e => {
            const isAllDay = !!(e.start?.date && !e.start?.dateTime);
            let startMoment = null;
            let endMoment = null;
            let timeStatus = 'all-day';

            if (!isAllDay) {
                startMoment = moment(e.start?.dateTime);
                endMoment = moment(e.end?.dateTime);
                if (now.isBefore(startMoment)) timeStatus = 'upcoming';
                else if (now.isAfter(endMoment)) timeStatus = 'past';
                else timeStatus = 'current';
            }

            return {
                summary: e.summary || '(no title)',
                status: e.status || 'confirmed',
                start: e.start || null,
                end: e.end || null,
                isAllDay: isAllDay,
                timeStatus: timeStatus,
                location: e.location || null,
                calendarName: e.parent?.summaryOverride || e.parent?.summary || null,
                htmlLink: e.htmlLink || null,
                description: e.description ? e.description.substring(0, 300) : null
            };
        });

        const allDay = classified.filter(e => e.isAllDay);
        const timed = classified.filter(e => !e.isAllDay);
        const upcoming = timed.filter(e => e.timeStatus === 'upcoming');
        const current = timed.filter(e => e.timeStatus === 'current');

        return {
            date: now.format('YYYY-MM-DD'),
            currentTime: now.format('HH:mm'),
            count: events.length,
            allDayCount: allDay.length,
            timedCount: timed.length,
            upcomingCount: upcoming.length,
            currentCount: current.length,
            events: classified
        };
    } catch (e) {
        return {error: 'getEvents failed: ' + e.message};
    }
})()
