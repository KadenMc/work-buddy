"""``git`` context source — recent commits across every project repo.

Multi-repo by default: walks every repo at depth 1 under ``repos_root``
(from ``load_config()``), falling back to the single work-buddy repo
(``repo_root()``) when ``repos_root`` isn't configured. Pass
``custom["repo_path"]`` to force single-repo mode for a specific caller.

The legacy single-repo behavior (``collectors/git_collector.py`` pre-
unification) shipped ``dirty_only``, working-tree status, diff-stat,
and per-commit session annotation. Those features are retained here
via ``custom`` parameters so existing callers keep working:

    - ``custom["dirty_only"]``: list only repos with uncommitted changes
    - ``custom["include_status"]``: include porcelain status + diff-stat
    - ``custom["repo_path"]``: single-repo mode (the legacy ``GitSource``
      default)
    - ``custom["session_map"]``: ``{short_hash: short_session_id}`` for
      annotation — opt-in from ``sessions.inspector.build_session_map``

Depth semantics:
  - BRIEF:  5 commits per repo
  - NORMAL: 20 commits per repo
  - DEEP:   50 commits per repo + author + short stat

``target_date`` support: window is ``[target_date - window_days,
target_date]``. Default ``window_days=1`` → last 24 hours.

``is_stale`` checks the HEAD of every scanned repo — if any moved,
refetch.
"""

from __future__ import annotations

import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from work_buddy.context.types import (
    BaseContextSource,
    ContextDepth,
    ContextRequest,
    ContextSection,
)
from work_buddy.context import registry as _registry
from work_buddy.logging_config import get_logger
from work_buddy.paths import repo_root

logger = get_logger(__name__)


_GIT_TIMEOUT = 5


