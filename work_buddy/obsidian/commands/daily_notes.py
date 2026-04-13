"""Daily notes command wrappers."""

from work_buddy.obsidian.commands import ObsidianCommands


class DailyNotesCommands:
    """Daily note navigation and creation."""

    def __init__(self, client: ObsidianCommands):
        self._client = client

    def open_today(self) -> bool:
        """Open today's daily note (creates it from template if needed)."""
        return self._client.execute("daily-notes")

    def open_previous(self) -> bool:
        """Navigate to the previous daily note."""
        return self._client.execute("daily-notes:goto-prev")

    def open_next(self) -> bool:
        """Navigate to the next daily note."""
        return self._client.execute("daily-notes:goto-next")

    def open_date(self, date_str: str) -> bool:
        """Open a specific daily note by date (YYYY-MM-DD).

        Uses the file-open API endpoint since there's no date-specific command.
        """
        return self._client.open_file(f"journal/{date_str}.md")
