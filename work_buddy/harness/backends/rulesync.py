"""rulesync subprocess backend."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from work_buddy.harness.config import load_harness_config
from work_buddy.harness.model import HarnessSyncResult, HarnessTarget
from work_buddy.harness.toolchain import rulesync_command


_FEATURE_ORDER = ("rules", "mcp", "commands", "skills", "hooks", "permissions")


class RulesyncBackend:
    def __init__(self, command: list[str] | None = None) -> None:
        cfg = load_harness_config()
        self.command = command or rulesync_command(cfg)

    def generate(
        self,
        *,
        input_root: Path,
        output_root: Path,
        targets: list[HarnessTarget],
        dry_run: bool = False,
        check: bool = False,
    ) -> HarnessSyncResult:
        rulesync_targets = ",".join(t.rulesync_target for t in targets)
        features = _feature_union(targets)
        argv = [
            *self.command,
            "--json",
            "generate",
            "--input-root",
            str(input_root),
            "--output-roots",
            str(output_root),
            "--targets",
            rulesync_targets,
            "--features",
            ",".join(features),
        ]
        if any(t.simulate_commands for t in targets):
            argv.append("--simulate-commands")
        if any(t.simulate_skills for t in targets):
            argv.append("--simulate-skills")
        if dry_run:
            argv.append("--dry-run")
        if check:
            argv.append("--check")

        try:
            proc = subprocess.run(argv, text=True, capture_output=True, check=False)
        except FileNotFoundError as exc:
            return HarnessSyncResult(
                ok=False,
                returncode=127,
                targets=tuple(t.id for t in targets),
                input_root=input_root,
                output_root=output_root,
                command=argv,
                dry_run=dry_run,
                check=check,
                error=f"rulesync executable not found: {exc.filename}",
            )
        data: dict = {}
        error = ""
        try:
            payload = json.loads(proc.stdout or proc.stderr or "{}")
            if isinstance(payload, dict):
                data = payload.get("data") or {}
                if payload.get("success") is False:
                    raw_error = payload.get("error") or "rulesync failed"
                    if isinstance(raw_error, dict):
                        error = str(raw_error.get("message") or raw_error)
                    else:
                        error = str(raw_error)
        except json.JSONDecodeError as exc:
            error = f"rulesync did not return JSON: {exc}"
        return HarnessSyncResult(
            ok=proc.returncode == 0 and not error,
            returncode=proc.returncode,
            targets=tuple(t.id for t in targets),
            input_root=input_root,
            output_root=output_root,
            command=argv,
            dry_run=dry_run,
            check=check,
            stdout=proc.stdout,
            stderr=proc.stderr,
            data=data,
            error=error,
        )


def _feature_union(targets: list[HarnessTarget]) -> list[str]:
    selected = {feature for t in targets for feature in t.features}
    return [feature for feature in _FEATURE_ORDER if feature in selected]
