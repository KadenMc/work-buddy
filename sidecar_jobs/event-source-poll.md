---
schedule: "*/5 * * * *"  # every 5 minutes; each source fires only when its own interval has elapsed
recurring: true
type: capability
capability: event_sources_poll
params: {}
---
Event-source poll tick. Every 5 minutes, poll each enabled event source whose
`interval` has elapsed: fetch → diff the watched value → publish
`ai.workbuddy.source.<name>.changed` on a meaningful change. The per-source
`interval` (not this cadence) governs how often any given source is actually
fetched — this tick just wakes the poller up.
