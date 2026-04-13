"""Collect project identity, state, and trajectory from multiple signals.

Synthesizes a project inventory from:
1. Vault project directories (work/projects/*)
2. STATE.md files in repos
3. Task project tags (#projects/<slug>)
4. Git repo activity
5. Chat session project names
6. Contracts
"""

import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Lifecycle directories under work/projects/ that imply status, not identity
_LIFECYCLE_DIRS = {"projects-past", "projects-future"}

# Files to skip when scanning vault project directories
_SKIP_FILES = {"projects.md"}  # Waypoint auto-generated folder note (often corrupted)


# ── Slug normalization ──────────────────────────────────────────

def _normalize_slug(name: str) -> str:
    """Normalize a project name to a canonical slug: lowercase, hyphens."""
    return name.lower().replace("_", "-").replace(" ", "-")


def _resolve_alias(slug: str, cfg: dict) -> str:
    """Resolve a slug through the alias map. Returns canonical slug.

    The alias map lives under cfg["projects"]["aliases"] and maps
    alternative names (repo slugs, task tag slugs) to the canonical
    vault directory slug.
    """
    aliases = cfg.get("projects", {}).get("aliases", {})
    return aliases.get(slug, slug)


# ── Signal extractors ───────────────────────────────────────────

def _scan_vault_projects(vault_root: Path) -> list[dict[str, Any]]:
    """Discover projects from work/projects/ directory structure.

    Returns a list of project dicts with slug, status, and last_modified.
    Directories directly under work/projects/ (excluding lifecycle dirs)
    are canonical active projects. Subdirs under projects-past/ and
    projects-future/ carry their respective status.
    """
    projects_dir = vault_root / "work" / "projects"
    if not projects_dir.is_dir():
        return []

    results = []
    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name in _SKIP_FILES:
            continue

        if name == "projects-past":
            for sub in sorted(entry.iterdir()):
                if sub.is_dir():
                    results.append({
                        "slug": _normalize_slug(sub.name),
                        "status": "past",
                        "source": "vault_dir",
                        "last_modified": _dir_mtime(sub),
                    })
        elif name == "projects-future":
            for sub in sorted(entry.iterdir()):
                if sub.is_dir():
                    results.append({
                        "slug": _normalize_slug(sub.name),
                        "status": "future",
                        "source": "vault_dir",
                        "last_modified": _dir_mtime(sub),
                    })
        elif name not in _LIFECYCLE_DIRS:
            results.append({
                "slug": _normalize_slug(name),
                "status": "active",
                "source": "vault_dir",
                "last_modified": _dir_mtime(entry),
            })

    return results


def _dir_mtime(path: Path) -> datetime | None:
    """Get the most recent mtime of any file in a directory (1 level deep)."""
    latest = None
    try:
        for f in path.iterdir():
            if f.is_file():
                mt = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if latest is None or mt > latest:
                    latest = mt
    except OSError:
        pass
    return latest


def _scan_state_files(repos_root: Path) -> dict[str, dict[str, Any]]:
    """Find STATE.md files in repos and extract compact state fingerprints.

    Returns {slug: {snapshot_date, title, status_dimensions, headline}}.
    """
    results = {}
    if not repos_root.is_dir():
        return results

    for entry in sorted(repos_root.iterdir()):
        if not entry.is_dir():
            continue
        state_file = entry / "STATE.md"
        if not state_file.is_file():
            continue

        slug = _normalize_slug(entry.name)
        try:
            content = state_file.read_text(encoding="utf-8", errors="replace")
            fingerprint = _extract_state_fingerprint(content, state_file)
            fingerprint["raw_content"] = content
            results[slug] = fingerprint
        except OSError:
            logger.debug("Failed to read STATE.md in %s", entry.name)

    return results


def _extract_state_fingerprint(content: str, path: Path) -> dict[str, Any]:
    """Extract a compact state fingerprint from STATE.md content.

    Pulls: title (H1), snapshot date, status summary table, and file mtime.
    Does NOT copy the full content.
    """
    fp: dict[str, Any] = {
        "file_mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
    }

    # Extract H1 title
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if m:
        fp["title"] = m.group(1).strip()

    # Extract snapshot date (e.g., "*Snapshot: 2026-04-04*")
    m = re.search(r"\*Snapshot:\s*(\d{4}-\d{2}-\d{2})\*", content)
    if m:
        fp["snapshot_date"] = m.group(1)

    # Extract status summary table (look for "Status Summary" section)
    status_block = _extract_status_table(content)
    if status_block:
        fp["status_dimensions"] = status_block

    # Extract deadline mentions
    deadline_match = re.search(
        r"(?:deadline|due)[:\s]*([A-Z][a-z]+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})",
        content, re.IGNORECASE,
    )
    if deadline_match:
        fp["deadline"] = deadline_match.group(1)

    return fp


