"""Git utilities for work-buddy."""

from pathlib import Path

from work_buddy.process import run_tool

# Repository root (parent of work_buddy/ package)
_REPO_ROOT = Path(__file__).parent.parent.parent


def get_wb_commit_hash() -> str:
    """Get the short commit hash of the work-buddy repo."""
    try:
        result = run_tool(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"
