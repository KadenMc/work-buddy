"""In-memory registry of inline commands.

Handlers register themselves via the :func:`inline_command` decorator at
import time. The dispatcher queries this registry by command name.
"""

from __future__ import annotations

import logging
from typing import Callable

from work_buddy.inline.models import InlineCommand

logger = logging.getLogger(__name__)


_COMMANDS: dict[str, InlineCommand] = {}


def register(command: InlineCommand) -> InlineCommand:
    """Idempotent upsert keyed on ``command.name``."""
    if not command.name:
        raise ValueError("InlineCommand.name is required")
    if command.name in _COMMANDS:
        logger.info("Replacing inline command %r", command.name)
    _COMMANDS[command.name] = command
    return command


def get(name: str) -> InlineCommand | None:
    return _COMMANDS.get(name)


def list_commands() -> list[InlineCommand]:
    return list(_COMMANDS.values())


def list_for_surface(surface: str) -> list[InlineCommand]:
    return [c for c in _COMMANDS.values() if surface in c.surfaces]


def clear() -> None:
    """Test helper — wipe the registry."""
    _COMMANDS.clear()


def inline_command(
    *,
    name: str,
    surfaces: list[str],
    consume_mode: str = "leave",
    persistent: bool = False,
    menu_label: str | None = None,
    interactive: bool = False,
    context_scope: str = "line",
    description: str = "",
) -> Callable[[Callable], Callable]:
    """Decorator that builds an :class:`InlineCommand` and registers it.

    The decorated function is returned unchanged so it remains directly
    callable (useful for tests).
    """

    def decorator(fn: Callable) -> Callable:
        cmd = InlineCommand(
            name=name,
            description=description or (fn.__doc__ or "").strip().splitlines()[0:1][0]
            if (description or fn.__doc__)
            else "",
            surfaces=list(surfaces),
            consume_mode=consume_mode,
            persistent=persistent,
            menu_label=menu_label,
            interactive=interactive,
            context_scope=context_scope,
            handler=fn,
        )
        register(cmd)
        return fn

    return decorator


def _load_builtin_handlers() -> None:
    """Import the handlers package so decorators fire."""
    try:
        import work_buddy.inline.handlers  # noqa: F401
    except ImportError as exc:
        logger.warning("Inline handler package import failed: %s", exc)


_load_builtin_handlers()