def _extract_status_table(content: str) -> list[dict[str, str]] | None:
    """Extract rows from a 'Status Summary' or similar table.

    Returns [{dimension, state}, ...] or None.
    """
    # Find a section with "Status" or "Summary" in the heading
    m = re.search(
        r"^##\s+.*(?:Status|Summary).*$\n+((?:\|.+\|[\r\n]+)+)",
        content,
        re.MULTILINE | re.IGNORECASE,
    )
    if not m:
        return None

    rows = []
    for line in m.group(1).strip().splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 2:
            dim, state = cells[0].strip(), cells[1].strip()
            # Skip header separator rows
            if dim and not dim.startswith("-") and dim.lower() != "dimension":
                rows.append({"dimension": dim, "state": state})
    return rows or None


def _scan_task_projects(vault_root: Path) -> dict[str, dict[str, int]]:
    """Count tasks per #projects/<slug> tag in master task list.

    Returns {slug: {open: N, done: N}}.
    Uses direct file reading to avoid Obsidian bridge dependency.
    """
    task_file = vault_root / "tasks" / "master-task-list.md"
    if not task_file.is_file():
        return {}

    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"open": 0, "done": 0})
    try:
        content = task_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue

        # Extract project tag
        m = re.search(r"#projects/(\S+)", stripped)
        if not m:
            continue

        slug = _normalize_slug(m.group(1))
        if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
            counts[slug]["done"] += 1
        else:
            counts[slug]["open"] += 1

    return dict(counts)


def _scan_git_activity(repos_root: Path, days: int = 7) -> dict[str, dict[str, Any]]:
    """Get recent git activity per repo.

    Returns {slug: {last_commit_date, recent_commit_count, branch}}.
    """
    results = {}
    if not repos_root.is_dir():
        return results

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    for entry in sorted(repos_root.iterdir()):
        if not entry.is_dir() or not (entry / ".git").exists():
            continue

        slug = _normalize_slug(entry.name)

        # Last commit date
        try:
            raw = subprocess.run(
                ["git", "log", "-1", "--format=%aI"],
                cwd=entry, capture_output=True, text=True, timeout=10,
            )
            last_date = raw.stdout.strip() if raw.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            last_date = None

        # Recent commit count
        try:
            raw = subprocess.run(
                ["git", "rev-list", "--count", f"--since={since}", "HEAD"],
                cwd=entry, capture_output=True, text=True, timeout=10,
            )
            count = int(raw.stdout.strip()) if raw.returncode == 0 else 0
        except (subprocess.TimeoutExpired, OSError, ValueError):
            count = 0

        # Current branch
        try:
            raw = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=entry, capture_output=True, text=True, timeout=10,
            )
            branch = raw.stdout.strip() if raw.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            branch = None

        if last_date or count > 0:
            results[slug] = {
                "last_commit_date": last_date,
                "recent_commits": count,
                "branch": branch,
            }

    return results


def _scan_contracts(contracts_dir: Path) -> dict[str, dict[str, str]]:
    """Find contracts and map their title slugs.

    Returns {slug: {title, status, file}}.
    """
    results = {}
    if not contracts_dir.is_dir():
        return results

    for f in sorted(contracts_dir.iterdir()):
        if not f.is_file() or f.suffix != ".md" or f.name.startswith("_"):
            continue

        slug = _normalize_slug(f.stem)
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Extract title from frontmatter or H1
        title = f.stem
        title_m = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
        if title_m:
            title = title_m.group(1).strip().strip('"').strip("'")

        status = "active"
        status_m = re.search(r"^status:\s*(.+)$", content, re.MULTILINE)
        if status_m:
            status = status_m.group(1).strip()

        results[slug] = {"title": title, "status": status, "file": f.name}

    return results


# ── Aggregation ─────────────────────────────────────────────────

