"""Typed Obsidian error hierarchy.

The Obsidian bridge layer raises subclasses of :class:`ObsidianError` to
signal failures. The gateway classifies them via ``isinstance`` rather than
substring-matching error strings; the dashboard, op records, and notification
surfaces consume the structured ``error_kind`` carried on each instance.

Hierarchy mirrors the four-state taxonomy in
:func:`work_buddy.obsidian.bridge.get_last_bridge_state`:

  ObsidianError                          error_kind = "obsidian_unknown"
  ├── ObsidianUnreachable                error_kind = "obsidian_unreachable"
  │   ├── ObsidianNotRunning             error_kind = "obsidian_not_running"
  │   ├── ObsidianPluginMissing          error_kind = "obsidian_plugin_missing"
  │   ├── ObsidianPluginDisabled         error_kind = "obsidian_plugin_disabled"
  │   └── ObsidianStartupRace            error_kind = "obsidian_startup_race"
  ├── ObsidianTimeout                    error_kind = "obsidian_timeout"
  │   └── ObsidianPostWriteUncertain     error_kind = "obsidian_post_write_uncertain"
  │       carries (path, content_hint, write_mode)
  └── ObsidianHTTPError                  carries (status, body)
      ├── ObsidianEditorConflict         error_kind = "obsidian_editor_conflict"
      ├── ObsidianRefused                error_kind = "obsidian_refused"
      └── ObsidianServerError            error_kind = "obsidian_server_error"

Discipline:
  - Bridge layer raises typed exceptions. No more returning ``False``/``None``
    for failure (one transitional shim aside; see ``bridge.write_file_raw``).
  - Capabilities do NOT try/except by default — let exceptions propagate.
    The gateway has a single top-level handler that classifies via isinstance.
  - Capabilities catch selectively only for genuine domain-specific recovery:
    filesystem fallback in ``vault_write``; post-write verify is gateway-side
    via :func:`work_buddy.obsidian.post_write_verify.verify_post_write`.

This module intentionally has NO imports from ``work_buddy.errors`` to avoid
a circular dependency. ``classify_error`` performs a lazy import of these
types at call-site.
"""

from __future__ import annotations

from typing import Any


class ObsidianError(Exception):
    """Base for all Obsidian-related failures.

    Subclasses set ``error_kind`` to a stable string that survives
    serialization (op records, result dicts, notifications). The string
    is the canonical category — never parse the human-readable message
    for routing decisions.
    """

    error_kind: str = "obsidian_unknown"


# ---------------------------------------------------------------------------
# Connectivity (the four-state taxonomy from bridge.get_last_bridge_state)
# ---------------------------------------------------------------------------


class ObsidianUnreachable(ObsidianError):
    """Connection couldn't be established. Subclass disambiguates *why*.

    Operation outcome is undefined at this layer — for a write, the request
    never reached the plugin; for a read, no data was retrieved. Caller
    semantics determine whether this is recoverable.
    """

    error_kind: str = "obsidian_unreachable"


class ObsidianNotRunning(ObsidianUnreachable):
    """State 1: the Obsidian app isn't running.

    Detected when the bridge port refuses connection AND the OS process
    table shows no Obsidian process. Terminal — no point retrying until
    the user opens Obsidian.
    """

    error_kind: str = "obsidian_not_running"


class ObsidianPluginMissing(ObsidianUnreachable):
    """State 3: Obsidian is running but the work-buddy plugin isn't installed.

    Detected when the bridge port refuses connection but Obsidian is up,
    AND the plugin's manifest is absent from the vault's plugin folder.
    Terminal — user must install from
    https://github.com/KadenMc/obsidian-work-buddy.
    """

    error_kind: str = "obsidian_plugin_missing"


class ObsidianPluginDisabled(ObsidianUnreachable):
    """State 4: plugin installed but disabled in Settings → Community Plugins.

    Detected when the manifest is present but ``community-plugins.json``
    does not list the plugin as enabled. Terminal — user must toggle on.
    """

    error_kind: str = "obsidian_plugin_disabled"


class ObsidianStartupRace(ObsidianUnreachable):
    """Plugin enabled but port not yet bound (Obsidian just started).

    Distinct from :class:`ObsidianNotRunning` because Obsidian IS running
    and the plugin IS enabled — we just hit the brief window between the
    process starting and the plugin's HTTP listener becoming ready.
    Worth a short retry; backs off to terminal if the race persists.
    """

    error_kind: str = "obsidian_startup_race"


# ---------------------------------------------------------------------------
# Response / latency
# ---------------------------------------------------------------------------


