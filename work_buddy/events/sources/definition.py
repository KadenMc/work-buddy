"""``EventSourceDef`` — the typed contract for a user-authored event source.

The schema is deliberately shallow (≤2 levels) so an LLM can fill it reliably
in the ``/wb-event-new`` authoring loop. Validation is shared between the loader
(read side) and the ``event_source_create`` op (write side), mirroring the
``create_user_job_file`` discipline: reject a malformed source at author/load
time with a specific message rather than failing silently at poll time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from work_buddy.frontmatter import parse_frontmatter

NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# ``fake`` is the no-network backend used by tests / dry runs.
KNOWN_SOURCE_TYPES = frozenset({"http_poll", "fake"})
EXTRACT_MODES = frozenset({"json_path", "css", "hash"})
DEDUP_MODES = frozenset({"unique", "greatest", "last", "hash"})
AUTONOMY = frozenset({"notify_only", "auto_execute"})
# Actions a source may declare. Extends as new Processors are registered.
KNOWN_ACTIONS = frozenset({"notify"})

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(value: Any) -> int | None:
    """``"6h"`` → ``21600`` seconds. Returns ``None`` on anything invalid."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value.lower())
    if not m:
        return None
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


@dataclass(frozen=True)
class EventSourceDef:
    """A loaded, validated event source. ``name`` is the ``.md`` file stem."""

    name: str
    type: str
    interval_s: int
    url: str | None = None
    auth: Any = None
    cursor_from: str = "now"            # now | all | <iso-date>
    extract_mode: str = "hash"          # json_path | css | hash
    extract_path: str | None = None
    id_field: str | None = None
    dedup: str = "hash"
    condition: str | None = None        # CEL expression (optional)
    action_name: str = "notify"
    action_params: dict[str, Any] = field(default_factory=dict)
    allowed_actions: tuple[str, ...] = ("notify",)
    autonomy: str = "notify_only"
    max_per_hour: int | None = None
    enabled: bool = True
    event_type: str = ""                # reverse-DNS; derived if unset
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def source_uri(self) -> str:
        return f"/wb/source/{self.name}"


def from_frontmatter(name: str, fm: dict[str, Any]) -> EventSourceDef:
    """Build an ``EventSourceDef`` from a parsed frontmatter dict (no validation
    — call :func:`validate_source_fm` first)."""
    source = fm.get("source") or {}
    extract = fm.get("extract") or {}
    cursor = fm.get("cursor") or {}
    action = fm.get("action") or {}
    rate = fm.get("rate_limit") or {}
    action_name = str(action.get("name", "notify"))
    allowed = fm.get("allowed_actions") or [action_name]
    return EventSourceDef(
        name=name,
        type=str(source.get("type", "")).strip(),
        interval_s=parse_interval(source.get("interval")) or 0,
        url=source.get("url"),
        auth=source.get("auth"),
        cursor_from=str(cursor.get("from", "now")),
        extract_mode=str(extract.get("mode", "hash")),
        extract_path=extract.get("path"),
        id_field=extract.get("id_field"),
        dedup=str(fm.get("dedup", "hash")),
        condition=fm.get("condition"),
        action_name=action_name,
        action_params=action.get("params") or {},
        allowed_actions=tuple(allowed),
        autonomy=str(fm.get("autonomy", "notify_only")),
        max_per_hour=rate.get("max_per_hour"),
        enabled=bool(fm.get("enabled", True)),
        event_type=str(fm.get("event_type") or f"ai.workbuddy.source.{name}.changed"),
        raw=fm,
    )


def parse_source_md(path: Path) -> EventSourceDef:
    fm, _ = parse_frontmatter(path)
    return from_frontmatter(path.stem, fm)


