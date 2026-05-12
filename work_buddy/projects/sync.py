"""Sync the project identity registry from multi-source signals.

Despite its historical name (``collectors.project_collector``), this is
a synthesis/sync job, not a context collector: it scans vault project
directories, STATE.md files in repos, task project tags, git activity,
chat session slugs, and contracts — then writes the merged view into
the projects SQLite store via :mod:`work_buddy.projects.store`.

Signal sources:

1. Vault project directories (``work/projects/*``)
2. STATE.md files in repos (``repos_root/*/STATE.md``)
3. Task project tags (``#projects/<slug>``)
4. Git repo activity
5. Chat session project names
6. Contracts (``contracts_dir/*.md``)

Entry point: :func:`sync_projects` (formerly ``collect``). Returns the
rendered project markdown for bundle output; ``_sync_to_store`` is
called as a side-effect so the SQLite registry reflects what the scan
saw.

The historical ``collect`` name is still exported as an alias for
back-compat; use ``sync_projects`` in new code.
"""

import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Lifecycle subdirectories under work/projects/. Scanning descends into them
# to discover the canonical project folder for past + future projects, but
# the location no longer drives status — status is store-owned.
_LIFECYCLE_DIRS = {"projects-past", "projects-future"}

# Files to skip when scanning vault project directories
_SKIP_FILES = {"projects.md"}  # Waypoint auto-generated folder note (often corrupted)

# Statuses rendered in the bundle by default. Other lifecycle states still
# get scanned and synced — they're just hidden from the rendered markdown
# unless an explicit ``statuses`` override is passed. ``deleted`` is never
# rendered through this surface; callers wanting the deleted lane must
# query the store directly with ``include_deleted=True``.
_DEFAULT_RENDER_STATUSES: tuple[str, ...] = ("active",)


# ── Slug normalization ──────────────────────────────────────────

def _normalize_slug(name: str) -> str:
    """Normalize a project name to a canonical slug: lowercase, hyphens."""
    return name.lower().replace("_", "-").replace(" ", "-")


def _resolve_alias(slug: str, cfg: dict | None = None) -> str:
    """Resolve a slug to its canonical form via the SQLite alias table.

    If the input matches an alias (e.g. ``electricrag``) on a registered
    project (e.g. ``ecg-inquiry``), returns the canonical slug. Falls
    back to the input unchanged when no alias matches. The ``cfg``
    parameter is accepted for back-compat with the old signature but
    is unused — aliases moved out of config into ``project_aliases``.
    """
    try:
        from work_buddy.projects import store
        pid = store.resolve_slug(slug)
        if pid is None:
            return slug
        row = store.get_project_by_id(pid, include_deleted=True)
        if row is None:
            return slug
        return row["slug"]
    except Exception:
        # Defensive: if the store can't be opened for any reason, fall
        # back to identity. Sync should never crash on alias lookup.
        return slug


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


def _read_git_repo_activity(
    repo_path: Path,
    *,
    detail_days: int = 7,
    score_window_days: int = 60,
) -> dict[str, Any] | None:
    """Read activity from a single directory if it contains ``.git``.

    Returns a dict with:

    - ``last_commit_date`` — ISO date of the most recent commit.
    - ``recent_commits`` — count of commits in the ``detail_days`` window.
      Drives the bundle's "N recent commits" display.
    - ``commit_dates`` — list of ISO commit timestamps in the
      ``score_window_days`` window. Drives the activity score's
      per-commit decay computation.
    - ``branch`` — current branch name.
    - ``repo_path`` — absolute path of the scanned repo (used by the
      folder-driven attribution path).

    Returns ``None`` if the path is not a git repo or has no usable
    state (e.g. an empty repo with no commits and no branch).
    """
    if not (repo_path / ".git").exists():
        return None

    # One ``git log`` call returns every commit timestamp in the
    # broader score window. Both ``recent_commits`` (narrow window)
    # and the score's per-commit decay are derived from it — avoids
    # making two redundant scans per repo.
    since = (
        datetime.now(timezone.utc) - timedelta(days=score_window_days)
    ).strftime("%Y-%m-%d")

    try:
        raw = subprocess.run(
            ["git", "log", f"--since={since}", "--format=%aI"],
            cwd=repo_path, capture_output=True, text=True, timeout=15,
        )
        commit_dates = (
            [line.strip() for line in raw.stdout.splitlines() if line.strip()]
            if raw.returncode == 0 else []
        )
    except (subprocess.TimeoutExpired, OSError):
        commit_dates = []

    last_commit_date = commit_dates[0] if commit_dates else None

    cutoff_detail = datetime.now(timezone.utc) - timedelta(days=detail_days)
    recent_commits = 0
    for d in commit_dates:
        try:
            t = datetime.fromisoformat(d)
            if t >= cutoff_detail:
                recent_commits += 1
        except ValueError:
            continue

    try:
        raw = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        branch = raw.stdout.strip() if raw.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        branch = None

    if not commit_dates and not branch:
        return None

    return {
        "last_commit_date": last_commit_date,
        "recent_commits": recent_commits,
        "commit_dates": commit_dates,
        "branch": branch,
        "repo_path": str(repo_path),
    }


