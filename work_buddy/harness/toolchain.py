"""Resolve the rulesync executable used by harness projection."""

from __future__ import annotations

import shutil

from work_buddy.harness.model import HarnessConfig


def rulesync_command(config: HarnessConfig) -> list[str]:
    """Return a subprocess argv prefix for rulesync.

    A configured command wins. Otherwise prefer an installed `rulesync`, and
    fall back to a pinned npm package through npx for development/bootstrap.
    """

    if config.rulesync_command:
        return [config.rulesync_command]
    rulesync = shutil.which("rulesync")
    if rulesync:
        return [rulesync]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", f"rulesync@{config.rulesync_version}"]
    return ["rulesync"]
