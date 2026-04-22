"""Dev-document scan: classify current-session changes and point at knowledge
units that likely need updating.

This module backs the ``dev-document`` workflow's ``scan`` auto_run step.
The intelligence lives in the calling agent — this step provides a
deterministic starting set (changed files, their subsystems, and knowledge
units that textually reference them) so the agent does not have to
reconstruct that by hand.

The agent is expected to supplement this with semantic searches against
``agent_docs`` during the subsequent reasoning step; ``scan_changes`` is
deliberately a grep-level match, not a semantic one.
"""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath
from typing import Any

from work_buddy.knowledge.store import load_store
from work_buddy.logging_config import get_logger
from work_buddy.paths import repo_root

logger = get_logger(__name__)


# Classification buckets, ordered by specificity (first match wins).
_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("slash", (".claude/commands/",)),
    ("knowledge", ("knowledge/store/",)),
    ("tests", ("tests/",)),
    ("module", ("work_buddy/",)),
)

# File-extension-level fallbacks when no prefix matches.
_EXT_BUCKETS: dict[str, str] = {
    ".md": "config",
    ".yaml": "config",
    ".yml": "config",
    ".toml": "config",
    ".json": "config",
    ".cfg": "config",
    ".ini": "config",
}


def _run_git(*args: str) -> list[str]:
    """Run a git command in the repo root; return stdout lines (no blanks)."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(repo_root()),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git %s failed: %s", " ".join(args), exc)
        return []
    if out.returncode != 0:
        logger.debug("git %s rc=%d: %s", " ".join(args), out.returncode, out.stderr.strip())
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _classify(path: str) -> str:
    norm = path.replace("\\", "/")
    for bucket, prefixes in _BUCKETS:
        if any(norm.startswith(p) for p in prefixes):
            return bucket
    suffix = PurePosixPath(norm).suffix.lower()
    if suffix in _EXT_BUCKETS:
        return _EXT_BUCKETS[suffix]
    return "other"


def _subsystem_slugs(path: str) -> list[str]:
    """Derive subsystem slugs from a changed file path.

    ``work_buddy/obsidian/tasks/namespace_suggest.py`` →
    ``["obsidian", "obsidian/tasks", "namespace_suggest"]``.

    The stem is included separately so knowledge units that only mention
    a module name (e.g. "namespace_suggest" in content) still match.
    """
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    slugs: list[str] = []

    if parts[0] == "work_buddy" and len(parts) >= 2:
        accum: list[str] = []
        for p in parts[1:-1]:  # skip leaf
            accum.append(p)
            slugs.append("/".join(accum))
        stem = PurePosixPath(parts[-1]).stem
        if stem and stem != "__init__":
            slugs.append(stem)
    elif parts[0] == "knowledge" and len(parts) >= 3:
        stem = PurePosixPath(parts[-1]).stem
        if stem:
            slugs.append(stem)
    elif parts[0] == ".claude" and "commands" in parts:
        stem = PurePosixPath(parts[-1]).stem  # e.g. "wb-commit"
        if stem.startswith("wb-"):
            slugs.append(stem[3:])  # "commit"
        slugs.append(stem)
    return [s for s in slugs if s]


def _match_units(
    changed_files: list[str],
    subsystem_slugs: list[str],
) -> list[dict[str, Any]]:
    """Find knowledge units whose text references a changed path or subsystem.

    Returns one entry per matching unit with the specific tokens that matched,
    so the agent can judge why the unit was flagged.
    """
    if not (changed_files or subsystem_slugs):
        return []

    # File-path tokens: the full repo-relative path, plus the bare filename.
    # ``__init__.py`` is filtered — it appears in every package and yields noise.
    path_tokens: set[str] = set()
    for f in changed_files:
        norm = f.replace("\\", "/")
        path_tokens.add(norm)
        name = PurePosixPath(norm).name
        if name != "__init__.py":
            path_tokens.add(name)

    slug_tokens: set[str] = {s for s in subsystem_slugs if len(s) >= 3}

    try:
        store = load_store(scope="system")
    except Exception as exc:  # defensive; failure here shouldn't kill the scan
        logger.warning("knowledge store load failed: %s", exc)
        return []

    matches: list[dict[str, Any]] = []
    for unit_path, unit in store.items():
        haystack = " ".join(
            [
                unit.name or "",
                unit.description or "",
                unit.content.get("full", "") or "",
                unit.content.get("summary", "") or "",
                " ".join(getattr(unit, "entry_points", []) or []),
                " ".join(unit.tags or []),
            ]
        )
        matched_on: list[str] = []
        for tok in path_tokens:
            if tok and tok in haystack:
                matched_on.append(tok)
        # Slug matches require word boundaries so "dev" doesn't match "developer".
        # Cheap check: pad haystack with spaces and look for padded token.
        padded = f" {haystack} "
        for slug in slug_tokens:
            needle = slug.replace("/", " ")
            # bare appearance, either space-delimited or punctuation-delimited
            if f" {needle} " in padded or f"/{needle}" in haystack or f"{needle}." in haystack:
                matched_on.append(slug)
        if matched_on:
            matches.append(
                {
                    "path": unit_path,
                    "kind": unit.kind,
                    "name": unit.name,
                    "description": unit.description,
                    "matched_on": sorted(set(matched_on)),
                }
            )
    # Deterministic ordering: units with more matches first, then by path.
    matches.sort(key=lambda m: (-len(m["matched_on"]), m["path"]))
    return matches


def scan_changes(base_ref: str = "HEAD") -> dict[str, Any]:
    """Enumerate current-session code changes and flag candidate knowledge units.

    The result is consumed by the ``dev-document`` workflow's reasoning step.

    Args:
        base_ref: Git ref to diff against. Default ``"HEAD"`` captures both
                  staged and unstaged changes (the common case: ``/wb-dev-document``
                  runs *before* committing).

    Returns:
        Dict with:
            - ``changed_files`` (list[str]) - repo-relative, forward-slash paths
            - ``classified`` (dict[str, list[str]]) - files grouped by bucket
            - ``subsystem_slugs`` (list[str]) - unique module/subsystem keys
            - ``candidate_units`` (list[dict]) - knowledge units referencing
              a changed file or subsystem, newest-first by match strength
            - ``base_ref`` (str) - echoed back for traceability
            - ``warnings`` (list[str]) - non-fatal issues (e.g. empty diff)
    """
    # Tracked changes (unstaged + staged) vs base_ref.
    tracked = _run_git("diff", "--name-only", base_ref)
    # Untracked files that aren't gitignored (new files the agent just wrote).
    untracked = _run_git("ls-files", "--others", "--exclude-standard")

    changed = sorted({*(p.replace("\\", "/") for p in tracked + untracked)})

    classified: dict[str, list[str]] = {
        "module": [],
        "knowledge": [],
        "slash": [],
        "tests": [],
        "config": [],
        "other": [],
    }
    for f in changed:
        classified[_classify(f)].append(f)

    slugs: list[str] = []
    seen: set[str] = set()
    for f in changed:
        for s in _subsystem_slugs(f):
            if s not in seen:
                seen.add(s)
                slugs.append(s)

    candidates = _match_units(changed, slugs)

    warnings: list[str] = []
    if not changed:
        warnings.append(
            f"No changes detected vs {base_ref}. "
            "Either nothing to document, or you are on a clean tree."
        )
    if classified["knowledge"]:
        warnings.append(
            "Direct edits to knowledge/store/*.json detected. "
            "Prefer docs_create/docs_update/docs_delete — hand-edits bypass DAG validation."
        )

    return {
        "changed_files": changed,
        "classified": classified,
        "subsystem_slugs": slugs,
        "candidate_units": candidates,
        "base_ref": base_ref,
        "warnings": warnings,
    }
