"""``git`` context source — recent commits from the work-buddy repo.

Wraps the ``git log --oneline`` call that lived inline in
:func:`work_buddy.triage.recommend.build_triage_context`. Only one
repo is consulted (the work-buddy repo — the one running this
process). When :data:`t-3d733f68` (``repo_paths`` on projects) lands,
this source extends to walk every project's repo(s) and attribute
commits per project; until then we emit a flat list.

Depth semantics:
  - BRIEF:  5 commits.
  - NORMAL: 20 commits (matches build_triage_context's default cap).
  - DEEP:   50 commits + author + short stat if available.

``target_date`` support: window is ``[target_date - window_days,
target_date]``. Default ``window_days=1`` → last 24 hours.

``is_stale`` checks ``git rev-parse HEAD`` — if the head moved since
the cache was written, we refetch even within the freshness window.
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
    """Recent commits source. Registered at module import."""

    name = "git"

    def collect(self, request: ContextRequest) -> ContextSection:
        custom = request.custom_for(self.name)
        max_commits = int(custom.get("max_commits", 50))
        repo = _resolve_repo(custom.get("repo_path"))

        since, until = _window_for(request, window_days=request.window_days)

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
            logger.debug("git source: git log failed: %s", exc)
            return ContextSection(source=self.name, items=[], metadata={"repo": str(repo)})

        if result.returncode != 0:
            logger.debug("git source: non-zero returncode (%d): %s",
                         result.returncode, result.stderr[:200])
            return ContextSection(source=self.name, items=[], metadata={"repo": str(repo)})

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

        head_sha = _head_sha(repo)
        return ContextSection(
            source=self.name,
            items=items,
            metadata={
                "repo": str(repo),
                "head": head_sha,
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
                "total_count": len(items),
            },
        )

    def render(self, section: ContextSection, depth: ContextDepth) -> str:
        items = section.items or []
        if not items:
            return ""

        cap = _cap_for_depth(depth)
        shown = items[:cap]

        lines = [f"### Recent Commits ({len(items)})"]
        for c in shown:
            short = c.get("short", "")
            subject = c.get("subject", "")
            if depth >= ContextDepth.DEEP:
                author = c.get("author", "")
                line = f"- {short} — {subject}" + (f"  [{author}]" if author else "")
            else:
                line = f"- {short} — {subject}"
            lines.append(line)
        if len(items) > cap:
            lines.append(f"- … ({len(items) - cap} more)")
        return "\n".join(lines)

    def is_stale(
        self,
        cached: ContextSection,
        request: ContextRequest,
    ) -> bool:
        """Refetch when HEAD moved since the cache was written."""
        cached_head = (cached.metadata or {}).get("head") or ""
        custom = request.custom_for(self.name)
        repo = _resolve_repo(custom.get("repo_path"))
        current_head = _head_sha(repo)
        if not current_head:
            return False
        return current_head != cached_head

    def drill_down(self, item_id: str, field: str) -> dict[str, Any]:
        """``field='full_message'`` returns the commit's full message body.

        ``field='diff_stats'`` returns the shortlog stat. ``item_id``
        is the commit SHA (short or full; git resolves both).
        """
        repo = repo_root()
        if field == "full_message":
            try:
                out = subprocess.run(
                    ["git", "show", "-s", "--pretty=fuller", item_id],
                    capture_output=True, text=True, cwd=str(repo),
                    timeout=_GIT_TIMEOUT,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise RuntimeError(f"git show failed: {exc}") from exc
            if out.returncode != 0:
                raise KeyError(f"No commit {item_id!r}: {out.stderr[:200]}")
            return {"sha": item_id, "message": out.stdout}

        if field == "diff_stats":
            try:
                out = subprocess.run(
                    ["git", "show", "--stat", "--format=", item_id],
                    capture_output=True, text=True, cwd=str(repo),
                    timeout=_GIT_TIMEOUT,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise RuntimeError(f"git show --stat failed: {exc}") from exc
            if out.returncode != 0:
                raise KeyError(f"No commit {item_id!r}: {out.stderr[:200]}")
            return {"sha": item_id, "stat": out.stdout.strip()}

        raise KeyError(
            f"GitSource.drill_down: unknown field {field!r}. "
            "Valid: 'full_message', 'diff_stats'."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_repo(override: str | None) -> Path:
    if override:
        return Path(override)
    return repo_root()


def _head_sha(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(repo),
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


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
        # "last N days" → since only. build_triage_context used 24h,
        # but window_days=1 maps here cleanly without fractional logic.
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
    return 20  # NORMAL — matches build_triage_context's original cap


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


_registry.register(GitSource())
