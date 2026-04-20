"""Inline Obsidian commands — activation surface for work-buddy from inside a note.

Two surfaces funnel into one dispatcher:

- ``menu`` — right-click ephemeral one-shot commands
- ``tag`` — ``#wb/cmd/*`` in-document triggers (one-shot or persistent watchers)

Handlers are registered via the :func:`registry.inline_command` decorator and
declare their own ``consume_mode`` (strip/annotate/replace/leave) and whether
they install a persistent watcher.
"""
