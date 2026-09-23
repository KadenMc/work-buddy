"""Cross-platform compatibility helpers.

Centralizes platform detection and OS-appropriate path resolution. Starting
and ending processes belongs to ``work_buddy.process``.
"""

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def obsidian_log_path() -> Path:
    """Resolve the Obsidian main process log file path for the current OS."""
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            appdata = str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "obsidian" / "obsidian.log"
    elif IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.log"
    else:
        # Linux: XDG_CONFIG_HOME or ~/.config
        config_home = os.environ.get("XDG_CONFIG_HOME", "")
        if not config_home:
            config_home = str(Path.home() / ".config")
        return Path(config_home) / "obsidian" / "obsidian.log"


def chrome_native_messaging_dir() -> Path:
    """Resolve Chrome's native messaging hosts directory for the current OS."""
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "Google" / "Chrome" / "NativeMessagingHosts"
    elif IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
    else:
        return Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"


def user_data_dir() -> Path:
    """Per-user data directory for a packaged (non-clone) work-buddy install.

    Windows ``%LOCALAPPDATA%\\work-buddy``, macOS
    ``~/Library/Application Support/work-buddy``, Linux ``$XDG_DATA_HOME/
    work-buddy`` (or ``~/.local/share/work-buddy``). Mirrors the platform
    branching of :func:`obsidian_log_path` / :func:`chrome_native_messaging_dir`.
    This is where the sidecar's mutable state (DBs, caches, logs, consent) lives
    when work-buddy is installed rather than cloned; the HOME working copy keeps
    the code, assets, config, and secrets.
    """
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "work-buddy"
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "work-buddy"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "work-buddy"
