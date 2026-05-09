"""Dev-document scan: classify current-session changes and point at knowledge
units that likely need updating.

This module backs the ``dev-document`` workflow's ``scan`` auto_run step.
The intelligence lives in the calling agent — this step provides a
deterministic starting set (changed files, their subsystems, and a ranked
list of candidate knowledge units) so the agent does not have to
reconstruct that by hand.

The scan ranks candidates via the work-buddy knowledge search (BM25 +
dense embeddings), with a scored substring-grep fallback when the
embedding service is unhealthy. A small "force-include" override surfaces
units whose ``entry_points`` or ``tags`` fields exactly match a changed
module path — embeddings can miss those when a unit's prose is sparse.
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

# Cap on candidates returned to the agent. Tuned for the propose step's
# discipline: large enough to surface all plausible candidates for moderate
# branches, small enough that the agent can realistically read each at full
# depth.
_TOP_N = 20


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


def _module_paths_from_changed(changed_files: list[str]) -> list[str]:
    """Derive ``work_buddy.foo.bar`` dotted paths from changed file paths.

    Used by ``_force_include_canonical_units`` to match against unit
    ``entry_points`` (which typically read like ``work_buddy.dashboard.api``).
    """
    out: list[str] = []
    for f in changed_files:
        norm = f.replace("\\", "/")
        if not (norm.startswith("work_buddy/") and norm.endswith(".py")):
            continue
        # Strip "work_buddy/" and ".py", convert "/" to ".", drop "__init__".
        inner = norm[len("work_buddy/"): -len(".py")]
        if inner.endswith("/__init__"):
            inner = inner[: -len("/__init__")]
        if inner:
            out.append("work_buddy." + inner.replace("/", "."))
    return out


def _build_rag_query(changed_files: list[str], slugs: list[str]) -> str:
    """Build a query string for the knowledge search.

    Mixes subsystem slugs (high signal — these are subsystem names) with
    leaf basenames from changed files (medium signal — module names).
    Slashes in slugs are flattened to spaces so each token is independent.
    """
    parts: list[str] = []
    for s in slugs:
        parts.append(s.replace("/", " "))
    for f in changed_files:
        name = PurePosixPath(f.replace("\\", "/")).stem
        if name and name != "__init__":
            parts.append(name)
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return " ".join(deduped)


def _slim_search_hit(hit: dict[str, Any], why: str) -> dict[str, Any]:
    """Reduce a search() result to the {path, name, description, score, why} contract."""
    return {
        "path": hit.get("path", ""),
        "name": hit.get("name", ""),
        "description": hit.get("description", ""),
        "score": round(float(hit.get("score", 0.0)), 4),
        "why": why,
    }


def _search_units_via_rag(
    changed_files: list[str],
    slugs: list[str],
) -> list[dict[str, Any]] | None:
    """Query the knowledge search for candidate units.

    Returns:
        A list of ``{path, name, description, score, why}`` dicts on success,
        or ``None`` if the search failed (embedding service unavailable,
        empty results, exception).  ``None`` is the signal for ``scan_changes``
        to fall back to the scored grep path.
    """
    if not (changed_files or slugs):
        return []

    query = _build_rag_query(changed_files, slugs)
    if not query.strip():
        return []

    try:
        from work_buddy.knowledge.search import search
        result = search(
            query=query,
            knowledge_scope="system",
            top_n=_TOP_N,
            depth="index",
        )
    except Exception as exc:  # noqa: BLE001 — fallback path is the recovery
        logger.warning("RAG search failed (%s); falling back to grep.", exc)
        return None

    if "error" in result:
        logger.warning("RAG search returned error (%s); falling back to grep.", result["error"])
        return None

    raw = result.get("results") or []
    if not raw:
        # Empty results from search aren't necessarily wrong (the diff might
        # genuinely have no related units), but the contract requires us to
        # surface them anyway.  Return an empty list, not None.
        return []

    # Synthesize a "why" from the slugs that contributed to the query.
    why_summary = ", ".join(slugs[:3]) if slugs else "matched query terms"
    why_label = f"matched: {why_summary}"
    return [_slim_search_hit(hit, why=why_label) for hit in raw]


def _match_units_via_grep(
    changed_files: list[str],
    subsystem_slugs: list[str],
) -> list[dict[str, Any]]:
    """Scored substring-grep fallback when the RAG search is unavailable.

    Scoring weights:
      - 3: unit's ``entry_points`` or ``tags`` exactly contain a changed
        module path (path-component match).
      - 2: subsystem slug appears as a word-boundary token in the
        unit's text (stronger signal than substring).
      - 1: changed-file path or basename appears as a substring in
        the unit's text (any prose mention).

    Returns up to ``_TOP_N`` candidates sorted by ``(-score, path)``.
    """
    if not (changed_files or subsystem_slugs):
        return []

    # Path tokens: full repo-relative path + bare filename, minus __init__.py.
    path_tokens: set[str] = set()
    for f in changed_files:
        norm = f.replace("\\", "/")
        path_tokens.add(norm)
        name = PurePosixPath(norm).name
        if name != "__init__.py":
            path_tokens.add(name)

    slug_tokens: set[str] = {s for s in subsystem_slugs if len(s) >= 3}
    module_paths = _module_paths_from_changed(changed_files)

    try:
        store = load_store(scope="system")
    except Exception as exc:
        logger.warning("knowledge store load failed: %s", exc)
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
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
        score = 0.0
        why_parts: list[str] = []

        # Weight 3: unit explicitly names this module via entry_points or tags.
        ep_tag_str = (
            " ".join(getattr(unit, "entry_points", []) or [])
            + " "
            + " ".join(unit.tags or [])
        )
        for mp in module_paths:
            if mp in ep_tag_str:
                score += 3.0
                why_parts.append(f"canonical: {mp}")

        # Weight 2: subsystem slug as a word-boundary token.
        padded = f" {haystack} "
        for slug in slug_tokens:
            needle = slug.replace("/", " ")
            if (
                f" {needle} " in padded
                or f"/{needle}" in haystack
                or f"{needle}." in haystack
            ):
                score += 2.0
                why_parts.append(f"slug: {slug}")

        # Weight 1: changed-file path or basename anywhere in haystack.
        for tok in path_tokens:
            if tok and tok in haystack:
                score += 1.0
                why_parts.append(f"path: {tok}")

        if score > 0:
            scored.append(
                (
                    score,
                    {
                        "path": unit_path,
                        "name": unit.name or "",
                        "description": unit.description or "",
                        "score": round(score, 4),
                        "why": "; ".join(why_parts[:3]),
                    },
                )
            )

    scored.sort(key=lambda t: (-t[0], t[1]["path"]))
    return [c for _, c in scored[:_TOP_N]]


def _force_include_canonical_units(
    changed_files: list[str],
    slugs: list[str],
) -> list[dict[str, Any]]:
    """Return units whose ``entry_points`` exactly contain a changed module path.

    These are canonical-doc cases: a unit that documents
    ``work_buddy.dashboard.forms`` will list that path under
    ``entry_points``, but its prose may be too sparse for embeddings to bind
    tightly.  Pinning these by structural match guarantees they surface.

    Tag matches are weaker (tags are often free-form keywords); only
    ``entry_points`` get the force-include treatment here.
    """
    module_paths = _module_paths_from_changed(changed_files)
    if not module_paths:
        return []

    try:
        store = load_store(scope="system")
    except Exception as exc:
        logger.warning("knowledge store load failed: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    for unit_path, unit in store.items():
        eps = getattr(unit, "entry_points", []) or []
        if not eps:
            continue
        matched = [mp for mp in module_paths if any(mp == ep or ep.startswith(mp + ".") or mp.startswith(ep + ".") for ep in eps)]
        if matched:
            out.append({
                "path": unit_path,
                "name": unit.name or "",
                "description": unit.description or "",
                "score": 1.0,  # Canonical pin — score is sentinel, not relative.
                "why": f"canonical entry_points: {matched[0]}",
            })
    return out


def _merge_candidates(
    primary: list[dict[str, Any]],
    forced: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge force-included canonicals into the primary list.

    Forced entries that aren't already in the primary list are inserted at
    the top (canonical pins outrank score-based ranking).  Primary entries
    that overlap with forced are deduplicated, with the primary's score
    preserved (it's the more discriminating signal).
    """
    seen = {c["path"] for c in primary}
    new_forced = [f for f in forced if f["path"] not in seen]
    return new_forced + primary


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
            - ``candidate_units`` (list[dict]) - knowledge units relevant to
              the changes, slimmed to ``{path, name, description, score, why}``,
              capped at ``_TOP_N`` entries
            - ``base_ref`` (str) - echoed back for traceability
            - ``warnings`` (list[str]) - non-fatal issues (e.g. empty diff)
            - ``_source`` (str) - ``"rag"`` or ``"grep_fallback"``, indicates
              which matching path produced the candidates
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

    # Try RAG first; fall back to scored grep on failure.
    rag_candidates = _search_units_via_rag(changed, slugs)
    if rag_candidates is None:
        candidates = _match_units_via_grep(changed, slugs)
        source = "grep_fallback"
    else:
        candidates = rag_candidates
        source = "rag"

    # Force-include canonical units regardless of which path ran.
    canonical = _force_include_canonical_units(changed, slugs)
    candidates = _merge_candidates(candidates, canonical)[:_TOP_N]

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
        "_source": source,
    }
