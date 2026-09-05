---
name: Log Record Lifecycle
kind: concept
description: What a Journal Log record is, which records accept in-place text and time edits, and how the day timeline offers them.
summary: A Log record is a native Journal item representing something that happened. Records this surface authored accept an in-place edit of their exact text and occurrence time, and a delete; records carrying another authority explain themselves instead of offering controls that cannot act.
tags:
- journal
- log
- records
- editing
- timeline
- lifecycle
aliases:
- Journal Log record
- editing a Log entry
- record edit and delete
parents:
- journal
dev_notes: |-
  The edit gate reads three fields together on a timeline item: authority kind, exact text, and a positive integer version. A producer that omits any of them silently disables the actions, which is why the fixture provider carries the same three fields as the live one. Version is the item's own `current_revision`, never the whole-model revision string.
---

A Log record is a native Journal item representing something that already
happened. It sits on the day timeline alongside plans and calendar events, and
unlike a Running Note it is not working material the user is still shaping.

## Which records accept an edit

Editing a record in place changes content, so the surface offers it only where
the content authority permits it. A record this surface authored accepts an edit
of its exact text and of its occurrence time, plus a delete. A record whose
content authority lies elsewhere, whether carried over from the file-backed
Journal or produced by a generator, accepts neither.

The decision reads three facts together: the record's authority kind, its exact
text, and its numeric revision. A producer that omits any of them yields a record
the surface treats as not editable, so a producer that means a record to be
editable carries all three. That includes fixture producers, which model the same
contract as the live one so a demo exercises the real gate rather than a weaker
one.

A read-only day offers neither action, regardless of authority.

## What an edit changes

Text and occurrence time change together in one write. The occurrence time is the
record's own position in the day, so correcting it moves the record within that
day's ordering rather than leaving it filed under the moment it was typed. A
corrected time is bounded by the window of the day the record belongs to.

The write asserts the record's numeric revision, so a stale client conflicts
instead of overwriting a newer edit, and a client mutation identifier makes a
retry idempotent. Replaying the same identifier with different content or a
different time is a conflict rather than a silent divergence.

## Why a record with no available action still says something

A record that can offer neither action renders an explanation rather than an
empty menu or a control that dispatches nowhere. Buttons that resolve to nothing
read as breakage, and an empty surface reads as a rendering fault, so the record
states why it cannot be changed here.

See `journal/running-note-lifecycle`, `journal/source-backed-capture`,
`journal/day-lifecycle`, and `services/dashboard/react/calendar-surface`.
