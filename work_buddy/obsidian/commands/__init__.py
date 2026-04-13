"""Obsidian command execution via Local REST API."""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from work_buddy.obsidian.plugins import plugin_config


def _get_api_key(vault_root: Path) -> str:
    """Auto-read the API key from the REST API plugin config."""
    cfg = plugin_config(vault_root, "obsidian-local-rest-api")
    key = cfg.get("apiKey", "")
    if not key:
        raise RuntimeError(
            "Cannot read API key from obsidian-local-rest-api plugin. "
            "Is the plugin installed and configured?"
        )
    return key


def _get_api_port(vault_root: Path) -> int:
    """Read the API port from plugin config."""
    cfg = plugin_config(vault_root, "obsidian-local-rest-api")
    return cfg.get("port", 27124)


# SSL context that trusts the self-signed certificate
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


class ObsidianCommands:
    """Client for executing Obsidian commands via the Local REST API.

    Requires Obsidian to be running with the Local REST API plugin enabled.
    API key is auto-read from the plugin's data.json.
    """

    def __init__(self, vault_root: Path, port: int | None = None):
        self.vault_root = vault_root
        self._api_key = _get_api_key(vault_root)
        self._port = port or _get_api_port(vault_root)
        # Try HTTPS first, fall back to HTTP if the insecure port is enabled
        self._base_url = f"https://127.0.0.1:{self._port}"
        self._use_ssl = True

    def _request(
        self,
        method: str,
        path: str,
        data: dict | str | None = None,
        content_type: str = "application/json",
    ) -> dict[str, Any] | str | None:
        """Make an authenticated request to the Obsidian REST API."""
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
        }

        body = None
        if data is not None:
            if isinstance(data, dict):
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"
            else:
                body = data.encode("utf-8")
                headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        ctx = _ssl_ctx if self._use_ssl else None

        try:
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                if resp.headers.get("Content-Type", "").startswith("application/json"):
                    return json.loads(raw) if raw else {}
                return raw
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"Obsidian API error {e.code}: {e.read().decode('utf-8', errors='replace')}"
            ) from e

    def is_available(self) -> bool:
        """Check if Obsidian is running and the API is reachable."""
        try:
            self._request("GET", "/")
            return True
        except (OSError, RuntimeError, urllib.error.URLError):
            return False

    def list_commands(self) -> list[dict[str, str]]:
        """List all available Obsidian commands.

        Returns list of {"id": "...", "name": "..."} dicts.
        """
        result = self._request("GET", "/commands/")
        if isinstance(result, dict):
            return result.get("commands", [])
        return []

    def require_available(self) -> None:
        """Raise RuntimeError if Obsidian is not running or API is unreachable."""
        if not self.is_available():
            raise RuntimeError(
                "Obsidian is not running or the Local REST API is unreachable. "
                "Please open Obsidian and ensure the Local REST API plugin is enabled."
            )

    def execute(self, command_id: str) -> bool:
        """Execute an Obsidian command by ID.

        Returns True on success, raises RuntimeError if Obsidian is not running.
        """
        self.require_available()
        self._request("POST", f"/commands/{command_id}")
        return True

    def search(self, query: str) -> list[dict]:
        """Run Obsidian's built-in search.

        Returns list of matching files with context snippets.
        """
        result = self._request("POST", f"/search/simple/?query={urllib.parse.quote(query)}")
        if isinstance(result, list):
            return result
        return []

    def open_file(self, vault_path: str) -> bool:
        """Open a file in the Obsidian UI.

        Args:
            vault_path: Path relative to vault root (e.g., "journal/2026-04-01.md")
        """
        self._request("POST", f"/open/{vault_path}")
        return True


# Re-export for convenience
from work_buddy.obsidian.commands.quickadd import QuickAddCommands  # noqa: E402
from work_buddy.obsidian.commands.dataview import DataviewCommands  # noqa: E402
from work_buddy.obsidian.commands.daily_notes import DailyNotesCommands  # noqa: E402
from work_buddy.obsidian.commands.maintenance import MaintenanceCommands  # noqa: E402

__all__ = [
    "ObsidianCommands",
    "QuickAddCommands",
    "DataviewCommands",
    "DailyNotesCommands",
    "MaintenanceCommands",
]
