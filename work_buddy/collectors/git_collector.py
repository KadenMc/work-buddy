"""Collect git status and recent activity across all repositories."""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _run_git(repo_path: Path, *args: str, timeout: int = 15) -> str:
    """Run a git command in a repo and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _get_last_commit_date(repo_path: Path) -> datetime | None:
    """Get the date of the most recent commit."""
    raw = _run_git(repo_path, "log", "-1", "--format=%aI")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _get_recent_commits(
    repo_path: Path,
    since_days: int,
    max_commits: int,
    since: str | None = None,
    until: str | None = None,
) -> str:
    """Get recent commit log with author timestamps.

    If ``since``/``until`` are provided, they override ``since_days``.
    """
    since_val = since or (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    args = ["log", f"--since={since_val}", f"--max-count={max_commits}",
            "--format=%aI %h %s", "--no-decorate"]
    if until:
        args.insert(2, f"--until={until}")
    return _run_git(repo_path, *args)


def _get_status(repo_path: Path) -> str:
    """Get git status in porcelain format."""
    return _run_git(repo_path, "status", "--porcelain")


def _get_diff_stat(repo_path: Path) -> str:
    """Get diff stat for unstaged changes."""
    return _run_git(repo_path, "diff", "--stat")


def _get_current_branch(repo_path: Path) -> str:
    """Get the current branch name."""
    return _run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")


def _discover_repos(repos_root: Path) -> list[Path]:
    """Find all git repos at depth 1 under repos_root."""
    repos = []
    if not repos_root.is_dir():
        return repos
    for entry in sorted(repos_root.iterdir()):
        if entry.is_dir() and (entry / ".git").exists():
            repos.append(entry)
    return repos


def _annotate_commits(raw: str, session_map: dict[str, str] | None) -> str:
    """Tag commit lines with the agent session that produced them.

    Each line has the format ``<ISO-timestamp> <short-hash> <subject>``.
    When *session_map* contains the hash, appends ``[agent: <sid>]``.

    The map values are full session UUIDs; this function truncates to
    8 chars for display — the canonical short form accepted by all
    ``session_*`` inspection capabilities.
    """
    if not session_map or not raw:
        return raw
    out = []
    for line in raw.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2:
            sid = session_map.get(parts[1][:7])
            if sid:
                line = f"{line}  [agent: {sid[:8]}]"
        out.append(line)
    return "\n".join(out)


def collect(
    cfg: dict[str, Any],
    session_map: dict[str, str] | None = None,
) -> str:
    """Collect git context and return markdown string.

    Args:
        cfg: Configuration dict. Set ``git.dirty_only=true`` to only include
            repos with uncommitted changes.
        session_map: Optional ``{short_hash: short_session_id}`` mapping from
            :func:`work_buddy.sessions.inspector.build_session_map`.  When
            provided, commit lines made by agent sessions are annotated.
    """
    repos_root = Path(cfg["repos_root"])
    git_cfg = cfg.get("git", {})
    active_days = git_cfg.get("active_days", 30)
    detail_days = git_cfg.get("detail_days", 7)
    max_commits = git_cfg.get("max_commits", 20)
    dirty_only = git_cfg.get("dirty_only", False)

    # Explicit time range overrides (from update-journal workflow)
    range_since = cfg.get("since")
    range_until = cfg.get("until")

    now = datetime.now(timezone.utc)
    repos = _discover_repos(repos_root)

    detailed = []   # last N days — full info
    recent = []     # last N days — summary only
    dormant = []    # older

    for repo_path in repos:
        name = repo_path.name
        status = _get_status(repo_path)

        # dirty_only: skip clean repos entirely
        if dirty_only and not status:
            continue

        last_commit = _get_last_commit_date(repo_path)

        if last_commit is None:
            if dirty_only and status:
                # No commits but has dirty files — show it
                detailed.append({
                    "name": name,
                    "branch": _get_current_branch(repo_path),
                    "last_commit": "never",
                    "commits": "",
                    "status": status,
                    "diff_stat": _get_diff_stat(repo_path),
                })
            elif not dirty_only:
                dormant.append(name)
            continue

        age = now - last_commit.astimezone(timezone.utc)

        if dirty_only:
            # In dirty_only mode, all dirty repos get full detail regardless of age
            detailed.append({
                "name": name,
                "branch": _get_current_branch(repo_path),
                "last_commit": last_commit.strftime("%Y-%m-%d %H:%M"),
                "commits": _annotate_commits(_get_recent_commits(repo_path, detail_days, max_commits, since=range_since, until=range_until), session_map),
                "status": status,
                "diff_stat": _get_diff_stat(repo_path),
            })
        elif age <= timedelta(days=detail_days):
            branch = _get_current_branch(repo_path)
            commits = _annotate_commits(_get_recent_commits(repo_path, detail_days, max_commits, since=range_since, until=range_until), session_map)
            diff_stat = _get_diff_stat(repo_path)
            detailed.append({
                "name": name,
                "branch": branch,
                "last_commit": last_commit.strftime("%Y-%m-%d %H:%M"),
                "commits": commits,
                "status": status,
                "diff_stat": diff_stat,
            })
        elif age <= timedelta(days=active_days):
            last_msg = _run_git(repo_path, "log", "-1", "--format=%s")
            recent.append({
                "name": name,
                "last_commit": last_commit.strftime("%Y-%m-%d"),
                "last_message": last_msg,
            })
        else:
            dormant.append(name)

    # Build markdown
    mode_note = " (dirty repos only)*" if dirty_only else "*"
    lines = [
        "# Git Summary",
        "",
        f"*Collected: {now.strftime('%Y-%m-%d %H:%M UTC')}{mode_note}",
        f"*Scanned {len(repos)} repositories under `{repos_root}`*",
        "",
    ]

    if detailed:
        if dirty_only:
            lines.append("## Dirty Repos")
        elif detail_days < 1:
            hours = detail_days * 24
            lines.append(f"## Active (last {hours:.0f} hours)")
        else:
            lines.append(f"## Active (last {detail_days:.0f} days)")
        lines.append("")
        for repo in detailed:
            lines.append(f"### {repo['name']}")
            lines.append(f"- **Branch:** `{repo['branch']}`")
            lines.append(f"- **Last commit:** {repo['last_commit']}")
            if repo["status"]:
                dirty_files = repo["status"].split("\n")
                lines.append(f"- **Dirty files:** {len(dirty_files)}")
                lines.append("")
                lines.append("```")
                lines.append(repo["status"])
                lines.append("```")
            else:
                lines.append("- **Working tree:** clean")
            if repo["commits"]:
                lines.append("")
                lines.append("**Recent commits:**")
                lines.append("```")
                lines.append(repo["commits"])
                lines.append("```")
            if repo["diff_stat"]:
                lines.append("")
                lines.append("**Unstaged changes:**")
                lines.append("```")
                lines.append(repo["diff_stat"])
                lines.append("```")
            lines.append("")

    if recent:
        lines.append(f"## Recent (last {active_days} days)")
        lines.append("")
        for repo in recent:
            lines.append(f"- **{repo['name']}** — last commit {repo['last_commit']}: {repo['last_message']}")
        lines.append("")

    if dormant:
        lines.append("## Dormant")
        lines.append("")
        lines.append(", ".join(dormant))
        lines.append("")

    return "\n".join(lines)