def build_source_fm(
    *,
    source_type: str,
    interval: str,
    url: str | None = None,
    extract_mode: str = "hash",
    extract_path: str | None = None,
    condition: str | None = None,
    action: str = "notify",
    action_params: dict[str, Any] | None = None,
    allowed_actions: list[str] | None = None,
    autonomy: str = "notify_only",
    max_per_hour: int | None = None,
    cursor_from: str = "now",
    enabled: bool = True,
    event_type: str | None = None,
) -> dict[str, Any]:
    """Build an ``event_source`` frontmatter dict from structured fields — the
    inverse of :func:`from_frontmatter`, used by the ``event_source_create`` op
    and the ``/wb-event-new`` authoring loop. Validate the result with
    :func:`validate_source_fm` before writing it."""
    fm: dict[str, Any] = {
        "kind": "event_source",
        "source": {"type": source_type, "interval": interval},
        "extract": {"mode": extract_mode},
        "action": {"name": action},
        "allowed_actions": list(allowed_actions or [action]),
        "autonomy": autonomy,
        "enabled": bool(enabled),
    }
    if url:
        fm["source"]["url"] = url
    if extract_path:
        fm["extract"]["path"] = extract_path
    if condition:
        fm["condition"] = condition
    if action_params:
        fm["action"]["params"] = action_params
    if max_per_hour is not None:
        fm["rate_limit"] = {"max_per_hour": int(max_per_hour)}
    if cursor_from and cursor_from != "now":
        fm["cursor"] = {"from": cursor_from}
    if event_type:
        fm["event_type"] = event_type
    return fm


def validate_source_fm(name: str, fm: dict[str, Any]) -> list[str]:
    """Return validation errors (empty list = valid). Shared by the loader and
    the write op so a bad source is caught at author/load time."""
    errors: list[str] = []

    if not NAME_PATTERN.fullmatch(name or ""):
        errors.append(
            f"name {name!r} must be 1-64 chars, start alphanumeric, "
            "and contain only letters, digits, hyphens, or underscores."
        )
    if fm.get("kind") != "event_source":
        errors.append("frontmatter `kind` must be 'event_source'.")

    source = fm.get("source") or {}
    src_type = str(source.get("type", "")).strip()
    if src_type not in KNOWN_SOURCE_TYPES:
        errors.append(
            f"source.type {src_type!r} unknown (known: {sorted(KNOWN_SOURCE_TYPES)})."
        )
    if src_type == "http_poll" and not source.get("url"):
        errors.append("source.type=http_poll requires source.url.")
    if parse_interval(source.get("interval")) is None:
        errors.append(
            f"source.interval {source.get('interval')!r} is invalid "
            "(use e.g. '30s', '5m', '6h', '1d')."
        )

    extract = fm.get("extract") or {}
    mode = str(extract.get("mode", "hash"))
    if mode not in EXTRACT_MODES:
        errors.append(f"extract.mode {mode!r} invalid (known: {sorted(EXTRACT_MODES)}).")
    if mode in ("json_path", "css") and not extract.get("path"):
        errors.append(f"extract.mode={mode} requires extract.path.")
    if mode == "json_path" and extract.get("path"):
        try:
            import jsonpath_ng

            jsonpath_ng.parse(str(extract["path"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"extract.path is not valid JSONPath: {exc}")

    cond = fm.get("condition")
    if cond:
        try:
            import celpy

            celpy.Environment().compile(str(cond))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"condition is not valid CEL ({type(exc).__name__}): {exc}")

    action = fm.get("action") or {}
    action_name = action.get("name")
    if not action_name:
        errors.append("action.name is required.")
    elif action_name not in KNOWN_ACTIONS:
        errors.append(f"action.name {action_name!r} unknown (known: {sorted(KNOWN_ACTIONS)}).")

    allowed = fm.get("allowed_actions")
    if allowed is not None and not isinstance(allowed, list):
        errors.append("allowed_actions must be a list.")
    elif action_name and isinstance(allowed, list) and action_name not in allowed:
        errors.append(f"action.name {action_name!r} is not in allowed_actions {allowed}.")

    autonomy = str(fm.get("autonomy", "notify_only"))
    if autonomy not in AUTONOMY:
        errors.append(f"autonomy {autonomy!r} invalid (known: {sorted(AUTONOMY)}).")

    return errors