class ObsidianTimeout(ObsidianError):
    """State 2: connection succeeded; response didn't arrive within deadline.

    For idempotent operations (reads), retry is safe. For mutating
    operations where the request body was sent, see
    :class:`ObsidianPostWriteUncertain` — that subclass carries
    enough context for a verify-then-decide recovery.
    """

    error_kind: str = "obsidian_timeout"


class ObsidianPostWriteUncertain(ObsidianTimeout):
    """A mutating request timed out *after* the body was sent.

    The vault state may or may not reflect the change. The gateway
    catches this exception and dispatches to
    :func:`work_buddy.obsidian.post_write_verify.verify_post_write`,
    which reads the target file from filesystem and decides:

      - ``"verified"`` → write actually landed; return success-with-warning
      - ``"absent"``   → write definitively didn't land; re-raise as
                        plain :class:`ObsidianTimeout` for normal enqueue
      - ``"indeterminate"`` → conservatively treat as ``"absent"``

    Carriers:
      - ``path``: vault-relative path of the target file.
      - ``content_hint``: substring fragment for ``write_mode in
        {"insert", "append"}``; sha256 of full content for
        ``write_mode == "replace"``.
      - ``write_mode``: one of ``"insert" | "append" | "replace"``.
    """

    error_kind: str = "obsidian_post_write_uncertain"

    def __init__(
        self,
        path: str,
        *,
        content_hint: str | None = None,
        write_mode: str = "replace",
    ) -> None:
        self.path = path
        self.content_hint = content_hint
        self.write_mode = write_mode
        super().__init__(
            f"obsidian_post_write_uncertain: {path} "
            f"(write_mode={write_mode}, hint_len={len(content_hint or '')})"
        )


# ---------------------------------------------------------------------------
# HTTP responses (server reachable, returned non-2xx)
# ---------------------------------------------------------------------------


class ObsidianHTTPError(ObsidianError):
    """Server reachable, returned a non-2xx status. Subclass picks policy.

    Carriers:
      - ``status``: HTTP status code.
      - ``body``: parsed response body (dict) when JSON, else None.
    """

    error_kind: str = "obsidian_http_error"

    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body
        super().__init__(f"{self.error_kind}: HTTP {status}")


class ObsidianEditorConflict(ObsidianHTTPError):
    """409: target file is open in an editor with unsaved typing.

    Always retryable, but the *payload* must be recomputed from a fresh
    read after the user finishes editing — replaying the original bytes
    would clobber the user's saved typing. The retry sweep re-invokes
    the whole capability, which performs a fresh read-modify-write.

    Replaces the standalone ``EditorConflict`` exception that lived
    in :mod:`work_buddy.obsidian.bridge` before CP1. Constructor
    signature is preserved (``path, reason="editor_dirty"``) and the
    legacy ``editor_dirty: <path>`` message format is reproduced
    byte-for-byte for log scrapers and existing test assertions. The
    backwards-compat alias was removed in CP9.
    """

    error_kind: str = "obsidian_editor_conflict"

    def __init__(
        self,
        path: str,
        reason: str = "editor_dirty",
        body: dict[str, Any] | None = None,
    ) -> None:
        # Skip ObsidianHTTPError.__init__ — we want the legacy message
        # format ("editor_dirty: <path>") not the generic HTTP one.
        self.status = 409
        self.body = body
        self.path = path
        self.reason = reason
        # Initialise base Exception with the legacy message shape.
        Exception.__init__(self, f"{reason}: {path}")


class ObsidianRefused(ObsidianHTTPError):
    """4xx other than 409 — structural rejection.

    NOT retryable as-is. Possible causes: malformed path, missing auth,
    permission denied. The gateway classifies this as ``"permanent"``
    so it doesn't waste retries on a request that will never succeed
    without changing.
    """

    error_kind: str = "obsidian_refused"


class ObsidianServerError(ObsidianHTTPError):
    """5xx — plugin-side fault.

    Generally transient (the plugin may have hit an internal error or
    been mid-reload). Safe to retry. The gateway classifies as transient.
    """

    error_kind: str = "obsidian_server_error"


__all__ = [
    "ObsidianError",
    "ObsidianUnreachable",
    "ObsidianNotRunning",
    "ObsidianPluginMissing",
    "ObsidianPluginDisabled",
    "ObsidianStartupRace",
    "ObsidianTimeout",
    "ObsidianPostWriteUncertain",
    "ObsidianHTTPError",
    "ObsidianEditorConflict",
    "ObsidianRefused",
    "ObsidianServerError",
]
