from __future__ import annotations

import pytest

from work_buddy.mcp_server.ops.task_note_migration_ops import (
    task_note_migration_operator,
)
def test_per_note_markdown_operator_retires_after_native_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "work_buddy.tasks.runtime.native_authority_active",
        lambda: True,
    )

    result = task_note_migration_operator("inventory")

    assert result["success"] is False
    assert result["retired"] is True
    assert "Markdown migration operator is retired" in result["error"]
