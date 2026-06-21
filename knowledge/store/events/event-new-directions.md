---
name: Event New Directions
kind: directions
description: How to run /wb-event-new — author an event source in conversation (elicit → propose → dry-run → confirm → activate → monitor). Builds polling watchers on the events backbone via event_source_create, grounded to registered source types and actions.
trigger: When the user invokes /wb-event-new or asks to watch a source, get notified when something changes, or create an event source / watcher
command: wb-event-new
workflow: events/event-new
capabilities:
- event_source_create
- event_source_dry_run
- event_source_list
- event_source_toggle
tags:
- events
- source
- authoring
- directions
- watcher
aliases:
- event new directions
- how to author an event source
- watch a source directions
parents:
- events
---

Run `/wb-event-new` to author an event source — a durable watcher that polls some
state, reacts on a *meaningful change*, evaluates a condition, and fires a notify
action. The workflow walks elicit → propose → dry-run → confirm → activate →
monitor, so the user sees a real preview before anything is written or fires.

## When to run

When the user wants to be notified when something out in the world changes — a
price, a status page, a number on a JSON endpoint — without babysitting it. Not
for one-off "check this now" questions (just fetch it); a source is for *standing*
watches.

## Stay grounded

Propose only what the registry actually supports: `source.type: http_poll`,
`extract.mode` in {`json_path`, `css`, `hash`}, `action: notify`, `autonomy:
notify_only`. The condition is CEL over `event.data` / `prev.data`. If the user
asks for something outside this set (a push webhook, an auto-executing action,
another source type), say it isn't supported yet and stop — never fabricate a
source that fails validation.

## The Tier-3 semantic gate (optional)

For "only notify me if it's *material*" intents, add a `semantic` block on top of
the CEL condition: `{question: "<the materiality question>", query: "<web-search
query>", cooldown: "1h", debounce: "2/3", min_confidence: 0.0}`. Only `question`
is required (`query` defaults to it). It runs **after** CEL passes — CEL is the
cheap prefilter, the semantic gate web-searches and asks a local model. Use it
when the change-detection (CEL) can't express the judgement ("is this news
material?", "is this a real outage vs. a blip?"). It needs websearch reachable and
a local model loaded; if either is unavailable the gate **fails closed** (never
fires) rather than erroring. Recommend a `cooldown` so an ongoing story doesn't
re-notify.

## The dry-run is the safety gate

`event_source_dry_run` previews the proposed source with **zero side effects** —
no file, no publish, no action. Run it on the proposal *before* writing anything,
and only call `event_source_create` after the user confirms. A brand-new source's
first observation is a silent baseline, so `would_fire: false` on the preview is
normal — read the dry-run as "the fetch, extraction, and condition are sound,"
then activate. The semantic gate is **reported but not run** by default (it's a
real search + LLM call); pass `run_semantic: true` to actually evaluate it in the
preview.

## Scope + autonomy

A source runs only the actions in its `allowed_actions`, and `notify_only` means
the action notifies — it never executes a state-changing capability on the user's
behalf. The notification is the surface; the user decides what to do with it.

## Managing sources

`event_source_list` shows what's authored (and any that failed validation);
`event_source_toggle` pauses or resumes one without deleting it (the cursor is
preserved). A source that exceeds its `max_per_hour` auto-suspends itself.