class GitSource(BaseContextSource):
    """Recent commits across all project repos (or one, if explicitly scoped)."""

    name = "git"

    def collect(self, request: ContextRequest) -> ContextSection:
        custom = request.custom_for(self.name)
        max_commits = int(custom.get("max_commits", 50))
        dirty_only = bool(custom.get("dirty_only", False))
        include_status = bool(custom.get("include_status", False)) or dirty_only
        session_map = custom.get("session_map") or {}

        since, until = _window_for(request, window_days=request.window_days)

        repos = _resolve_repos(custom.get("repo_path"))

        items: list[dict[str, Any]] = []
        repo_metadata: list[dict[str, Any]] = []

        for repo in repos:
            status = _porcelain_status(repo) if include_status else ""
            if dirty_only and not status:
                # Skip clean repos entirely in dirty_only mode.
                continue

            commits = _log_commits(repo, max_commits, since, until)
            # Annotate with project slug (best-effort from repo dir name).
            project = repo.name
            for c in commits:
                c["project"] = project
                sid = session_map.get(c.get("short", "")[:7])
                if sid:
                    c["agent_session"] = sid[:8]
            items.extend(commits)

            meta_entry: dict[str, Any] = {
                "project": project,
                "repo": str(repo),
                "head": _head_sha(repo),
                "dirty_files": len(status.splitlines()) if status else 0,
                "status": status if include_status else "",
                "commit_count": len(commits),
            }
            # Branch + diff-stat are only useful when status is requested;
            # skip the extra subprocess calls on the hot path.
            if include_status:
                meta_entry["branch"] = _current_branch(repo)
                meta_entry["diff_stat"] = _diff_stat(repo)
            repo_metadata.append(meta_entry)

        # Stable ordering: by date desc so the newest activity surfaces first
        # regardless of repo enumeration order.
        items.sort(key=lambda c: c.get("date", ""), reverse=True)

        return ContextSection(
            source=self.name,
            items=items,
            metadata={
                "repos": repo_metadata,
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
                "total_count": len(items),
                "total_repos": len(repo_metadata),
                "dirty_only": dirty_only,
            },
        )

    def render(self, section: ContextSection, depth: ContextDepth) -> str:
        items = section.items or []
        metadata = section.metadata or {}
        repos_meta: list[dict[str, Any]] = metadata.get("repos") or []
        if not items and not (metadata.get("dirty_only") and repos_meta):
            return ""

        cap = _cap_for_depth(depth)

        # Bucket by project so each repo gets its own subsection.
        by_project: dict[str, list[dict[str, Any]]] = {}
        for c in items:
            by_project.setdefault(c.get("project") or "unknown", []).append(c)

        # Repo ordering: projects that had commits first (by recency), then
        # empty/dirty-only repos.
        ordered: list[str] = []
        for meta in sorted(
            repos_meta,
            key=lambda m: (-(m.get("commit_count") or 0), m.get("project") or ""),
        ):
            project = meta.get("project") or ""
            if project and project not in ordered:
                ordered.append(project)
        # Append any projects surfaced via items but missing metadata.
        for project in by_project:
            if project not in ordered:
                ordered.append(project)

        lines: list[str] = [
            f"### Recent Commits ({metadata.get('total_count') or len(items)} across "
            f"{len(ordered) or 1} repos)"
        ]
        for project in ordered:
            repo_commits = by_project.get(project, [])
            meta = next(
                (m for m in repos_meta if m.get("project") == project),
                {},
            )
            header_bits: list[str] = [f"#### {project}"]
            branch = meta.get("branch") or ""
            if branch:
                header_bits.append(f"`{branch}`")
            if meta.get("dirty_files"):
                header_bits.append(f"*{meta['dirty_files']} dirty file(s)*")
            lines.append(" — ".join(header_bits))

            if not repo_commits:
                if meta.get("dirty_files"):
                    lines.append("- (no commits in window; working tree is dirty)")
                else:
                    lines.append("- (no commits in window)")
                continue

            shown = repo_commits[:cap]
            for c in shown:
                short = c.get("short", "")
                subject = c.get("subject", "")
                suffix_parts: list[str] = []
                if depth >= ContextDepth.DEEP and c.get("author"):
                    suffix_parts.append(f"[{c['author']}]")
                if c.get("agent_session"):
                    suffix_parts.append(f"[agent: {c['agent_session']}]")
                suffix = (" " + " ".join(suffix_parts)) if suffix_parts else ""
                lines.append(f"- {short} — {subject}{suffix}")
            if len(repo_commits) > cap:
                lines.append(f"- … ({len(repo_commits) - cap} more in {project})")

            if depth >= ContextDepth.DEEP and meta.get("diff_stat"):
                lines.append("")
                lines.append("```")
                lines.append(meta["diff_stat"].strip())
                lines.append("```")

        return "\n".join(lines)

    def is_stale(
        self,
        cached: ContextSection,
        request: ContextRequest,
    ) -> bool:
        """Refetch when any scanned repo's HEAD moved since cache was written."""
        cached_meta = cached.metadata or {}
        cached_repos = cached_meta.get("repos") or []
        # Legacy cache shape (pre-multi-repo): single {"head": ...} at top level.
        if not cached_repos and "head" in cached_meta:
            custom = request.custom_for(self.name)
            repo = Path(custom.get("repo_path") or repo_root())
            return _head_sha(repo) != (cached_meta.get("head") or "")

        for repo_entry in cached_repos:
            path = repo_entry.get("repo")
            cached_head = repo_entry.get("head") or ""
            if not path:
                continue
            current = _head_sha(Path(path))
            if current and current != cached_head:
                return True
        return False

    def drill_down(self, item_id: str, field: str) -> dict[str, Any]:
        """``field='full_message'`` or ``'diff_stats'`` for a commit by SHA.

        With multi-repo scanning, a given short SHA could theoretically
        exist in multiple repos. We probe every configured repo and
        return the first match — in practice SHA collisions across
        repos are vanishingly rare; short-SHA collisions within a repo
        get resolved by git itself.
        """
        for repo in _resolve_repos(None):
            if field == "full_message":
                out = _git_show(repo, ["-s", "--pretty=fuller", item_id])
            elif field == "diff_stats":
                out = _git_show(repo, ["--stat", "--format=", item_id])
            else:
                raise KeyError(
                    f"GitSource.drill_down: unknown field {field!r}. "
                    "Valid: 'full_message', 'diff_stats'."
                )
            if out is None:
                continue  # not found in this repo; try next
            if field == "full_message":
                return {"sha": item_id, "message": out, "repo": repo.name}
            return {"sha": item_id, "stat": out.strip(), "repo": repo.name}

        raise KeyError(f"No commit {item_id!r} in any scanned repo")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_repos(override: str | None) -> list[Path]:
    """Return the list of repos to scan.

    Priority:
      1. Explicit ``override`` (single-repo mode).
      2. ``cfg['repos_root']`` — walk every dir at depth 1 that has ``.git``.
      3. Fall back to ``repo_root()`` (the work-buddy repo itself).
    """
    if override:
        return [Path(override)]

    try:
        from work_buddy.config import load_config
        cfg = load_config()
    except Exception as exc:
        logger.debug("git source: load_config failed: %s", exc)
        return [repo_root()]

    repos_root_str = cfg.get("repos_root")
    if not repos_root_str:
        return [repo_root()]

    repos_root_path = Path(repos_root_str)
    if not repos_root_path.is_dir():
        return [repo_root()]

    discovered: list[Path] = []
    for entry in sorted(repos_root_path.iterdir()):
        if entry.is_dir() and (entry / ".git").exists():
            discovered.append(entry)

    if not discovered:
        return [repo_root()]
    return discovered


