"""Harness config read/write helpers."""

from __future__ import annotations

from work_buddy import config as wb_config
from work_buddy.harness.model import HarnessConfig
from work_buddy.harness.registry import get_harness


def load_harness_config() -> HarnessConfig:
    cfg = (wb_config.load_config().get("harness") or {})
    rulesync = cfg.get("rulesync") or {}
    enabled = tuple(str(x) for x in (cfg.get("enabled") or []))
    primary = str(cfg.get("primary") or "")
    version = str(rulesync.get("version") or "9.6.0")
    command = str(rulesync.get("command") or "")
    return HarnessConfig(
        enabled=enabled,
        primary=primary,
        rulesync_version=version,
        rulesync_command=command,
    )


def save_harness_selection(
    *, enabled: tuple[str, ...] | None = None, primary: str | None = None
) -> HarnessConfig:
    local = wb_config.read_config_local()
    current = local.get("harness") or {}
    rulesync = current.get("rulesync") or {}

    next_enabled = tuple(current.get("enabled") or ())
    next_primary = str(current.get("primary") or "")

    if enabled is not None:
        for harness_id in enabled:
            get_harness(harness_id)
        next_enabled = tuple(dict.fromkeys(enabled))
    if primary is not None:
        if primary:
            get_harness(primary)
        next_primary = primary

    data = {
        "primary": next_primary,
        "enabled": list(next_enabled),
        "rulesync": {
            "version": str(rulesync.get("version") or "9.6.0"),
            "command": str(rulesync.get("command") or ""),
        },
    }
    wb_config.write_config_local("harness", data)
    return HarnessConfig(
        enabled=tuple(data["enabled"]),
        primary=data["primary"],
        rulesync_version=data["rulesync"]["version"],
        rulesync_command=data["rulesync"]["command"],
    )
