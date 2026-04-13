"""QuickAdd command wrappers.

QuickAdd choices are identified by UUIDs. This module reads the QuickAdd
config to map friendly names to their command IDs.
"""

from pathlib import Path

from work_buddy.obsidian.plugins import plugin_config, require_plugins
from work_buddy.obsidian.commands import ObsidianCommands


def _get_quickadd_choices(vault_root: Path) -> dict[str, str]:
    """Map QuickAdd choice names to their command IDs.

    Returns {name: "quickadd:choice:<uuid>"} for all configured choices.
    """
    cfg = plugin_config(vault_root, "quickadd")
    choices = cfg.get("choices", [])
    result = {}
    for choice in choices:
        name = choice.get("name", "")
        uid = choice.get("id", "")
        if name and uid:
            result[name] = f"quickadd:choice:{uid}"
    return result


class QuickAddCommands:
    """QuickAdd-specific command execution."""

    def __init__(self, client: ObsidianCommands):
        self._client = client
        self._choices = _get_quickadd_choices(client.vault_root)

    @property
    def available_choices(self) -> dict[str, str]:
        """Return {name: command_id} for all QuickAdd choices."""
        return dict(self._choices)

    def run_choice(self, name: str) -> bool:
        """Execute a QuickAdd choice by its friendly name.

        Raises KeyError if the choice name is not found.
        """
        cmd_id = self._choices.get(name)
        if cmd_id is None:
            available = ", ".join(sorted(self._choices.keys()))
            raise KeyError(
                f"QuickAdd choice '{name}' not found. Available: {available}"
            )
        return self._client.execute(cmd_id)

    def trigger_log_entry(self) -> bool:
        """Trigger the daily_note_log_entry capture.

        Opens QuickAdd's input modal for a timestamped log entry.
        """
        return self.run_choice("daily_note_log_entry")

    def trigger_new_task(self) -> bool:
        """Trigger the new_task_no_note capture."""
        return self.run_choice("new_task_no_note")

    def trigger_running_entry(self) -> bool:
        """Trigger the daily_note_running_entry capture."""
        return self.run_choice("daily_note_running_entry")
