"""rulesync subprocess backend."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from work_buddy.harness.config import load_harness_config
from work_buddy.harness.model import HarnessSyncResult, HarnessTarget
from work_buddy.harness.toolchain import rulesync_command


_FEATURE_ORDER = ("rules", "mcp", "commands", "skills", "hooks", "permissions")


class RulesyncBackend:
    def __init__(
        self,
        command: list[str] | None = None,
        *,
        install_toolchain: bool = False,
    ) -> None:
        cfg = load_harness_config()
        self.command = command or rulesync_command(cfg, install=install_toolchain)

    def generate(
        self,
        *,
        input_root: Path,
        output_root: Path,
        targets: list[HarnessTarget],
        dry_run: bool = False,
        check: bool = False,
    ) -> HarnessSyncResult:
        if len(targets) > 1:
            return self._generate_targets(
                input_root=input_root,
                output_root=output_root,
                targets=targets,
                dry_run=dry_run,
                check=check,
            )
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

    def _generate_targets(
        self,
        *,
        input_root: Path,
        output_root: Path,
        targets: list[HarnessTarget],
        dry_run: bool,
        check: bool,
    ) -> HarnessSyncResult:
        """Give each renderer only its declared servers, then combine results.

        MCP adapters differ in whether they honor per-server target metadata.
        Filtering before invoking them keeps host-specific tools scoped correctly.
        The caller still previews and rolls back the complete generated path set.
        """
        results: list[HarnessSyncResult] = []
        with TemporaryDirectory(prefix="wb-harness-projection-") as staging:
            for target in targets:
                target_input = Path(staging) / target.id
                root = target_input / ".rulesync"
                shutil.copytree(input_root / ".rulesync", root)
                mcp_path = root / "mcp.json"
                if mcp_path.is_file():
                    config = json.loads(mcp_path.read_text(encoding="utf-8"))
                    config["mcpServers"] = {
                        name: server
                        for name, server in config.get("mcpServers", {}).items()
                        if "*" in server.get("targets", ["*"])
                        or target.rulesync_target in server.get("targets", ["*"])
                    }
                    mcp_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
                result = self.generate(
                    input_root=target_input,
                    output_root=output_root,
                    targets=[target],
                    dry_run=dry_run,
                    check=check,
                )
                results.append(result)
                if not result.ok:
                    break

        features: dict[str, dict] = {}
        for result in results:
            for name, feature in result.data.get("features", {}).items():
                combined = features.setdefault(name, {"count": 0, "paths": []})
                combined["paths"] = sorted(set(combined["paths"] + feature.get("paths", [])))
                combined["count"] = len(combined["paths"])
        failed = next((result for result in results if not result.ok), None)
        data = {
            "features": features,
            "totalFiles": len({path for result in results for path in result.generated_paths}),
            "projection_commands": [result.command for result in results],
        }
        if any(result.has_diff is not None for result in results):
            data["hasDiff"] = any(result.has_diff for result in results)
        return HarnessSyncResult(
            ok=failed is None,
            returncode=failed.returncode if failed else 0,
            targets=tuple(target.id for target in targets),
            input_root=input_root,
            output_root=output_root,
            command=results[0].command,
            dry_run=dry_run,
            check=check,
            stdout="\n".join(result.stdout for result in results if result.stdout),
            stderr="\n".join(result.stderr for result in results if result.stderr),
            data=data,
            error=failed.error if failed else "",
        )


def _feature_union(targets: list[HarnessTarget]) -> list[str]:
    selected = {feature for t in targets for feature in t.features}
    return [feature for feature in _FEATURE_ORDER if feature in selected]