def _scan_git_activity(repos_root: Path, days: int = 7) -> dict[str, dict[str, Any]]:
    """Scan ``repos_root`` for git repos. Returns ``{slug: activity}``.

    This is the **candidate-discovery** path — it finds unregistered
    repos under the configured ``repos_root`` so they can surface as
    candidates in the bundle render. Confirmed projects get git
    activity attached via the **folder-driven** path (see
    ``_merge_project_signals``), not via this slug-keyed map.

    Each activity dict includes a ``repo_path`` field that the merge
    step uses to de-duplicate against folders already attached to
    registered projects.
    """
    results: dict[str, dict[str, Any]] = {}
    if not repos_root.is_dir():
        return results

    for entry in sorted(repos_root.iterdir()):
        if not entry.is_dir():
            continue
        activity = _read_git_repo_activity(entry, detail_days=days)
        if activity:
            slug = _normalize_slug(entry.name)
            results[slug] = activity
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

    Two categories of records emerge:

    - **Confirmed projects** — slug exists in the SQLite store (either
      as canonical slug or as an alias). Status + origin come from the
      store, never from the scan. The scan attaches trajectory data
      (state file, task counts, git activity, contracts) but does NOT
      mutate identity.
    - **Candidates** — slug NOT in the store, but signals exist.
      ``is_candidate=True``. Rendered in their own bundle section;
      not auto-promoted (the user calls ``project_create`` to register).

    Any non-vault signal slug is run through the store's alias table,
    so a STATE.md or git repo at ``electricrag`` gets attributed to the
    canonical row ``ecg-inquiry``.
    """
    from work_buddy.projects import store

    # Snapshot the store: canonical_slug -> row.
    try:
        registered = {
            p["slug"]: p
            for p in store.list_projects(include_deleted=False)
        }
    except Exception:
        logger.warning(
            "Could not read project store; treating all signals as candidates."
        )
        registered = {}

    def canonical_for(raw_slug: str) -> str:
        """Resolve raw_slug via store alias lookup; identity fallback."""
        try:
            pid = store.resolve_slug(raw_slug)
            if pid is None:
                return raw_slug
            row = store.get_project_by_id(pid, include_deleted=True)
            return row["slug"] if row else raw_slug
        except Exception:
            return raw_slug

    projects: dict[str, dict[str, Any]] = {}

    def ensure(slug: str, *, is_candidate: bool, source: str) -> dict[str, Any]:
        if slug not in projects:
            store_row = registered.get(slug)
            projects[slug] = {
                "slug": slug,
                "status": store_row["status"] if store_row else None,
                "origin": store_row["origin"] if store_row else None,
                "is_candidate": is_candidate and store_row is None,
                "sources": [source] if source else [],
                "vault_mtime": None,
                "state_file": None,
                "tasks": None,
                "git": None,
                "contract": None,
            }
        else:
            entry = projects[slug]
            if source and source not in entry["sources"]:
                entry["sources"].append(source)
        return projects[slug]

    # 1. Vault directory scan. Folder names start as slugs, but we
    #    alias-resolve so a folder whose name matches an alias merges
    #    into the canonical project (e.g. ``work/projects/ecg-cred/``
    #    folds into ``ecg-inquiry`` via the ``ECG-CRED`` alias).
    for vp in vault_projects:
        slug = canonical_for(vp["slug"])
        entry = ensure(slug, is_candidate=False, source="vault_dir")
        entry["vault_mtime"] = vp["last_modified"]
        entry["is_candidate"] = False

    # 2. STATE.md files.
    for raw_slug, fp in state_files.items():
        slug = canonical_for(raw_slug)
        entry = ensure(slug, is_candidate=True, source="state_file")
        entry["state_file"] = fp

    # 3. Task project tags.
    for raw_slug, counts in task_counts.items():
        slug = canonical_for(raw_slug)
        entry = ensure(slug, is_candidate=True, source="tasks")
        existing = entry.get("tasks")
        if existing:
            existing["open"] += counts["open"]
            existing["done"] += counts["done"]
        else:
            entry["tasks"] = dict(counts)

    # 4. Git activity — folder-driven attribution.
    #
    #    Step 4a: for each registered (non-candidate) project, walk its
    #    folders and attach activity for the first non-archived git
    #    repo found. This correctly attributes repos whose folder name
    #    differs from the project slug (e.g. ``ecg-fm`` ↔
    #    ``repos/foundational-ecg/``) — slug-name matching alone
    #    would miss those.
    #
    #    Step 4b: surface any git repo under ``repos_root`` that isn't
    #    already attached to a registered project as a CANDIDATE
    #    (the slug-keyed ``git_activity`` map provides this).
    git_by_path: dict[str, dict[str, Any]] = {
        a["repo_path"]: a for a in git_activity.values()
        if a.get("repo_path")
    }
    claimed_repo_paths: set[str] = set()
    git_detail_days = (cfg or {}).get("git", {}).get("detail_days", 7)

    for slug, entry in projects.items():
        if entry["is_candidate"]:
            continue
        store_row = registered.get(slug)
        if not store_row:
            continue
        for f in store_row.get("folders", []):
            if f.get("archived"):
                continue
            try:
                folder_path = Path(f["path"]).resolve()
            except OSError:
                continue
            activity = git_by_path.get(str(folder_path))
            if activity is None:
                # Folder lives outside repos_root — scan it directly.
                try:
                    activity = _read_git_repo_activity(
                        folder_path, detail_days=git_detail_days,
                    )
                except OSError:
                    activity = None
            if activity:
                entry["git"] = activity
                if "git" not in entry["sources"]:
                    entry["sources"].append("git")
                claimed_repo_paths.add(activity.get("repo_path") or str(folder_path))
                break  # first git folder per project wins

    # 4b. Candidate surfacing for unclaimed repos under repos_root.
    for raw_slug, activity in git_activity.items():
        if activity.get("repo_path") in claimed_repo_paths:
            continue
        slug = canonical_for(raw_slug)
        if slug in projects:
            # Resolved via alias to a confirmed project that has no
            # git folder yet (e.g. the slug-matched repo isn't on the
            # project's folders list). Attach as a fallback.
            entry = projects[slug]
            if "git" not in entry:
                entry["git"] = activity
                if "git" not in entry["sources"]:
                    entry["sources"].append("git")
        elif activity.get("recent_commits", 0) > 0:
            # Brand-new repo, no project owns it; surface as candidate.
            entry = ensure(slug, is_candidate=True, source="git")
            entry["git"] = activity

    # 5. Contracts — attach to existing entries only (contracts don't
    #    seed candidates).
    for slug, contract in contracts.items():
        if slug in projects:
            entry = projects[slug]
            entry["contract"] = contract
            if "contract" not in entry["sources"]:
                entry["sources"].append("contract")

    # Sort: confirmed projects first by status, then candidates by slug.
    status_rank = {"active": 0, "paused": 1, "future": 2, "past": 3}

    def sort_key(p: dict[str, Any]) -> tuple[int, int, str]:
        candidate_rank = 1 if p["is_candidate"] else 0
        s_rank = status_rank.get(p.get("status") or "", 9)
        return (candidate_rank, s_rank, p["slug"])

    return sorted(projects.values(), key=sort_key)


# ── Markdown rendering ──────────────────────────────────────────

def _render_project(p: dict[str, Any]) -> str:
    """Render a single project entry as markdown."""
    lines = [f"### {p['slug']}"]
    if p.get("is_candidate"):
        lines.append("**Status:** candidate (not yet registered)  ")
    elif p.get("status"):
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


def _render_markdown(
    projects: list[dict[str, Any]],
    *,
    statuses: list[str] | None = None,
) -> str:
    """Render projects summary as markdown.

    ``statuses`` controls which lifecycle states appear in the
    Confirmed section. Default: ``_DEFAULT_RENDER_STATUSES`` (active).
    Pass an explicit list (e.g. ``["active", "paused", "past"]``) to
    widen. ``deleted`` is never rendered through this surface.

    Section order and heading labels derive from
    ``store.STATUS_DISPLAY_ORDER`` so the dashboard, the render, and
    the SQL ordering all stay in lockstep.

    The Candidates section (slugs surfaced by signal-scan but not yet
    registered) always renders — it isn't a status filter target.
    """
    from work_buddy.projects.store import STATUS_DISPLAY_ORDER

    if not projects:
        return "# Projects\n\nNo projects discovered.\n"

    allowed = set(statuses) if statuses is not None else set(_DEFAULT_RENDER_STATUSES)
    # Filter ``deleted`` explicitly even if the caller asked for it.
    allowed.discard("deleted")

    confirmed = [p for p in projects if not p.get("is_candidate")]
    candidates = [p for p in projects if p.get("is_candidate")]

    # Group confirmed projects by status, preserving STATUS_DISPLAY_ORDER.
    by_status: dict[str, list[dict[str, Any]]] = {s: [] for s in STATUS_DISPLAY_ORDER}
    for p in confirmed:
        s = p.get("status")
        if s in allowed and s in by_status:
            by_status[s].append(p)

    sections = ["# Projects\n"]
    first = True
    for status in STATUS_DISPLAY_ORDER:
        items = by_status[status]
        if not items:
            continue
        heading = f"## {status.title()}\n"
        sections.append(("\n" if not first else "") + heading)
        for p in items:
            sections.append(_render_project(p))
        first = False

    if candidates:
        sections.append("\n## Candidates (not yet registered)\n")
        for p in candidates:
            sections.append(_render_project(p))

    return "\n\n".join(sections) + "\n"


# ── Store sync ──────────────────────────────────────────────────

def _sync_to_store(merged: list[dict[str, Any]], state_files: dict[str, dict]) -> None:
    """Persist confirmed project signals to the SQLite identity registry.

    Behavior under the temporal model:

    - **Candidates** (no store row) are NEVER auto-created here. They
      surface in the rendered markdown for the user / agent to register
      explicitly via ``project_create``.
    - **Vault-canonical projects without a store row** ARE auto-created
      with ``origin='vault'``. They count as confirmed-canonical signals.
    - **Existing rows** never have their ``status`` overwritten by sync.
      If a vault directory appears for a previously-manual row, ``origin``
      is upgraded ``manual`` → ``vault`` (and a revision is written for
      that real state change). Otherwise the row is only touched (its
      ``updated_at`` bumped), with no revision written — the sync is
      observation, not mutation.

    STATE.md retention to Hindsight continues for any project with a
    state file, regardless of provenance.
    """
    try:
        from work_buddy.projects import store
    except Exception:
        logger.warning("Could not import project store; skipping sync.")
        return

    try:
        existing_rows = {
            p["slug"]: p for p in store.list_projects(include_deleted=False)
        }
    except Exception:
        logger.warning("Could not read project store; skipping sync.", exc_info=True)
        return

    for p in merged:
        slug = p["slug"]
        is_canonical = "vault_dir" in p.get("sources", [])
        existing = existing_rows.get(slug)
        sf = p.get("state_file")

        if existing is None:
            if not is_canonical:
                # Candidate — skip auto-create. Will appear in render.
                continue
            # Vault dir on a slug not in the active store. Could still
            # collide with a soft-deleted row of the same slug (rare
            # but real after a merge). Check before upserting.
            soft_deleted = None
            try:
                pid_any = store.resolve_slug(slug)
                if pid_any is not None:
                    row_any = store.get_project_by_id(
                        pid_any, include_deleted=True,
                    )
                    if row_any and row_any.get("status") == "deleted":
                        soft_deleted = row_any
            except Exception:
                soft_deleted = None
            if soft_deleted is not None:
                logger.info(
                    "Skipping auto-create for %r: a soft-deleted row "
                    "exists. To revive, project_update(status='active').",
                    slug,
                )
                continue
            # New vault-canonical project: auto-create with origin='vault'.
            display_name = slug.replace("-", " ").title()
            try:
                store.upsert_project(
                    slug, display_name,
                    status="active",
                    origin="vault",
                    author="agent",
                    change_summary="auto-created from vault directory scan",
                )
            except Exception:
                logger.warning(
                    "Failed to auto-create project %s", slug, exc_info=True,
                )
                continue
        else:
            # Known project. Only mutate if origin needs upgrading.
            if is_canonical and existing["origin"] != "vault":
                try:
                    store.upsert_project(
                        slug,
                        # Don't overwrite name — pass None preserves existing.
                        None,
                        status=existing["status"],
                        description=existing["description"],
                        origin="vault",
                        author="agent",
                        change_summary=(
                            "promoted origin to vault (vault directory detected)"
                        ),
                    )
                except Exception:
                    logger.warning(
                        "Failed to upgrade origin for %s", slug, exc_info=True,
                    )
            else:
                # Pure observation. Just bump updated_at; no revision.
                store.touch_project(slug)

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

def sync_projects(
    cfg: dict[str, Any],
    *,
    statuses: list[str] | None = None,
) -> str:
    """Sync the project registry from every signal source + return summary markdown.

    Scans vault directories, STATE.md files, task tags, git repos, and
    contracts. Writes merged identity into the SQLite project store,
    retains STATE.md text into Hindsight project-memory, then returns a
    markdown summary for bundle inclusion.

    ``statuses`` filters only the rendered markdown — every project still
    flows through the scan + store sync regardless. Default (``None``)
    renders ``_DEFAULT_RENDER_STATUSES`` (active + inferred); pass an
    explicit list to widen.

    Historical alias: ``collect`` (still exported for back-compat).
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

    return _render_markdown(merged, statuses=statuses)


# Back-compat alias — old code paths still call ``project_collector.collect``.
collect = sync_projects
