"""One-shot install provisioning: the logic behind ``wbuddy provision``.

The native installer, after it has unpacked the HOME working copy and created the
uv venv, invokes ``<venv>/python -m work_buddy.cli provision``. This module seeds
config, writes secrets, relocates the mutable-state tree to a hidden per-user data
dir, refreshes the Claude Code ``.mcp.json``, runs the core bootstrap checks, and
starts the sidecar. It is idempotent: re-running repairs rather than duplicates.

Model A layout. HOME (which is ``paths.config_dir()`` and ``paths.asset_root()``
under an editable install) holds the code, assets, config, and secrets; only the
mutable tree relocates to the data dir, via an absolute ``paths.data_root``. Both
``config.yaml`` and ``config.local.yaml`` are gitignored, so writing user values
into ``config.yaml`` never conflicts with a later update of the HOME working copy,
and no ``WORK_BUDDY_*`` env vars are needed at runtime.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def provision(
    *,
    data_dir: str | Path | None = None,
    vault_root: str | None = None,
    repos_root: str | None = None,
    timezone: str | None = None,
    anthropic_key: str | None = None,
    start: bool = True,
) -> dict:
    """Provision an installed work-buddy. Idempotent. Returns a structured result."""
    from work_buddy import paths
    from work_buddy.cli.commands import _mcp_config
    from work_buddy.compat import user_data_dir
    from work_buddy.health import fixers
    from work_buddy.health.requirements import RequirementChecker

    steps: list[str] = []
    home = paths.config_dir()
    asset = paths.asset_root()
    data = (Path(data_dir).expanduser() if data_dir else user_data_dir()).resolve()

    # 1. Seed config.yaml + config.local.yaml from the shipped templates if absent.
    if not (home / "config.yaml").exists():
        (home / "config.yaml").write_text(
            (asset / "config.example.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        steps.append("seeded config.yaml from config.example.yaml")
    if not (home / "config.local.yaml").exists():
        (home / "config.local.yaml").write_text(
            "# machine-local overrides\n", encoding="utf-8"
        )
        steps.append("created config.local.yaml stub")

    # 2. Relocate mutable state + pin the interpreter. This process IS the venv
    #    python the installer invoked, so sys.executable is the interpreter the
    #    supervised children should run under.
    fixers._set_config_value("paths.data_root", str(data))
    fixers._set_config_value("sidecar.python_executable", sys.executable)
    steps.append(f"data_root -> {data}; sidecar.python_executable pinned")

    # 3. User values (validated where a fixer exists; vault_root has none).
    if vault_root:
        fixers._set_config_value("vault_root", str(Path(vault_root).expanduser()))
        steps.append("vault_root set")
    if repos_root:
        steps.append("repos_root: " + fixers.fix_repos_root(path=repos_root)["detail"])
    if timezone:
        steps.append("timezone: " + fixers.fix_timezone(timezone=timezone)["detail"])
    if anthropic_key:
        steps.append(
            "anthropic key: " + fixers.fix_anthropic_api_key(api_key=anthropic_key)["detail"]
        )

    # 4. Ensure the data tree exists and is writable (creates it under data_root).
    dw = fixers.fix_data_writable()
    steps.append("data writable: " + str(dw.get("detail", dw)))

    # 5. Refresh the Claude Code MCP wiring at the HOME (project) root. Reuses the
    #    canonical config so the port can never drift from the bound gateway.
    (home / ".mcp.json").write_text(
        json.dumps(_mcp_config(), indent=2) + "\n", encoding="utf-8"
    )
    steps.append("wrote .mcp.json (gateway wiring)")

    # 6. Bootstrap checks.
    checker = RequirementChecker()
    boot_results = checker.check_bootstrap()
    summary = checker.summarize(boot_results)

    result = {
        "ok": bool(summary.get("all_required_pass", False)),
        "home": str(home),
        "data_dir": str(data),
        "steps": steps,
        "bootstrap": {
            "results": [r.to_dict() for r in boot_results],
            "summary": summary,
        },
        "sidecar": None,
    }

    # 7. Start the sidecar (brings up the MCP gateway).
    if start:
        from work_buddy.cli import lifecycle

        result["sidecar"] = lifecycle.start_sidecar()

    return result


def uninstall(*, remove_data: bool = False) -> dict:
    """Tear down a provisioned install: stop the sidecar and remove auto-start.

    Removing the HOME working copy is left to the OS uninstaller (it knows the
    install path). The data dir is removed only when ``remove_data`` is set.
    """
    from work_buddy import autostart, paths
    from work_buddy.cli import lifecycle

    steps: list[str] = []
    steps.append("stop sidecar: " + str(lifecycle.stop_sidecar().get("detail")))
    steps.append("autostart: " + str(autostart.unregister().get("detail")))
    if remove_data:
        data = paths._data_base()
        shutil.rmtree(data, ignore_errors=True)
        steps.append(f"removed data dir {data}")
    return {"ok": True, "steps": steps}
