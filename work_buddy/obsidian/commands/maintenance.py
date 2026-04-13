"""Vault maintenance command wrappers."""

from work_buddy.consent import requires_consent
from work_buddy.obsidian.commands import ObsidianCommands


class MaintenanceCommands:
    """Vault maintenance operations — linting, checklist reset, etc."""

    def __init__(self, client: ObsidianCommands):
        self._client = client

    def lint_current_file(self) -> bool:
        """Run the Linter on the currently active file."""
        return self._client.execute("obsidian-linter:lint-file")

    @requires_consent(
        operation="reset_checklists",
        reason="Resets all checkboxes in the current file, clearing completion state",
        risk="moderate",
        default_ttl=5,
    )
    def reset_checklists(self) -> bool:
        """Reset all checkboxes in the current file."""
        return self._client.execute("obsidian-checklist-reset:checklist-reset")

    def check_all(self) -> bool:
        """Check all checkboxes in the current file."""
        return self._client.execute("obsidian-checklist-reset:checklist-check-all")

    def create_from_template(self) -> bool:
        """Open Templater's template selection modal."""
        return self._client.execute(
            "templater-obsidian:create-new-note-from-template"
        )

    def insert_template(self) -> bool:
        """Open Templater's insert template modal for the active file."""
        return self._client.execute("templater-obsidian:insert-templater")
