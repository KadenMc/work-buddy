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
import os
import shutil
import sys
from pathlib import Path

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def provision(
    *,
    home: str | Path | None = None,
    data_dir: str | Path | None = None,
    vault_root: str | None = None,
    repos_root: str | None = None,
    timezone: str | None = None,
    anthropic_key: str | None = None,
    start: bool = True,
    harness: str | None = None,
    no_harness: bool = False,
    allow_experimental_harness: bool = False,
) -> dict:
    """Provision an installed work-buddy. Idempotent. Returns a structured result.

    ``home`` explicitly targets the install directory: it redirects ``config_dir``
    for this process so config, secrets, and ``.mcp.json`` land there regardless
    of the cwd or which ``work_buddy`` package is imported. Since ``config_dir()``
    is env-var-only by design, this is the one safe way to point provisioning at a
    specific home; without it, ``config_dir`` defaults to the running package's
    repo root, which is correct for an editable install but unsafe for a test run
    next to a live clone.
    """
    from work_buddy import paths
    from work_buddy.cli.commands import _mcp_config
    from work_buddy.compat import user_data_dir
    from work_buddy.health import fixers
    from work_buddy.health.requirements import RequirementChecker

    if home is not None:
        os.environ["WORK_BUDDY_CONFIG_DIR"] = str(Path(home).expanduser())

    if harness and no_harness:
        raise ValueError("--harness and --no-harness cannot be used together")
    harness_target = None
    if harness:
        from work_buddy.harness.registry import get_harness

        harness_target = get_harness(harness)
        if not harness_target.setup_ready and not allow_experimental_harness:
            raise ValueError(
                f"harness {harness!r} is not setup-ready yet: "
                f"{harness_target.setup_note}"
            )

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

    # 5. Refresh the Claude Code MCP wiring at the HOME (project) root. A default
    #    .mcp.json is committed to the repo, so a plain clone works in Claude Code
    #    with zero steps; this rewrite exists because the file's one variable
    #    field, the gateway port (sidecar.services.mcp_gateway.port), can differ
    #    per machine. Under default config the regenerated file is byte-identical
    #    to the committed one.
    (home / ".mcp.json").write_text(
        json.dumps(_mcp_config(), indent=2) + "\n", encoding="utf-8"
    )
    steps.append("wrote .mcp.json (gateway wiring)")

    # 5b. Publish the wbuddy CLI on the user's PATH (a one-command shim; the
    #     venv's Scripts/bin never lands on PATH). Best-effort: a failed shim
    #     must not fail the install — the venv CLI itself still works.
    try:
        from work_buddy import userpath

        steps.append("cli shim: " + str(userpath.install_cli_shim(home).get("detail")))
    except Exception as exc:
        steps.append(f"cli shim: skipped ({exc})")

    # 6. Bootstrap checks. Advisory: they guide the user's next steps but do not,
    #    by themselves, decide provisioning success. Feature config (vault_root,
    #    the Anthropic key) is deferred to `/wb-setup guided`, so those unmet
    #    required checks are expected on a fresh install and must NOT fail it.
    checker = RequirementChecker()
    boot_results = checker.check_bootstrap()
    summary = checker.summarize(boot_results)

    result = {
        "ok": False,  # decided after the install-critical work below
        "home": str(home),
        "data_dir": str(data),
        "steps": steps,
        "bootstrap": {
            "results": [r.to_dict() for r in boot_results],
            "summary": summary,
        },
        "sidecar": None,
        "harness": None,
    }

    # 6b. Optional first-run harness projection. Installer flows may pass one
    # setup-ready primary harness. Multi-harness setup remains a post-install
    # `wbuddy harness ...` operation.
    harness_ok = True
    if harness and not no_harness:
        from work_buddy.harness.config import save_harness_selection
        from work_buddy.harness.sync import sync_harnesses

        save_harness_selection(enabled=(harness,), primary=harness)
        sync = sync_harnesses(
            (harness,), output_root=home, install_toolchain=True
        )
        result["harness"] = {
            "id": harness,
            "ok": sync.ok,
            "setup_ready": harness_target.setup_ready,
            "setup_note": harness_target.setup_note,
            "generated_paths": sync.generated_paths,
            "error": sync.error,
            "returncode": sync.returncode,
            "warnings": list(getattr(sync, "warnings", [])),
            "backup_dir": (
                str(getattr(sync, "backup_dir", ""))
                if getattr(sync, "backup_dir", None)
                else None
            ),
        }
        harness_ok = sync.ok
        detail = "ok" if sync.ok else f"failed: {sync.error or sync.stderr}"
        steps.append(f"harness {harness}: {detail}")

    # 7. Start the sidecar (brings up the MCP gateway).
    if start:
        from work_buddy.cli import lifecycle

        result["sidecar"] = lifecycle.start_sidecar()

    # Provisioning succeeds when ITS OWN work completed: the data tree is writable
    # and (if requested) the sidecar came up. The MVP first-run state is "sidecar
    # up + MCP wired"; feature configuration comes later via the wizard, so unmet
    # feature-config checks are surfaced (in `bootstrap`) but never fail the install.
    sidecar_ok = (not start) or bool((result["sidecar"] or {}).get("started"))
    result["ok"] = bool(dw.get("ok", True)) and sidecar_ok and harness_ok

    return result


def uninstall(*, remove_data: bool = False) -> dict:
    """Tear down a provisioned install: stop the sidecar and tray, remove
    their login items, and remove the PATH shim.

    Removing the HOME working copy is left to the OS uninstaller (it knows the
    install path). The data dir is removed only when ``remove_data`` is set.
    """
    from work_buddy import autostart, paths, userpath
    from work_buddy.cli import lifecycle

    steps: list[str] = []
    steps.append("stop sidecar: " + str(lifecycle.stop_sidecar().get("detail")))
    # Tray teardown rides in the same call: stop the process, drop its login
    # item. Best-effort like the shim: a tray failure must not block uninstall.
    try:
        from work_buddy import tray

        steps.append("tray: " + str(tray.stop_running().get("detail")))
        steps.append(
            "tray login item: " + str(autostart.unregister_tray().get("detail"))
        )
    except Exception as exc:
        steps.append(f"tray: skipped ({exc})")
    steps.append("autostart: " + str(autostart.unregister().get("detail")))
    steps.append("cli shim: " + str(userpath.uninstall_cli_shim(paths.config_dir()).get("detail")))
    if remove_data:
        data = paths._data_base()
        shutil.rmtree(data, ignore_errors=True)
        steps.append(f"removed data dir {data}")
    return {"ok": True, "steps": steps}