def _merge_project_signals(
    vault_projects: list[dict],
    state_files: dict[str, dict],
    task_counts: dict[str, dict[str, int]],
    git_activity: dict[str, dict],
    contracts: dict[str, dict],
    cfg: dict | None = None,
) -> list[dict[str, Any]]:
    """Merge all signals into a unified project list.

    Priority: vault dirs are canonical; other sources add evidence to
    existing entries or create "inferred" entries.

    Alias resolution (cfg["projects"]["aliases"]) is applied in steps 2-4
    so that non-vault signals (state files, task tags, git repos) with
    aliased names are merged into the canonical vault entry instead of
    creating separate "inferred" entries.
    """
    if cfg is None:
        cfg = {}
    projects: dict[str, dict[str, Any]] = {}

    # 1. Vault directories — canonical identity (no alias resolution needed;
    #    vault dir names are always the canonical slug)
    for vp in vault_projects:
        slug = vp["slug"]
        projects[slug] = {
            "slug": slug,
            "status": vp["status"],
            "sources": ["vault_dir"],
            "vault_mtime": vp["last_modified"],
            "state_file": None,
            "tasks": None,
            "git": None,
            "contract": None,
        }

    # 2. STATE.md files — resolve aliases so repo-named STATE.md files
    #    merge into the canonical vault entry
    for raw_slug, fp in state_files.items():
        slug = _resolve_alias(raw_slug, cfg)
        if slug in projects:
            projects[slug]["state_file"] = fp
            if "state_file" not in projects[slug]["sources"]:
                projects[slug]["sources"].append("state_file")
        else:
            projects[slug] = {
                "slug": slug,
                "status": "inferred",
                "sources": ["state_file"],
                "vault_mtime": None,
                "state_file": fp,
                "tasks": None,
                "git": None,
                "contract": None,
            }

    # 3. Task project tags — resolve aliases so #projects/my-project tasks
    #    merge into the my-research entry
    for raw_slug, counts in task_counts.items():
        slug = _resolve_alias(raw_slug, cfg)
        if slug in projects:
            # Accumulate counts if the canonical entry already has task data
            # (e.g., both my-project and my-research tags exist)
            existing = projects[slug].get("tasks")
            if existing:
                existing["open"] += counts["open"]
                existing["done"] += counts["done"]
            else:
                projects[slug]["tasks"] = counts
            if "tasks" not in projects[slug]["sources"]:
                projects[slug]["sources"].append("tasks")
        else:
            projects[slug] = {
                "slug": slug,
                "status": "inferred",
                "sources": ["tasks"],
                "vault_mtime": None,
                "state_file": None,
                "tasks": counts,
                "git": None,
                "contract": None,
            }

    # 4. Git activity — resolve aliases so repo-named git activity merges
    #    into the canonical vault entry; only creates inferred entries for
    #    repos with recent activity
    for raw_slug, activity in git_activity.items():
        slug = _resolve_alias(raw_slug, cfg)
        if slug in projects:
            projects[slug]["git"] = activity
            if "git" not in projects[slug]["sources"]:
                projects[slug]["sources"].append("git")
        else:
            # Only create inferred project from git if there's recent activity
            if activity.get("recent_commits", 0) > 0:
                projects[slug] = {
                    "slug": slug,
                    "status": "inferred",
                    "sources": ["git"],
                    "vault_mtime": None,
                    "state_file": None,
                    "tasks": None,
                    "git": activity,
                    "contract": None,
                }

    # 5. Contracts
    for slug, contract in contracts.items():
        if slug in projects:
            projects[slug]["contract"] = contract
            if "contract" not in projects[slug]["sources"]:
                projects[slug]["sources"].append("contract")

    return sorted(projects.values(), key=lambda p: (
        {"active": 0, "inferred": 1, "future": 2, "past": 3}.get(p["status"], 9),
        p["slug"],
    ))


# ── Markdown rendering ──────────────────────────────────────────

def _render_project(p: dict[str, Any]) -> str:
    """Render a single project entry as markdown."""
    lines = [f"### {p['slug']}"]
    lines.append(f"**Status:** {p['status']}  ")
    lines.append(f"**Evidence:** {', '.join(p['sources'])}")

    # State file fingerprint
    sf = p.get("state_file")
    if sf:
        parts = []
        if sf.get("title"):
            parts.append(f"Title: {sf['title']}")
        if sf.get("snapshot_date"):
            parts.append(f"Snapshot: {sf['snapshot_date']}")
        if sf.get("deadline"):
            parts.append(f"Deadline: {sf['deadline']}")
        if parts:
            lines.append(f"**State:** {' | '.join(parts)}")
        dims = sf.get("status_dimensions")
        if dims:
            for d in dims:
                lines.append(f"  - {d['dimension']}: {d['state']}")

    # Tasks
    tasks = p.get("tasks")
    if tasks:
        lines.append(f"**Tasks:** {tasks['open']} open, {tasks['done']} done")

    # Git
    git = p.get("git")
    if git:
        parts = []
        if git.get("recent_commits"):
            parts.append(f"{git['recent_commits']} recent commits")
        if git.get("branch"):
            parts.append(f"branch: {git['branch']}")
        if git.get("last_commit_date"):
            parts.append(f"last: {git['last_commit_date'][:10]}")
        if parts:
            lines.append(f"**Git:** {', '.join(parts)}")

    # Contract
    contract = p.get("contract")
    if contract:
        lines.append(f"**Contract:** {contract['title']} ({contract['status']})")

    return "\n".join(lines)


