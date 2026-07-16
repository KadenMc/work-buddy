"""Shared isolated fixtures for truth-kernel unit tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def truth_root(tmp_path: Path) -> Path:
    """Return an isolated scope root for one truth store."""
    root = tmp_path / "scope"
    root.mkdir()
    return root


@pytest.fixture
def profile_writer() -> Callable[..., Path]:
    """Write a minimal store profile without importing the engine."""

    def _write(
        root: Path,
        *,
        profile: str = "test",
        store_id: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Path:
        from work_buddy.truth.identity import new_id

        sidecar = root / ".wb-truth"
        sidecar.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "store_id": store_id or new_id(),
            "profile": profile,
            "title": "Test truth store",
            "allowed_claim_kinds": ["fact", "preference"],
            "required_fields": {},
            "gate": {
                "rejected_content": "redact",
                "confirmation_surfaces": ["dashboard", "cli", "chat_consent"],
                "block_materialize_on_flags": False,
            },
            "projection": "none",
            "export_committed": True,
        }
        if overrides:
            payload.update(overrides)
        path = sidecar / "store.yaml"
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )
        return path

    return _write
