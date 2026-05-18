---
name: Inline Commands Directions
kind: directions
description: How to add a new inline command — decorator, context scope, consume mode, persistence
summary: Add a handler under work_buddy/inline/handlers/, decorate with @inline_command declaring name, surfaces, consume_mode, persistent, context_scope. Receives an InlineContext; returns a result dict.
trigger: 'user wants to add a new right-click menu item or #wb/cmd/* tag command'
tags:
- inline
- directions
- handler
aliases:
- add inline command
- register inline
- inline handler
parents:
- inline
- inline
---

# Adding an inline command

## 1. Write the handler

Drop a new file in `work_buddy/inline/handlers/`:

```python
from work_buddy.inline.registry import inline_command
from work_buddy.inline.models import InlineContext

@inline_command(
    name="mything/new",
    surfaces=["menu", "tag"],
    consume_mode="annotate",    # strip | annotate | replace | leave
    persistent=False,            # True → registers a PersistentWatcher instead of running
    menu_label="Do my thing",    # shown in right-click menu (prefixed with 'Work Buddy: ')
    interactive=True,            # route through thread chat
    context_scope="paragraph",   # line | paragraph | section | file
    description="One-sentence description for the menu.",
)
async def mything_new(ctx: InlineContext) -> dict:
    # ctx.surface, ctx.file_path, ctx.selection, ctx.line_text, ctx.paragraph, ...
    text = ctx.text_for_llm()
    # ... do the work ...
    return {"ok": True, "thread_id": ...}
```

Import the module in `work_buddy/inline/handlers/__init__.py` so registry auto-load picks it up.

## 2. Choose consume mode

| Mode | Effect | Use when |
|---|---|---|
| `strip` | Remove the tag from the note | Tag was a single-use trigger and you don't want an audit trail |
| `annotate` | Leave tag; append a `> [!work-buddy]` callout below | Default for most one-shot commands |
| `replace` | Rewrite `#wb/cmd/foo` → `#wb/cmd/foo/done` | Breadcrumb without callout clutter |
| `leave` | No-op | Persistent watchers (the tag IS the watcher) |

## 3. Persistence

Set `persistent=True` to register a `PersistentWatcher` on first tag detection instead of running immediately. The `inline-sync` sidecar job wakes watchers on their schedule and cancels them when the tag is removed from the vault.

## 4. Context scope

| Scope | Text passed in |
|---|---|
| `line` | Just the cursor/tag line |
| `paragraph` | Blank-line-separated block |
| `section` | Enclosing markdown heading section |
| `file` | Full note |

All scopes are pre-populated in `InlineContext`; the handler can use whichever fits.

## 5. Tag syntax

Single namespace: `#wb/cmd/<handler-name>`. Example: `#wb/cmd/task/new`. The handler's declared `persistent` field decides whether the tag is consumed or registered.

## Reuse

- Thread chat for interactive confirmation: `thread_create`, `thread_ask`, `thread_poll`
- Tag reads: `work_buddy.obsidian.tags.get_file_tags(path)` (returns positions)
- Note I/O: `work_buddy.obsidian.bridge.read_file` / `write_file`
- Consent: `work_buddy.consent` for destructive mutations
