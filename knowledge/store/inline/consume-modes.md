---
name: Inline Consume Modes (Retired)
kind: concept
description: Historical note-mutation modes used by the retired Obsidian inline dispatcher.
summary: 'strip, annotate, replace, and leave remain implementation history only; no native action may mutate a Markdown tag through this path.'
tags:
- inline
- retired
- mutation
aliases:
- consume modes
- inline mutation
parents:
- inline
---

The former dispatcher could strip, annotate, replace, or leave an Obsidian
command tag after execution. Those modes are not active product behavior. New
native actions use their owning domain's mutation and provenance contracts;
they must not route through `work_buddy.inline.consume` or the Obsidian bridge.
