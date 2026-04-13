"""Dataview and Dataview Serializer command wrappers."""

from work_buddy.consent import requires_consent
from work_buddy.obsidian.commands import ObsidianCommands


class DataviewCommands:
    """Dataview-specific command execution."""

    def __init__(self, client: ObsidianCommands):
        self._client = client

    def force_refresh(self) -> bool:
        """Force refresh all Dataview views and blocks."""
        return self._client.execute("dataview:dataview-force-refresh-views")

    def drop_cache(self) -> bool:
        """Drop all cached Dataview file metadata."""
        return self._client.execute("dataview:dataview-drop-cache")

    def rebuild_current_view(self) -> bool:
        """Rebuild the current Dataview view."""
        return self._client.execute("dataview:dataview-rebuild-current-view")

    @requires_consent(
        operation="serialize_all_dataview",
        reason="Serializes all Dataview queries across the entire vault into static markdown",
        risk="moderate",
        default_ttl=10,
    )
    def serialize_all(self) -> bool:
        """Serialize all Dataview queries in the vault to static markdown.

        This bakes live query results into the files so they persist
        in git and are readable without Obsidian running.
        """
        return self._client.execute(
            "dataview-serializer:serialize-all-dataview-queries"
        )

    def serialize_current_file(self) -> bool:
        """Serialize Dataview queries in the currently open file."""
        return self._client.execute(
            "dataview-serializer:serialize-current-file-dataview-queries"
        )
