"""Requirement fixers — programmatic and input-required.

Each fixer is called from ``POST /api/control/fix/<req_id>`` after the
endpoint validates that the requirement opts into a fix and after
consent is granted.

Return shape::

    {"ok": bool, "detail": str, "side_effects": list[str]}

``side_effects`` is for the UI to show the user what changed (e.g.
list of files written, dirs created). Optional.

Fixers should be:
  * Idempotent — running twice produces the same end state.
  * Specific in their detail message — say what was created/changed.
  * Honest about partial failure — return ``ok=False`` if anything
    blocks completion; never raise (the dispatcher converts exceptions
    to {ok: False, detail: ...}).

Phase Fix-A: pipeline smoke test only (data/writable).
Phase Fix-B: filesystem creators — journal / contracts / personal-knowledge
             directories, daily-note section appenders, master task list.
Phase Fix-C: input_required fixers (repos-root, timezone, API keys, ...).
Phase Fix-D: agent-handoff fixers for genuinely complex setups.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vault_root() -> Path | None:
    """Resolve the configured Obsidian vault root, or None if missing."""
    from work_buddy.config import load_config
    raw = load_config().get("vault_root", "")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def _journal_dir() -> Path | None:
    """Configured journal directory inside the vault, or None if vault unset."""
    from work_buddy.config import load_config
    vault = _vault_root()
    if vault is None:
        return None
    rel = load_config().get("obsidian", {}).get("journal_dir", "journal")
    return vault / rel


def _ensure_dir(path: Path, side_effects: list[str]) -> tuple[bool, str]:
    """Create *path* if missing. Returns (ok, detail). Records the
    creation in side_effects."""
    if path.exists() and path.is_dir():
        return True, f"Already exists: {path}"
    if path.exists():
        return False, (
            f"{path} exists but is not a directory — manual cleanup needed."
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
        side_effects.append(f"Created {path}")
        return True, f"Created {path}"
    except OSError as exc:
        return False, f"Could not create {path}: {exc}"


# ---------------------------------------------------------------------------
# Smoke fix (Fix-A) — proves the dispatch pipeline end-to-end
# ---------------------------------------------------------------------------

def fix_data_writable() -> dict[str, Any]:
    """Create the ``data/`` directory at the repo root if it doesn't exist.

    Smoke test for the fix system. The check itself
    (``check_data_writable`` in requirement_checks.py) creates and
    deletes a sentinel file to verify writability — this fix just
    creates the directory if missing. If the dir exists but isn't
    writable (permissions issue), this fix can't help and returns
    ``ok=False``.
    """
    from work_buddy.paths import data_dir

    target = data_dir()
    side_effects: list[str] = []

    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=True)
            side_effects.append(f"Created {target}")
        except OSError as exc:
            return {
                "ok": False,
                "detail": f"Could not create {target}: {exc}",
                "side_effects": side_effects,
            }
    elif not target.is_dir():
        return {
            "ok": False,
            "detail": (
                f"{target} exists but is not a directory — manual cleanup "
                "required (it might be a stray file with that name)."
            ),
            "side_effects": [],
        }

    # Verify writability the same way the check does
    sentinel = target / ".wb-fix-sentinel"
    try:
        sentinel.write_text("ok", encoding="utf-8")
        sentinel.unlink()
    except OSError as exc:
        return {
            "ok": False,
            "detail": (
                f"{target} exists but is not writable (sentinel write "
                f"failed: {exc}). Check filesystem permissions."
            ),
            "side_effects": side_effects,
        }

    return {
        "ok": True,
        "detail": f"data/ directory ready at {target}",
        "side_effects": side_effects,
    }


# ---------------------------------------------------------------------------
# Fix-B — directory creators (vault subdirs)
# ---------------------------------------------------------------------------

def fix_journal_dir() -> dict[str, Any]:
    """Create the configured journal directory inside the vault."""
    side_effects: list[str] = []
    target = _journal_dir()
    if target is None:
        return {
            "ok": False,
            "detail": "vault_root not configured (or doesn't exist) — fix vault_root first.",
            "side_effects": side_effects,
        }
    ok, detail = _ensure_dir(target, side_effects)
    return {"ok": ok, "detail": detail, "side_effects": side_effects}


def fix_contracts_dir() -> dict[str, Any]:
    """Create the configured contracts directory inside the vault."""
    from work_buddy.config import load_config
    vault = _vault_root()
    if vault is None:
        return {
            "ok": False,
            "detail": "vault_root not configured — fix vault_root first.",
            "side_effects": [],
        }
    rel = load_config().get("contracts", {}).get("vault_path", "work-buddy/contracts")
    target = vault / rel
    side_effects: list[str] = []
    ok, detail = _ensure_dir(target, side_effects)
    return {"ok": ok, "detail": detail, "side_effects": side_effects}


def fix_personal_knowledge_dir() -> dict[str, Any]:
    """Create the configured personal knowledge directory inside the vault."""
    from work_buddy.config import load_config
    vault = _vault_root()
    if vault is None:
        return {
            "ok": False,
            "detail": "vault_root not configured — fix vault_root first.",
            "side_effects": [],
        }
    rel = load_config().get("personal_knowledge", {}).get("vault_path", "Meta/WorkBuddy")
    target = vault / rel
    side_effects: list[str] = []
    ok, detail = _ensure_dir(target, side_effects)
    return {"ok": ok, "detail": detail, "side_effects": side_effects}


def fix_master_task_list() -> dict[str, Any]:
    """Create the master task list file with a minimal seed if missing.

    The file is checked-for at ``<vault>/tasks/master-task-list.md``.
    Seeded with a heading + commented placeholder so the user has
    structure to start filling in.
    """
    vault = _vault_root()
    if vault is None:
        return {
            "ok": False,
            "detail": "vault_root not configured — fix vault_root first.",
            "side_effects": [],
        }
    target = vault / "tasks" / "master-task-list.md"
    side_effects: list[str] = []
    if target.exists():
        return {"ok": True, "detail": f"Already exists: {target}", "side_effects": []}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not (vault / "tasks").exists():
            side_effects.append(f"Created {vault / 'tasks'}")
        seed = (
            "# Master Task List\n\n"
            "Tasks added here are surfaced by the work-buddy Tasks panel.\n"
            "Each line should follow the Obsidian Tasks format:\n\n"
            "    - [ ] Example task #todo/inbox\n\n"
        )
        target.write_text(seed, encoding="utf-8")
        side_effects.append(f"Created {target}")
        return {
            "ok": True,
            "detail": f"Master task list seeded at {target}",
            "side_effects": side_effects,
        }
    except OSError as exc:
        return {
            "ok": False,
            "detail": f"Could not create {target}: {exc}",
            "side_effects": side_effects,
        }


# ---------------------------------------------------------------------------
# Fix-B — daily-note section appenders
# ---------------------------------------------------------------------------

def _latest_or_today_note() -> tuple[Path | None, str | None]:
    """Mirror of requirement_checks._find_latest_daily_note but returns
    today's path (creating it if missing) so the fix matches what the
    user expects: 'add the section to today's note'.

    If today's exists → today.
    Else if a recent note (within 30 days) exists → that one.
    Else → return today's path with a flag so the caller knows to create.
    """
    from datetime import date, timedelta
    journal = _journal_dir()
    if journal is None or not journal.exists():
        return None, None
    today = date.today()
    today_path = journal / f"{today.strftime('%Y-%m-%d')}.md"
    if today_path.exists():
        return today_path, "today"
    for i in range(1, 31):
        d = today - timedelta(days=i)
        candidate = journal / f"{d.strftime('%Y-%m-%d')}.md"
        if candidate.exists():
            return candidate, d.strftime("%Y-%m-%d")
    # No note within 30 days — caller will create today's
    return today_path, None


def _append_section(header: str) -> dict[str, Any]:
    """Append a markdown section to the latest available daily note.

    Idempotent: if the section already exists (case-insensitive header
    match), the file is left untouched and the fixer reports "already
    present". Creates today's note if no recent note exists.
    """
    import re

    note, which = _latest_or_today_note()
    if note is None:
        return {
            "ok": False,
            "detail": "Journal directory not configured/created — fix journal-dir first.",
            "side_effects": [],
        }

    side_effects: list[str] = []
    if not note.exists():
        # Create today's note with just this section
        try:
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(f"# {header}\n\n", encoding="utf-8")
            side_effects.append(f"Created {note}")
            return {
                "ok": True,
                "detail": f"Created today's note with '# {header}' section.",
                "side_effects": side_effects,
            }
        except OSError as exc:
            return {
                "ok": False,
                "detail": f"Could not create {note}: {exc}",
                "side_effects": side_effects,
            }

    # Note exists — check whether the header is already there
    try:
        content = note.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "detail": f"Could not read {note}: {exc}",
            "side_effects": side_effects,
        }

    pattern = rf"^#+\s+\**{re.escape(header)}"
    if re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
        prefix = "" if which == "today" else f"({which}) "
        return {
            "ok": True,
            "detail": f"{prefix}'# {header}' section already present in {note.name}",
            "side_effects": [],
        }

    # Append the section, preserving prior content. Add a blank line
    # before the header if needed.
    sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
    new_content = content + sep + f"# {header}\n\n"
    try:
        note.write_text(new_content, encoding="utf-8")
        side_effects.append(f"Appended '# {header}' to {note}")
        prefix = "" if which == "today" else f"(operating on last available daily note: {which}) "
        return {
            "ok": True,
            "detail": f"{prefix}Added '# {header}' section to {note.name}",
            "side_effects": side_effects,
        }
    except OSError as exc:
        return {
            "ok": False,
            "detail": f"Could not write {note}: {exc}",
            "side_effects": side_effects,
        }


def fix_log_section() -> dict[str, Any]:
    return _append_section("Log")


def fix_sign_in_section() -> dict[str, Any]:
    return _append_section("Sign-In")


def fix_running_notes_section() -> dict[str, Any]:
    return _append_section("Running Notes")


# ---------------------------------------------------------------------------
# Fix-C — input_required helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _set_env_var(name: str, value: str) -> tuple[bool, str, list[str]]:
    """Write a key=value line to the repo-root .env, replacing any
    existing line with the same key. Creates .env if missing.

    Returns (ok, detail, side_effects). The current process's
    ``os.environ`` is also updated so the value is visible without a
    restart — important for the immediate post-fix recheck.
    """
    import os

    side_effects: list[str] = []
    env_file = _repo_root() / ".env"

    # Update in-memory env first (so the recheck after the fix sees the new value)
    os.environ[name] = value

    if env_file.exists():
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return False, f"Could not read .env: {exc}", side_effects
        new_lines: list[str] = []
        replaced = False
        for line in lines:
            if line.startswith(f"{name}="):
                new_lines.append(f"{name}={value}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"{name}={value}")
            side_effects.append(f"Appended {name}= line to {env_file}")
        else:
            side_effects.append(f"Updated {name}= in {env_file}")
        try:
            env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        except OSError as exc:
            return False, f"Could not write .env: {exc}", side_effects
        return True, f"{name} set in .env (in-memory env also updated)", side_effects

    # No .env file yet — create one
    try:
        env_file.write_text(f"{name}={value}\n", encoding="utf-8")
        side_effects.append(f"Created {env_file} with {name}= line")
        return True, f"{name} set (created new .env file)", side_effects
    except OSError as exc:
        return False, f"Could not create .env: {exc}", side_effects


def _set_config_value(dotted_key: str, value: Any) -> tuple[bool, str, list[str]]:
    """Set a nested key in config.yaml (e.g. 'obsidian.journal_dir').

    Reads the current YAML, sets the value (creating intermediate dicts
    as needed), writes back. Triggers a config reload so subsequent
    checks see the new value.

    Returns (ok, detail, side_effects).
    """
    side_effects: list[str] = []
    config_file = _repo_root() / "config.yaml"

    if not config_file.exists():
        return False, (
            "config.yaml does not exist — fix that requirement first."
        ), side_effects

    try:
        import yaml
    except ImportError:
        return False, "PyYAML not available — cannot edit config.yaml.", side_effects

    try:
        existing = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        return False, f"Could not parse config.yaml: {exc}", side_effects

    # Navigate / create the nested key
    parts = dotted_key.split(".")
    cursor = existing
    for k in parts[:-1]:
        if k not in cursor or not isinstance(cursor[k], dict):
            cursor[k] = {}
        cursor = cursor[k]
    cursor[parts[-1]] = value

    try:
        config_file.write_text(
            yaml.safe_dump(existing, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        side_effects.append(f"Set {dotted_key}={value!r} in {config_file.name}")
    except OSError as exc:
        return False, f"Could not write config.yaml: {exc}", side_effects

    # Bust the config cache so the post-fix recheck sees the new value
    try:
        from work_buddy import config as _cfg_mod
        if hasattr(_cfg_mod, "_invalidate_cache"):
            _cfg_mod._invalidate_cache()
        elif hasattr(_cfg_mod, "_CONFIG"):
            _cfg_mod._CONFIG = None  # type: ignore[attr-defined]
    except Exception:
        pass  # config will reload on next request anyway

    return True, f"{dotted_key} set to {value!r}", side_effects


# ---------------------------------------------------------------------------
# Fix-C — input_required fixers
# ---------------------------------------------------------------------------

def fix_repos_root(*, path: str) -> dict[str, Any]:
    """Set ``repos_root`` in config.yaml to a directory path.

    Validates the path is a real directory before writing — half-broken
    config is worse than the previous half-broken config.
    """
    p = Path(path).expanduser()
    if not p.is_dir():
        return {
            "ok": False,
            "detail": f"Path is not an existing directory: {p}",
            "side_effects": [],
        }
    ok, detail, side = _set_config_value("repos_root", str(p))
    return {"ok": ok, "detail": detail, "side_effects": side}


def fix_timezone(*, timezone: str) -> dict[str, Any]:
    """Set ``timezone`` in config.yaml to a valid IANA timezone."""
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        return {"ok": False, "detail": "zoneinfo not available", "side_effects": []}

    try:
        ZoneInfo(timezone)  # validate
    except (ZoneInfoNotFoundError, KeyError):
        return {
            "ok": False,
            "detail": f"Not a valid IANA timezone: {timezone!r}. Try e.g. 'America/Toronto'.",
            "side_effects": [],
        }
    ok, detail, side = _set_config_value("timezone", timezone)
    return {"ok": ok, "detail": detail, "side_effects": side}


def fix_anthropic_api_key(*, api_key: str) -> dict[str, Any]:
    """Write SUBAGENT_ANTHROPIC_API_KEY to .env. Preferred over
    ANTHROPIC_API_KEY because spawned Claude Code sessions can fall
    back to OAuth/Claude Max when ANTHROPIC_API_KEY is intentionally
    absent — see work_buddy/llm/runner.py:214 for the precedence."""
    api_key = (api_key or "").strip()
    if not api_key:
        return {"ok": False, "detail": "API key cannot be empty.", "side_effects": []}
    if not api_key.startswith("sk-"):
        return {
            "ok": False,
            "detail": "Anthropic API keys start with 'sk-'. Got something else.",
            "side_effects": [],
        }
    ok, detail, side = _set_env_var("SUBAGENT_ANTHROPIC_API_KEY", api_key)
    return {"ok": ok, "detail": detail, "side_effects": side}


def fix_telegram_bot_token(*, bot_token: str) -> dict[str, Any]:
    """Write TELEGRAM_BOT_TOKEN to .env."""
    bot_token = (bot_token or "").strip()
    if not bot_token:
        return {"ok": False, "detail": "Bot token cannot be empty.", "side_effects": []}
    # Telegram bot tokens look like "<digits>:<base64-ish>" — basic shape check
    if ":" not in bot_token or len(bot_token) < 20:
        return {
            "ok": False,
            "detail": (
                "That doesn't look like a Telegram bot token. They are "
                "shaped like '123456789:AA…' (digits, colon, then ~35 chars)."
            ),
            "side_effects": [],
        }
    ok, detail, side = _set_env_var("TELEGRAM_BOT_TOKEN", bot_token)
    return {"ok": ok, "detail": detail, "side_effects": side}


# ---------------------------------------------------------------------------
# Remote access — Tailscale Serve
# ---------------------------------------------------------------------------

def fix_tailscale_serve_configured() -> dict[str, Any]:
    """Publish the dashboard via Tailscale Serve.

    Idempotent — running ``tailscale serve --bg <port>`` against an
    already-configured handler updates in place. Reads the dashboard
    port from config so a non-default sidecar deployment still publishes
    correctly. HTTPS is Serve's default mode; the CLI takes a single
    target argument (port number or URL).
    """
    side_effects: list[str] = []

    try:
        from work_buddy.config import load_config
        port = (
            load_config()
            .get("sidecar", {})
            .get("services", {})
            .get("dashboard", {})
            .get("port", 5127)
        )
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"Could not read dashboard port from config: {exc}",
            "side_effects": side_effects,
        }

    import subprocess
    try:
        proc = subprocess.run(
            ["tailscale", "serve", "--bg", str(port)],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "detail": (
                "tailscale CLI not found on PATH. Fix the "
                "integrations/tailscale/installed requirement first."
            ),
            "side_effects": side_effects,
        }
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"`tailscale serve` invocation failed: {exc}",
            "side_effects": side_effects,
        }

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:300]
        return {
            "ok": False,
            "detail": f"`tailscale serve` exited {proc.returncode}: {stderr}",
            "side_effects": side_effects,
        }

    side_effects.append(f"tailscale serve --bg {port}")

    # Bust the helper's memo so the post-fix recheck reads fresh state.
    try:
        from work_buddy.health.checks import get_tailscale_status
        get_tailscale_status(force=True)
    except Exception:
        pass

    return {
        "ok": True,
        "detail": f"Tailscale Serve now publishes the dashboard (port {port}) over HTTPS",
        "side_effects": side_effects,
    }


# ---------------------------------------------------------------------------
# github_backups
# ---------------------------------------------------------------------------

def fix_backup_repo_configured(repo_name: str) -> dict[str, Any]:
    """Set ``backups.github.repo`` in config.local.yaml; create the
    repo via ``gh repo create --private`` if it doesn't already exist.

    Idempotent: if the repo already exists on GitHub AND the config
    already points at it, this is a no-op (still returns ok=True).

    Args:
        repo_name: ``<user>/<repo>`` or just ``<repo>`` (gh defaults
            to the authenticated user's account in the latter case).

    Returns the standard ``{ok, detail, side_effects}`` shape.
    """
    import subprocess

    side_effects: list[str] = []
    if not repo_name or not repo_name.strip():
        return {"ok": False, "detail": "repo_name is empty",
                "side_effects": side_effects}
    repo_name = repo_name.strip()

    # Step 1: write config first so a subsequent gh repo view (or any
    # downstream caller) reads the new value.
    ok, detail, side = _set_config_value("backups.github.repo", repo_name)
    if not ok:
        return {
            "ok": False,
            "detail": f"Could not write backups.github.repo to config: {detail}",
            "side_effects": side,
        }
    side_effects.extend(side)

    # Step 2: check whether the repo exists on GitHub.
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", repo_name, "--json", "name"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "detail": ("gh CLI not on PATH — install it first "
                       "(gh-cli-installed requirement)."),
            "side_effects": side_effects,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "detail": "gh repo view timed out — check connectivity to github.com",
            "side_effects": side_effects,
        }

    if proc.returncode == 0:
        return {
            "ok": True,
            "detail": f"Config set; repo {repo_name} already exists on GitHub.",
            "side_effects": side_effects,
        }

    # Repo doesn't exist (or auth issue). Try creating it.
    stderr = (proc.stderr or "").strip()
    lower = stderr.lower()
    if "could not resolve to a repository" not in lower and "not found" not in lower:
        # Some other error (auth, rate limit, etc.) — surface it.
        return {
            "ok": False,
            "detail": f"gh repo view failed before create: {stderr}",
            "side_effects": side_effects,
        }

    # Step 3: create as PRIVATE with an auto-generated README so the
    # repo has a default branch from the start. Without ``--add-readme``,
    # ``gh release create`` later fails with "Repository is empty" —
    # GitHub Releases require at least one commit / a default branch
    # to attach the tag to.
    try:
        create = subprocess.run(
            ["gh", "repo", "create", repo_name,
             "--private",
             "--add-readme",
             "--description", "work-buddy data backups (auto-managed)"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "detail": "gh repo create timed out",
            "side_effects": side_effects,
        }
    if create.returncode != 0:
        return {
            "ok": False,
            "detail": ("gh repo create failed: "
                       f"{(create.stderr or '').strip()}"),
            "side_effects": side_effects,
        }
    side_effects.append(f"gh repo create {repo_name} --private --add-readme")
    return {
        "ok": True,
        "detail": (
            f"Created private repo {repo_name} (seeded with a README so "
            "GitHub Releases can attach) and wrote config."
        ),
        "side_effects": side_effects,
    }


def fix_projects_markdown_dir(*, path: str) -> dict[str, Any]:
    """Create the project-notes directory and set ``projects.markdown_dir``.

    ``path`` is vault-relative. Unlike :func:`fix_repos_root` (which
    requires a pre-existing directory), this directory is work-buddy's
    own to create — so the fixer makes it if absent, then writes config.
    """
    from work_buddy.config import load_config

    rel = path.strip().strip("/\\")
    if not rel:
        return {
            "ok": False,
            "detail": "Path must be a non-empty vault-relative directory.",
            "side_effects": [],
        }
    vault = load_config().get("vault_root", "")
    if not vault:
        return {
            "ok": False,
            "detail": "vault_root is not set — set it before this requirement.",
            "side_effects": [],
        }
    target = Path(vault) / rel
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "detail": f"Could not create directory {target}: {exc}",
            "side_effects": [],
        }
    ok, detail, side = _set_config_value("projects.markdown_dir", rel)
    side = list(side) + [f"created directory {target}"]
    return {"ok": ok, "detail": detail, "side_effects": side}


# ---------------------------------------------------------------------------
# Google Calendar — native OAuth
# ---------------------------------------------------------------------------


def fix_google_oauth_client_secret(*, client_secret_path: str) -> dict[str, Any]:
    """Copy your downloaded client_secret.json into the convention location
    (``<data_root>/credentials/google_client_secret.json``) where it is
    auto-discovered — no env var needed."""
    import json
    import shutil

    from work_buddy import paths

    path = (client_secret_path or "").strip().strip('"')
    if not path:
        return {"ok": False, "detail": "Path cannot be empty.", "side_effects": []}
    src = Path(path)
    if not src.is_file():
        return {
            "ok": False,
            "detail": (
                f"No file at {src}. Download the Desktop-app OAuth client's "
                "client_secret.json from Google Cloud Console first."
            ),
            "side_effects": [],
        }
    # Validate it's actually a Desktop-app client secret before copying.
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "detail": f"Could not read {src} as JSON: {exc}", "side_effects": []}
    if "installed" not in data or "client_id" not in data.get("installed", {}):
        return {
            "ok": False,
            "detail": (
                "That file isn't a Desktop-app OAuth client secret (no "
                "'installed' object). In Google Cloud Console create an OAuth "
                "client of type 'Desktop app' and download its JSON."
            ),
            "side_effects": [],
        }
    dest = paths.resolve("credentials/google-client-secret")
    try:
        shutil.copyfile(src, dest)
    except OSError as exc:
        return {"ok": False, "detail": f"Could not copy to {dest}: {exc}", "side_effects": []}
    return {
        "ok": True,
        "detail": f"Client secret installed at {dest} (auto-discovered).",
        "side_effects": [f"wrote {dest}"],
    }


def fix_google_oauth_token() -> dict[str, Any]:
    """Run the interactive Google OAuth consent flow and persist the token.

    Opens a browser and round-trips to a localhost loopback port — requires a
    user-present desktop session and the client secret already configured.
    """
    try:
        from work_buddy.calendar import google_auth
        from work_buddy.config import load_config

        cfg = ((load_config() or {}).get("calendar", {}) or {}).get("google_native", {}) or {}
        token_path = google_auth.run_oauth_flow(cfg)
        return {
            "ok": True,
            "detail": f"OAuth consent complete — refresh token saved to {token_path}.",
            "side_effects": [f"wrote {token_path}"],
        }
    except Exception as exc:
        return {"ok": False, "detail": f"OAuth flow failed: {exc}", "side_effects": []}
