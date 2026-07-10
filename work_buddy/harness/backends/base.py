"""Backend protocol for harness projection."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from work_buddy.harness.model import HarnessSyncResult, HarnessTarget


class HarnessBackend(Protocol):
    def generate(
        self,
        *,
        input_root: Path,
        output_root: Path,
        targets: list[HarnessTarget],
        dry_run: bool = False,
        check: bool = False,
    ) -> HarnessSyncResult:
        """Generate or check harness artifacts."""