def _log_commits(
    repo: Path,
    max_commits: int,
    since: date | None,
    until: date | None,
) -> list[dict[str, Any]]:
    args = [
        "git", "log",
        f"--max-count={max_commits}",
        "--pretty=format:%H\x1f%h\x1f%ad\x1f%an\x1f%s",
        "--date=iso-strict",
    ]
    if since:
        args.append(f"--since={since.isoformat()}")
    if until:
        args.append(f"--until={until.isoformat()}")
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=str(repo),
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git source: git log failed in %s: %s", repo, exc)
        return []
    if result.returncode != 0:
        logger.debug(
            "git source: non-zero returncode in %s (%d): %s",
            repo, result.returncode, result.stderr[:200],
        )
        return []

    items: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 4)
        if len(parts) != 5:
            continue
        sha, short, date_, author, subject = parts
        items.append({
            "sha": sha,
            "short": short,
            "date": date_,
            "author": author,
            "subject": subject,
        })
    return items


def _head_sha(repo: Path) -> str:
    return _git_capture(repo, ["rev-parse", "HEAD"])


def _current_branch(repo: Path) -> str:
    return _git_capture(repo, ["rev-parse", "--abbrev-ref", "HEAD"])


def _porcelain_status(repo: Path) -> str:
    return _git_capture(repo, ["status", "--porcelain"])


def _diff_stat(repo: Path) -> str:
    return _git_capture(repo, ["diff", "--stat"])


def _git_capture(repo: Path, args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, cwd=str(repo),
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def _git_show(repo: Path, args: list[str]) -> str | None:
    try:
        out = subprocess.run(
            ["git", "show", *args],
            capture_output=True, text=True, cwd=str(repo),
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git source: git show failed in %s: %s", repo, exc)
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _window_for(
    request: ContextRequest,
    *,
    window_days: int,
) -> tuple[date | None, date | None]:
    """Return the (since, until) window around the request's target_date.

    For the default path (target_date=None, window_days=1) this
    computes "the last 24 hours" as a since-only bound, matching the
    pre-refactor behavior. When a target_date is supplied, we
    center the window on it.
    """
    if request.target_date is None:
        since = date.today() - timedelta(days=max(window_days, 0))
        return since, None
    center = request.target_date
    if window_days <= 0:
        return center, center
    return center - timedelta(days=window_days), center + timedelta(days=window_days)


def _cap_for_depth(depth: ContextDepth) -> int:
    if depth == ContextDepth.BRIEF:
        return 5
    if depth == ContextDepth.DEEP:
        return 50
    return 20


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


_registry.register(GitSource())
