"""Typed models for agent-host harness projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HarnessTarget:
    """A supported agent host and its rulesync projection shape."""

    id: str
    label: str
    rulesync_target: str
    description: str
    features: tuple[str, ...]
    simulate_skills: bool = False
    simulate_commands: bool = False
    setup_ready: bool = True
    setup_note: str = ""
    support_tier: str = "first_class"
    session_env: str = ""
    transcript_provider: str = ""
    lifecycle_events: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessConfig:
    enabled: tuple[str, ...] = ()
    primary: str = ""
    rulesync_version: str = "9.6.0"
    rulesync_command: str = ""


@dataclass
class HarnessSyncResult:
    """Result of a rulesync projection run."""

    ok: bool
    returncode: int
    targets: tuple[str, ...]
    input_root: Path
    output_root: Path
    command: list[str]
    dry_run: bool = False
    check: bool = False
    stdout: str = ""
    stderr: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    backup_dir: Path | None = None

    @property
    def generated_paths(self) -> list[str]:
        features = (self.data or {}).get("features") or {}
        paths: list[str] = []
        for info in features.values():
            paths.extend(info.get("paths") or [])
        return sorted(set(paths))

    @property
    def has_diff(self) -> bool | None:
        if "hasDiff" in self.data:
            return bool(self.data["hasDiff"])
        return None

    @property
    def total_files(self) -> int | None:
        val = self.data.get("totalFiles")
        return int(val) if isinstance(val, int) else None
