"""Filesystem source for the vault semantic index — multi-vault discovery.

Walks the configured "vaults" (roots), applies dot-dir + name-based directory
pruning and gitignore-style include/exclude globs (via ``pathspec``), and yields
the indexable Markdown files with vault-namespaced ids. ``parse()`` turns a file
into ``Chunk``s via the content-handler registry.

A "vault" is any indexed root (Obsidian or not) with a stable ``id``. Chunk keys
are ``{vault_id}/{relative_path}`` (posix) — composed HERE; the chunker and store
stay vault-agnostic.

**Reachability boundary.** This layer DETECTS an unreachable
vault (missing path, unmounted drive) and reports it via :class:`VaultStatus` — it
never prunes chunks. The prune-vs-keep reconciliation (and the move/delete/offline
warning) is the indexer's job: reconcile only ``reachable=True`` vaults; for an
unreachable vault, KEEP its existing chunks and warn. ``VaultStatus.reason``
distinguishes a misconfigured path from a transient mount failure.

**Pathspec note.** Directory pruning uses only the literal folder-name set
(dot-dirs + ``obsidian.exclude_folders``), NOT the pathspec — a pathspec negation
can rescue a descendant of an "excluded-looking" directory, so pruning a whole
subtree off the pattern set would be unsound. The include/exclude pathspec is
applied at the file level only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pathspec import PathSpec

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.vault_index.chunker import Chunk
from work_buddy.vault_index.handlers import get_handler

logger = get_logger(__name__)

_DEFAULT_INCLUDE = ("**/*.md",)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VaultConfig:
    id: str
    root: Path
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    dir_excludes: frozenset[str]   # lowercased literal dir-NAME prune set


def _resolve_root(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    from work_buddy.paths import repo_root
    return repo_root() / p


def load_vault_configs(cfg: dict | None = None) -> list[VaultConfig]:
    """Normalize ``vault_index.vaults`` into VaultConfigs (zero-config default).

    Empty/absent ``vaults`` → one synthesized vault ``id="vault"`` rooted at
    ``vault_root``, excluding ``obsidian.exclude_folders``. Returned
    most-specific-first (longest root path) so nested-root resolution and the
    overlap warning are deterministic.
    """
    if cfg is None:
        cfg = load_config()

    vi = cfg.get("vault_index", {}) or {}
    raw_vaults = vi.get("vaults") or {}
    # Fast dir-name prune set. Defaults to obsidian.exclude_folders (what work-buddy
    # already treats as non-vault-content), but the INDEX can override via
    # vault_index.exclude_dirs — e.g. to INCLUDE repos/ that the obsidian
    # context-collector excludes. Dot-dirs are always skipped regardless.
    exclude_dirs_cfg = vi.get("exclude_dirs")
    if exclude_dirs_cfg is None:
        exclude_dirs_cfg = (cfg.get("obsidian", {}) or {}).get("exclude_folders", [])
    dir_excludes = frozenset(f.lower() for f in exclude_dirs_cfg)

    configs: list[VaultConfig] = []
    if not raw_vaults:
        root = cfg.get("vault_root") or ""
        if not root:
            logger.warning(
                "vault_index: no vaults configured and vault_root is empty"
            )
            return []
        configs.append(VaultConfig(
            id="vault",
            root=_resolve_root(root),
            include=_DEFAULT_INCLUDE,
            exclude=(),
            dir_excludes=dir_excludes,
        ))
    else:
        for vid, spec in raw_vaults.items():
            if "/" in vid or "\\" in vid:
                logger.warning(
                    "vault_index: skipping vault id %r — ids must not contain "
                    "'/' or '\\' (they namespace chunk ids)",
                    vid,
                )
                continue
            spec = spec or {}
            path = spec.get("path")
            if not path:
                logger.warning("vault_index: vault %r has no path; skipping", vid)
                continue
            configs.append(VaultConfig(
                id=str(vid),
                root=_resolve_root(path),
                include=tuple(spec.get("include") or _DEFAULT_INCLUDE),
                exclude=tuple(spec.get("exclude") or ()),
                dir_excludes=dir_excludes,
            ))

    configs.sort(key=lambda v: len(str(v.root)), reverse=True)
    _warn_on_overlap(configs)
    return configs


def _warn_on_overlap(configs: list[VaultConfig]) -> None:
    """Warn when one vault's root nests inside (or equals) another's."""
    norm = [(c, os.path.normcase(str(c.root))) for c in configs]
    for i, (ci, ni) in enumerate(norm):
        for cj, nj in norm[i + 1:]:
            if ci.id == cj.id:
                continue
            if ni == nj or ni.startswith(nj + os.sep) or nj.startswith(ni + os.sep):
                logger.warning(
                    "vault_index: vault roots overlap — %r (%s) and %r (%s); "
                    "files in the nested root are assigned to the more-specific vault",
                    ci.id, ci.root, cj.id, cj.root,
                )


# ---------------------------------------------------------------------------
# Discovery results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiscoveredFile:
    item_id: str       # "{vault_id}/{rel_posix}" — stable, vault-namespaced
    vault_id: str
    source_path: str   # == item_id; fed to the handler as Chunk.source_path
    mtime: float
    size: int
    abs_path: str


@dataclass(frozen=True)
class VaultStatus:
    vault_id: str
    root: str
    reachable: bool
    reason: str = ""        # "" | "not_a_directory" | "os_error: <msg>"
    file_count: int = 0


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

class FilesystemSource:
    """Discovers and parses indexable Markdown files across configured vaults."""

    def __init__(self, cfg: dict | None = None) -> None:
        self._cfg = cfg if cfg is not None else load_config()
        self._vaults = load_vault_configs(self._cfg)

    @property
    def name(self) -> str:
        return "vault_index"

    @property
    def vaults(self) -> list[VaultConfig]:
        return self._vaults

    def discover(self) -> tuple[list[DiscoveredFile], list[VaultStatus]]:
        """Walk every reachable vault; return (files, per-vault statuses).

        This layer only reports reachability — it never prunes. See the module
        docstring for the prune-vs-keep contract.
        """
        files: list[DiscoveredFile] = []
        statuses: list[VaultStatus] = []
        roots_by_id = {v.id: os.path.normcase(str(v.root)) for v in self._vaults}

        for vault in self._vaults:
            try:
                reachable = vault.root.is_dir()
            except OSError as exc:
                statuses.append(VaultStatus(
                    vault.id, str(vault.root), False, f"os_error: {exc}"
                ))
                continue
            if not reachable:
                statuses.append(VaultStatus(
                    vault.id, str(vault.root), False, "not_a_directory"
                ))
                continue

            include_spec = PathSpec.from_lines("gitignore", vault.include or _DEFAULT_INCLUDE)
            exclude_spec = PathSpec.from_lines("gitignore", vault.exclude)
            other_roots = {n for vid, n in roots_by_id.items() if vid != vault.id}

            count = 0
            for dirpath, dirnames, filenames in os.walk(vault.root):
                # Prune in place: dot-dirs, named excludes, and any subtree that
                # IS another (more-specific) vault's root.
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and d.lower() not in vault.dir_excludes
                    and os.path.normcase(str(Path(dirpath) / d)) not in other_roots
                ]
                for fname in filenames:
                    p = Path(dirpath) / fname
                    if get_handler(p.suffix) is None:
                        continue
                    rel = p.relative_to(vault.root).as_posix()
                    if not (include_spec.match_file(rel) and not exclude_spec.match_file(rel)):
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    sp = f"{vault.id}/{rel}"
                    files.append(DiscoveredFile(
                        item_id=sp, vault_id=vault.id, source_path=sp,
                        mtime=st.st_mtime, size=st.st_size, abs_path=str(p),
                    ))
                    count += 1
            statuses.append(VaultStatus(vault.id, str(vault.root), True, "", count))

        return files, statuses

    def parse(self, item_id: str) -> list[Chunk]:
        """Read a discovered file and chunk it via its handler.

        ``source_path`` is the vault-namespaced ``item_id``, so the resulting
        chunks' keys / breadcrumbs / embed-input are vault-namespaced for free.
        """
        if "/" not in item_id:
            return []
        vault_id, rel = item_id.split("/", 1)
        vc = next((v for v in self._vaults if v.id == vault_id), None)
        if vc is None:
            return []
        p = vc.root / rel
        handler = get_handler(p.suffix)
        if handler is None:
            return []
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return handler.chunk(text, source_path=item_id)
