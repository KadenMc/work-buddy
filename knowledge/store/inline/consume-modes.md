---
name: Inline Consume Modes
kind: concept
description: Post-execution note mutation behaviors — strip, annotate, replace, leave
summary: 'Four modes applied after a handler returns: strip removes the tag; annotate leaves it and appends a callout; replace rewrites to `/done`; leave is a no-op. User preference can override the handler''s declared default.'
tags:
- inline
- consume
- mutation
aliases:
- consume modes
- inline mutation
- post-exec
parents:
- inline
- inline
---

# Consume modes

Implemented in `work_buddy/inline/consume.py`. Invoked by the dispatcher after the handler returns (for one-shot commands) via `apply(mode, ctx, result)`.

| Mode | Before | After |
|---|---|---|
| `strip` | `- [ ] task #wb/cmd/task/new` | `- [ ] task` |
| `annotate` | `- [ ] task #wb/cmd/task/new` | unchanged, plus below: `> [!work-buddy] Processed at <ts>\n> Result: <summary>` |
| `replace` | `#wb/cmd/task/new` | `#wb/cmd/task/new/done` |
| `leave` | (unchanged) | (unchanged) |

## Override precedence

1. User preference (`features.inline.consume_mode_override` if set)
2. Handler's declared `consume_mode`
3. Default: `annotate`

## Safety

Note mutations go through `work_buddy.obsidian.bridge.write_file` to preserve Obsidian's dirty-buffer handling — never direct filesystem writes. Destructive modes (`strip`, `replace`) should gate on `work_buddy.consent` for high-stakes operations.
