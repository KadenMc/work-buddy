"""Assemble the work-buddy source-tree payload for the native installers.

Copies the files the installer lays down in the user's HOME working copy: the
work_buddy package, the shipped asset trees (knowledge/store, prompts,
sidecar_jobs, .claude), config templates, docs, and the project metadata
(pyproject/README/LICENSE/CHANGELOG). Prunes dev-only trees (tests, .data,
exploration, .git, caches) and bytecode by way of an allowlist.

The payload is a plain source tree (no .git); Model A's git-working-copy nature
is set up by the update lifecycle later. The heavy dependencies are NOT bundled:
uv downloads them at install time.

Build from a CLEAN CHECKOUT only (CI checkouts are; locally use a git worktree).
A live working tree hides gitignored private files inside shipped directories
(e.g. the personal knowledge store at knowledge/store.local), and copytree would
sweep them into the installer. build_payload refuses to run when it detects the
telltale local-only files.

Usage:  python packaging/build_payload.py --out dist/payload [--root <repo>]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

# Top-level items shipped into the user's HOME. An allowlist (safer than a
# denylist): anything not named here is simply not shipped.
INCLUDE = [
    "work_buddy",
    "knowledge",
    "prompts",
    "sidecar_jobs",
    ".claude",
    "docs",
    "config.example.yaml",
    "config.local.yaml.example",
    "config.local.example.yaml",
    "pyproject.toml",
    "poetry.lock",
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    ".mcp.json",
]

# Pruned wherever they appear inside the copied trees.
PRUNE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
PRUNE_SUFFIXES = {".pyc", ".pyo"}

# Presence of any of these marks a LIVE working tree (all are gitignored, so a
# clean checkout has none). Refuse to build: private local state must never be
# swept into an installer payload.
LIVE_TREE_MARKERS = [
    "knowledge/store.local",
    ".claude/settings.local.json",
    "config.local.yaml",
    ".env",
]


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [
        n
        for n in names
        if n in PRUNE_DIRS or any(n.endswith(s) for s in PRUNE_SUFFIXES)
    ]


def build_payload(root: Path, out: Path) -> dict:
    """Copy the installer payload from ``root`` into ``out``. Returns a summary."""
    found = [m for m in LIVE_TREE_MARKERS if (root / m).exists()]
    if found:
        raise ValueError(
            f"refusing to build from a live working tree ({', '.join(found)} present); "
            "build from a clean checkout (CI) or a git worktree"
        )
    out = out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    copied: list[str] = []
    missing: list[str] = []
    for name in INCLUDE:
        src = root / name
        if not src.exists():
            missing.append(name)
            continue
        dst = out / name
        if src.is_dir():
            shutil.copytree(src, dst, ignore=_ignore)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    return {"out": str(out), "copied": copied, "missing": missing}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble the work-buddy installer payload.")
    ap.add_argument("--out", required=True, help="output payload directory (recreated)")
    ap.add_argument(
        "--root", default=None,
        help="repo root to copy from (default: two levels up from this script)",
    )
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    summary = build_payload(root, Path(args.out))
    print(f"payload -> {summary['out']}")
    print(f"copied: {', '.join(summary['copied'])}")
    if summary["missing"]:
        print(f"skipped (absent): {', '.join(summary['missing'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