def _render_markdown(projects: list[dict[str, Any]]) -> str:
    """Render full projects summary as markdown."""
    if not projects:
        return "# Projects\n\nNo projects discovered.\n"

    active = [p for p in projects if p["status"] == "active"]
    inferred = [p for p in projects if p["status"] == "inferred"]
    future = [p for p in projects if p["status"] == "future"]
    past = [p for p in projects if p["status"] == "past"]

    sections = ["# Projects\n"]

    if active:
        sections.append("## Active\n")
        for p in active:
            sections.append(_render_project(p))

    if inferred:
        sections.append("\n## Inferred (no vault directory)\n")
        for p in inferred:
            sections.append(_render_project(p))

    if future:
        sections.append("\n## Future\n")
        for p in future:
            sections.append(_render_project(p))

    if past:
        sections.append("\n## Past\n")
        for p in past:
            sections.append(_render_project(p))

    return "\n\n".join(sections) + "\n"


# ── Store sync ──────────────────────────────────────────────────

def _sync_to_store(merged: list[dict[str, Any]], state_files: dict[str, dict]) -> None:
    """Persist confirmed project signals to the SQLite identity registry.

    Only syncs projects that are **confirmed** — meaning they come from a
    canonical source (vault directory) or already exist in the registry.
    Inferred-only projects (task tags, git repos) are reported in the
    markdown summary but NOT auto-created.  Use ``project_create`` or
    the ``project_discover`` capability to promote candidates.
    """
    try:
        from work_buddy.projects import store
    except Exception:
        logger.warning("Could not import project store; skipping sync.")
        return

    # Build set of already-confirmed slugs
    existing_slugs = {p["slug"] for p in store.list_projects()}

    for p in merged:
        slug = p["slug"]
        is_canonical = "vault_dir" in p.get("sources", [])
        is_known = slug in existing_slugs

        # Only sync canonical (vault dir) or already-registered projects
        if not is_canonical and not is_known:
            continue

        sf = p.get("state_file")
        name = slug.replace("-", " ").title()

        try:
            store.upsert_project(slug, name, status=p["status"])
        except Exception:
            logger.warning("Failed to upsert project %s", slug, exc_info=True)
            continue

        # Retain STATE.md to Hindsight project memory bank
        if sf:
            _retain_state_file(slug, sf)


def _retain_state_file(slug: str, sf: dict[str, Any]) -> None:
    """Retain a STATE.md snapshot to the Hindsight project memory bank.

    Uses retain_project_state_file() which sets a stable document_id
    (state-file-{slug}) for upsert/dedup on Hindsight's side — no need
    to check for prior versions locally.  Wrapped in try/except so
    Hindsight unavailability does not crash the collector.
    """
    raw_content = sf.get("raw_content", "")
    if not raw_content:
        logger.debug("No raw content for STATE.md of '%s'; skipping retain", slug)
        return

    try:
        from work_buddy.memory.ingest import retain_project_state_file
        retain_project_state_file(slug, raw_content)
    except Exception:
        logger.warning(
            "Failed to retain STATE.md for '%s' to Hindsight (non-fatal)",
            slug, exc_info=True,
        )


# ── Public API ──────────────────────────────────────────────────

def collect(cfg: dict[str, Any]) -> str:
    """Collect project identity, state, and trajectory from all signal sources.

    Scans vault directories, STATE.md files, task tags, git repos, and
    contracts.  Syncs results to the project store, then returns a markdown
    summary for bundle inclusion.
    """
    vault_root = Path(cfg["vault_root"])
    repos_root = Path(cfg.get("repos_root", vault_root / "repos"))
    git_days = cfg.get("git", {}).get("detail_days", 7)

    from work_buddy.contracts import get_contracts_dir
    contracts_dir = get_contracts_dir()

    logger.info("Scanning vault project directories...")
    vault_projects = _scan_vault_projects(vault_root)
    logger.info("Found %d vault projects.", len(vault_projects))

    logger.info("Scanning STATE.md files in repos...")
    state_files = _scan_state_files(repos_root)
    logger.info("Found %d STATE.md files.", len(state_files))

    logger.info("Scanning task project tags...")
    task_counts = _scan_task_projects(vault_root)
    logger.info("Found %d project tags in tasks.", len(task_counts))

    logger.info("Scanning git repo activity...")
    git_activity = _scan_git_activity(repos_root, days=git_days)
    logger.info("Found %d repos with activity.", len(git_activity))

    logger.info("Scanning contracts...")
    contracts = _scan_contracts(contracts_dir)
    logger.info("Found %d contracts.", len(contracts))

    merged = _merge_project_signals(
        vault_projects, state_files, task_counts, git_activity, contracts,
        cfg=cfg,
    )
    logger.info("Merged into %d projects total.", len(merged))

    # Persist to store (identity + state observations)
    _sync_to_store(merged, state_files)

    return _render_markdown(merged)
