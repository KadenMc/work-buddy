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

import re
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
    """Build the *structural* RAG query string from path tokens.

    Mixes subsystem slugs (high signal — these are subsystem names) with
    leaf basenames from changed files (medium signal — module names).
    Slashes in slugs are flattened to spaces so each token is independent.

    This is one of two query sources fused by ``_search_units_via_rag``;
    see ``_read_module_docstring`` for the other.
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


# Regex to extract a Python module's top-of-file docstring.  Anchored at the
# start of the file (modulo optional encoding/comment lines) and matches the
# first triple-quoted string at module scope.  Multiline / dotall flags so
# embedded newlines work; non-greedy so the first ``"""`` closes the match.
_MODULE_DOCSTRING_RE = re.compile(
    r'^\s*(?:#[^\n]*\n)*\s*[ru]?"""(.+?)"""',
    re.DOTALL,
)


def _read_module_docstring(file_path: str, max_chars: int = 500) -> str:
    """Return the first paragraph of a Python module's top-of-file docstring.

    Used to enrich the RAG query with domain-language tokens — file-path
    tokens surface units that mention the file by name; docstring tokens
    surface units that describe the *behavior* in domain language. The two
    are fused via RRF (see ``_search_units_via_rag``).

    For non-Python files, files without a top-of-file docstring, unreadable
    files, or any I/O / decoding error: returns ``""``. Capped at
    ``max_chars`` so a single very long docstring can't dominate the query.
    """
    if not file_path.endswith(".py"):
        return ""
    try:
        # Reading 2KB is plenty for a module docstring — Python convention
        # caps them around a few hundred chars.  Avoids loading huge files.
        with open(repo_root() / file_path, "r", encoding="utf-8") as f:
            head = f.read(2048)
    except (OSError, UnicodeDecodeError):
        return ""

    match = _MODULE_DOCSTRING_RE.search(head)
    if not match:
        return ""

    docstring = match.group(1).strip()
    # Take the first paragraph only — implementation details live further
    # down and add token noise without conceptual signal.
    first_paragraph = docstring.split("\n\n")[0].strip()
    return first_paragraph[:max_chars]


def _slim_search_hit(hit: dict[str, Any], why: str) -> dict[str, Any]:
    """Reduce a search() result to the {path, name, description, score, why} contract.

    When RRF fusion is applied (multi-query path), prefer ``rrf_score`` —
    that's the signal that actually drove the ranking. Falls back to the
    raw single-query ``score`` when ``rrf_score`` is absent (e.g. the
    grep-fallback path).
    """
    raw_score = hit.get("rrf_score", hit.get("score", 0.0))
    return {
        "path": hit.get("path", ""),
        "name": hit.get("name", ""),
        "description": hit.get("description", ""),
        "score": round(float(raw_score), 4),
        "why": why,
    }


def _search_units_via_rag(
    changed_files: list[str],
    slugs: list[str],
) -> list[dict[str, Any]] | None:
    """Multi-query RAG search fused via Reciprocal Rank Fusion.

    Two kinds of signal go in:

    - **Structural query**: path tokens + subsystem slugs. Surfaces units
      that mention the file by name (entry_points, tags, prose pointers).
    - **Per-file docstring queries**: each ``.py`` file in ``changed_files``
      contributes its top-of-file docstring's first paragraph as a separate
      query. Surfaces units that describe the *behavior* in domain language
      (e.g. ``architecture/workflows`` for changes to ``conductor.py``).

    Each query produces its own ranked list; ``rrf_combine`` fuses them
    rank-by-rank with equal voice. Concatenating the queries into one
    string would dilute short structural signals under longer prosier
    docstring text — running them separately and fusing keeps each
    signal's discriminative power.

    Returns:
        A list of ``{path, name, description, score, why}`` dicts on
        success, or ``None`` if the structural query failed (embedding
        service unavailable, exception). ``None`` is the signal for
        ``scan_changes`` to fall back to the scored grep path. Per-file
        docstring queries that fail are logged and skipped — partial
        rankings are better than no rankings.
    """
    if not (changed_files or slugs):
        return []

    from work_buddy.knowledge.search import rrf_combine, search

    rankings: list[list[dict[str, Any]]] = []
    sources_per_path: dict[str, list[str]] = {}  # for "why" labels

    # --- Source 1: structural query (paths + slugs). ---
    structural_query = _build_rag_query(changed_files, slugs)
    if structural_query.strip():
        try:
            result = search(
                query=structural_query,
                knowledge_scope="system",
                top_n=_TOP_N,
                depth="index",
            )
        except Exception as exc:  # noqa: BLE001 — fallback path is the recovery
            logger.warning(
                "RAG structural search failed (%s); falling back to grep.",
                exc,
            )
            return None
        if "error" in result:
            logger.warning(
                "RAG structural search returned error (%s); falling back to grep.",
                result["error"],
            )
            return None
        ranking = result.get("results") or []
        if ranking:
            rankings.append(ranking)
            for hit in ranking:
                sources_per_path.setdefault(hit["path"], []).append("paths")

    # --- Source 2..N: per-file docstring queries. ---
    for f in changed_files:
        docstring = _read_module_docstring(f)
        if not docstring:
            continue
        try:
            result = search(
                query=docstring,
                knowledge_scope="system",
                top_n=_TOP_N,
                depth="index",
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal: drop this signal
            logger.warning(
                "RAG docstring search for %s failed (%s); continuing without it.",
                f, exc,
            )
            continue
        if "error" in result:
            logger.warning(
                "RAG docstring search for %s returned error (%s); skipping.",
                f, result["error"],
            )
            continue
        ranking = result.get("results") or []
        if ranking:
            rankings.append(ranking)
            label = f"docstring({PurePosixPath(f.replace(chr(92), '/')).stem})"
            for hit in ranking:
                sources_per_path.setdefault(hit["path"], []).append(label)

    # If every source returned empty, surface that to the caller as an empty
    # list (genuine "nothing matches"), not as a failure.  ``None`` is reserved
    # for "the structural query couldn't even run" — that's where the grep
    # fallback adds value.
    if not rankings:
        return []

    fused = rrf_combine(rankings)

    # Slim each candidate and synthesize the "why" from which sources
    # contributed.  This is materially more useful than the old single-source
    # "matched: <slugs>" label — a reader can tell at a glance whether a
    # candidate landed via path-mention or domain-language match.
    out: list[dict[str, Any]] = []
    for hit in fused[:_TOP_N]:
        sources = sources_per_path.get(hit["path"], [])
        why = "fused: " + " + ".join(sources) if sources else "matched query"
        out.append(_slim_search_hit(hit, why=why))
    return out


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
            "Direct edits to knowledge/store/*.md detected. "
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
