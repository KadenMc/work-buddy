"""Co-work (K2) HTTP surface package.

The dashboard mounts the co-work document routes by calling register_routes
with its Flask app (a one-line mount at the join). Sittings live on this HTTP
surface exclusively and call the Truth engine library directly under a
user_initiated consent boundary, threading a real dashboard user identity into
gesture actor refs rather than cloning the MCP path's fixed single-user
constant.

The Flask blueprint is imported lazily so that importing this package never
pulls Flask when only the transport or sitting helpers are needed.
"""

from __future__ import annotations

__all__ = ["cowork_blueprint", "register_routes"]


def __getattr__(name: str):  # pragma: no cover - thin lazy re-export
    if name in __all__:
        from work_buddy.cowork import api

        return getattr(api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
