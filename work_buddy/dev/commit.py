"""Dev-PR auto_run helpers: assess git state and scan the change set.

Backs the ``assess``, ``pii_check``, and ``transient_check`` auto_run steps
in the ``dev-pr`` workflow. Kept separate from ``document.py`` so the two
workflows can share the deterministic-offloading pattern without bleeding
concerns across files.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath
from typing import Any

from work_buddy.dev.document import _classify, _run_git
from work_buddy.logging_config import get_logger
from work_buddy.paths import repo_root

logger = get_logger(__name__)


# Branch-naming conventions; surfaced back to the agent when a branch needs creating.
_PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master"})

# Personally-identifying string patterns that should never land in shared code.
# Matches are regex-based and case-insensitive. Caller gets the literal
# offending text plus the file + line for fast triage.
_PII_PATTERNS: tuple[tuple[str, str], ...] = (
    # Vault and user-home paths (Windows + POSIX variants).
    (r"C:[\\/]Vaults\b", "windows-vault-path"),
    (r"/Users/[A-Za-z0-9._-]+", "posix-user-home"),
    (r"/home/[a-z][a-z0-9._-]*\b", "linux-user-home"),
    (r"C:[\\/]Users[\\/][^\\/\s\"']+", "windows-user-home"),
    # Named vault that routinely appears in personal paths.
    (r"\bSecondBrain\b", "personal-vault-name"),
    # Obsidian URIs carrying a vault name.
    (r"obsidian://open\?vault=[^\"'\s&]+", "obsidian-uri"),
)

_COMPILED_PII = tuple(
    (re.compile(pat, re.IGNORECASE), label) for pat, label in _PII_PATTERNS
)


def _guess_test_candidates(changed_files: list[str]) -> list[str]:
    """Suggest test files to run given changed module paths.

    Simple heuristic: for each ``work_buddy/foo/bar.py``, look for
    ``tests/unit/test_bar.py`` or ``tests/component/test_bar.py``. Also
    include any file under ``tests/`` that was changed directly.
    """
    repo = repo_root()
    candidates: list[str] = []
    seen: set[str] = set()

    for f in changed_files:
        norm = f.replace("\\", "/")
        if norm.startswith("tests/") and norm.endswith(".py"):
            if norm not in seen:
                seen.add(norm)
                candidates.append(norm)
            continue
        if not (norm.startswith("work_buddy/") and norm.endswith(".py")):
            continue
        stem = PurePosixPath(norm).stem
        if stem == "__init__":
            continue
        for probe in (
            f"tests/unit/test_{stem}.py",
            f"tests/component/test_{stem}.py",
            f"tests/integration/test_{stem}.py",
        ):
            if (repo / probe).exists() and probe not in seen:
                seen.add(probe)
                candidates.append(probe)
    return candidates


def assess_state() -> dict[str, Any]:
    """Snapshot the git state relevant to committing.

    Returns:
        Dict with:
            - ``current_branch``: str (empty on detached HEAD / failure)
            - ``is_main``: bool — True if on a protected branch
            - ``changed_files``: list[str] — tracked diffs + untracked (non-gitignored)
            - ``classified``: dict[str, list[str]] — module/knowledge/slash/tests/config/other
            - ``test_candidates``: list[str] — suggested test files to run
            - ``warnings``: list[str] — non-fatal issues (on main, empty diff, etc.)
    """
    branch_lines = _run_git("branch", "--show-current")
    current_branch = branch_lines[0] if branch_lines else ""

    tracked = _run_git("diff", "--name-only", "HEAD")
    untracked = _run_git("ls-files", "--others", "--exclude-standard")
    changed = sorted({*(p.replace("\\", "/") for p in tracked + untracked)})

    classified: dict[str, list[str]] = {
        "module": [], "knowledge": [], "slash": [],
        "tests": [], "config": [], "other": [],
    }
    for f in changed:
        classified[_classify(f)].append(f)

    test_candidates = _guess_test_candidates(changed)

    warnings: list[str] = []
    is_main = current_branch in _PROTECTED_BRANCHES
    if is_main:
        warnings.append(
            f"On protected branch '{current_branch}'. Create a feature "
            f"branch (fix/..., feat/..., docs/..., chore/...) before committing."
        )
    if not changed:
        warnings.append("No uncommitted or untracked changes detected. Nothing to commit.")
    if classified["knowledge"]:
        warnings.append(
            "Direct edits to knowledge/store/*.md detected — ensure they were "
            "validated and reconciled (the docs_edit workflow does this, or run "
            "docs_validate + agent_docs_rebuild)."
        )

    return {
        "current_branch": current_branch,
        "is_main": is_main,
        "changed_files": changed,
        "classified": classified,
        "test_candidates": test_candidates,
        "warnings": warnings,
    }


def _scan_text_for_pii(text: str) -> list[dict[str, Any]]:
    """Return one hit per line of text that matches any PII pattern."""
    hits: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pat, label in _COMPILED_PII:
            m = pat.search(line)
            if m:
                hits.append({
                    "line": lineno,
                    "label": label,
                    "match": m.group(0),
                    "context": line.strip()[:200],
                })
                break  # one hit per line is enough; agent can re-grep for detail
    return hits


def pii_check(files: list[str] | None = None) -> dict[str, Any]:
    """Scan candidate files for PII patterns that must not land in the repo.

    Args:
        files: Repo-relative file paths. If omitted, defaults to the current
               tracked-diff + untracked file set (same as ``assess_state``).

    Returns:
        Dict with:
            - ``files_scanned``: list[str]
            - ``hits``: list[{file, line, label, match, context}]
            - ``clean``: bool — True iff ``hits`` is empty
    """
    if files is None:
        tracked = _run_git("diff", "--name-only", "HEAD")
        untracked = _run_git("ls-files", "--others", "--exclude-standard")
        files = sorted({*(p.replace("\\", "/") for p in tracked + untracked)})

    repo = repo_root()
    hits: list[dict[str, Any]] = []
    scanned: list[str] = []

    for f in files:
        norm = f.replace("\\", "/")
        # Skip binary/irrelevant extensions.
        suffix = PurePosixPath(norm).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".whl"}:
            continue
        target = repo / norm
        if not target.exists() or not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned.append(norm)
        for hit in _scan_text_for_pii(text):
            hit["file"] = norm
            hits.append(hit)

    return {
        "files_scanned": scanned,
        "hits": hits,
        "clean": not hits,
    }


# Identifier-form archaeology that only occurs in code. The prose patterns
# in ``knowledge.validate.TRANSIENT_PATTERNS`` require a space or hyphen
# between the label word and the number, so ``SLICE_2_COLUMNS``-style
# identifiers slip through them.
_CODE_IDENT_RE = re.compile(
    # ``\b_?`` rather than plain ``\b``: underscore is a word character,
    # so ``\bSLICE`` never matches inside ``_SLICE_2_COLUMNS``.
    r"\b_?(?:SLICE|PHASE|STAGE|MILESTONE|WAVE)_\d+[A-Z_]*\b"
    r"|_(?:slice|phase|stage)_?\d+"
)

# Journal-shape surfaces exempt from the durable-surfaces rule (see
# ``dev/durable-surfaces``): write-once narratives where transient
# references are the point.
_TRANSIENT_EXEMPT_FILES: frozenset[str] = frozenset({"CHANGELOG.md", "DECISIONS.md"})

# In test files, date literals and task-id-shaped strings are almost always
# fixture data rather than archaeology; suppress those two categories there
# so the hit list stays judgeable.
_TEST_SUPPRESSED_CATEGORIES: frozenset[str] = frozenset({"date", "task_ref"})


def transient_check(files: list[str] | None = None) -> dict[str, Any]:
    """Scan candidate files for transient identifiers (durable-surfaces rule).

    Diff-scoped sibling of :func:`pii_check`. Code archaeology enters the
    repo through commits, so scanning the change set at commit time stops
    new stage labels, VCS references, and migration narrative at the door.
    The store-wide backstop for knowledge units is the ``durable_surfaces``
    check in ``work_buddy.knowledge.validate``; this function shares its
    pattern table so the two surfaces never drift.

    Hits are advisory input to the workflow's cleanup step: the committing
    agent judges each one (a versioned schema name or a quoted example is
    legitimate; a rollout label is not).

    Args:
        files: Repo-relative file paths. If omitted, defaults to the current
               tracked-diff + untracked file set (same as ``pii_check``).

    Returns:
        Dict with:
            - ``files_scanned``: list[str]
            - ``hits``: list[{file, line, category, match, context}]
            - ``clean``: bool
    """
    # Imported here to keep this module light at import time; the pattern
    # table lives with the store-wide check as the single source of truth.
    from work_buddy.knowledge.validate import TRANSIENT_PATTERNS

    patterns: list[tuple[str, re.Pattern[str]]] = (
        list(TRANSIENT_PATTERNS) + [("stage_label_ident", _CODE_IDENT_RE)]
    )

    if files is None:
        tracked = _run_git("diff", "--name-only", "HEAD")
        untracked = _run_git("ls-files", "--others", "--exclude-standard")
        files = sorted({*(p.replace("\\", "/") for p in tracked + untracked)})

    repo = repo_root()
    hits: list[dict[str, Any]] = []
    scanned: list[str] = []

    for f in files:
        norm = f.replace("\\", "/")
        suffix = PurePosixPath(norm).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".whl"}:
            continue
        if PurePosixPath(norm).name in _TRANSIENT_EXEMPT_FILES:
            continue
        target = repo / norm
        if not target.exists() or not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned.append(norm)
        in_tests = norm.startswith("tests/")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for category, pat in patterns:
                if in_tests and category in _TEST_SUPPRESSED_CATEGORIES:
                    continue
                m = pat.search(line)
                if m:
                    hits.append({
                        "file": norm,
                        "line": lineno,
                        "category": category,
                        "match": m.group(0),
                        "context": line.strip()[:200],
                    })
                    break  # one hit per line; agent can re-grep for detail

    return {
        "files_scanned": scanned,
        "hits": hits,
        "clean": not hits,
    }
