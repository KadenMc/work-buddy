"""Safe folder-relative file path resolution for Co-work.

All Co-work file operations use this module rather than joining caller input
onto a folder path directly.  The returned path is suitable for an immediate
operation, but callers performing a mutation must still hold the applicable
folder/path lock and re-run this resolver directly before I/O.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_RESERVED_COMPONENTS = frozenset({".wbuddy"})
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class CoworkPathError(ValueError):
    """A caller-supplied document path is unsafe or outside its folder."""


@dataclass(frozen=True, slots=True)
class ResolvedRelativeFilePath:
    """Immutable result of a contained folder-relative file resolution."""

    normalized: str
    path: Path
    path_key: str


@dataclass(frozen=True, slots=True)
class ResolvedMarkdownPath(ResolvedRelativeFilePath):
    """Compatibility result type for a contained Markdown path."""


def _folder_root(store_or_root: Any) -> Path:
    paths = getattr(store_or_root, "paths", None)
    if paths is not None and hasattr(paths, "root"):
        candidate = paths.root
    elif hasattr(store_or_root, "root") and hasattr(store_or_root, "sidecar"):
        candidate = store_or_root.root
    else:
        candidate = store_or_root
    try:
        root = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise CoworkPathError("The folder root does not exist or is not accessible") from exc
    if not root.is_dir():
        raise CoworkPathError("The folder root is not a directory")
    return root


def _is_reparse_or_symlink(path: Path) -> bool:
    """Return whether *path* is a symlink, junction, or other reparse point."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _validate_relative_path(
    relative_path: str,
    *,
    path_label: str,
    allowed_suffixes: frozenset[str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(relative_path, str):
        raise CoworkPathError(f"{path_label} path must be a string")
    if not relative_path:
        raise CoworkPathError(f"{path_label} path cannot be empty")
    if relative_path != relative_path.strip():
        raise CoworkPathError(
            f"{path_label} path cannot have leading or trailing whitespace"
        )
    if "\\" in relative_path:
        raise CoworkPathError(f"{path_label} path must use forward slashes")
    if relative_path.startswith(("/", "//")) or _DRIVE_PREFIX.match(relative_path):
        raise CoworkPathError(f"{path_label} path must be folder-relative")
    if any(ord(character) < 32 or ord(character) == 127 for character in relative_path):
        raise CoworkPathError(f"{path_label} path contains a control character")

    raw_parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise CoworkPathError(
            f"{path_label} path contains an empty, dot, or dot-dot segment"
        )
    if any(part.casefold() in _RESERVED_COMPONENTS for part in raw_parts):
        raise CoworkPathError(
            f"{path_label} path enters a Work Buddy managed namespace"
        )
    if any("\x00" in part for part in raw_parts):
        raise CoworkPathError(f"{path_label} path contains NUL")
    if any(any(character in _WINDOWS_FORBIDDEN for character in part) for part in raw_parts):
        raise CoworkPathError(
            f"{path_label} path contains a reserved filename character"
        )
    if any(part.endswith((" ", ".")) for part in raw_parts):
        raise CoworkPathError(
            f"{path_label} path has a segment ending in a space or dot"
        )
    if any(part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES for part in raw_parts):
        raise CoworkPathError(f"{path_label} path uses a reserved device name")

    normalized_parts = tuple(unicodedata.normalize("NFC", part) for part in raw_parts)
    normalized = "/".join(normalized_parts)
    if (
        allowed_suffixes is not None
        and Path(normalized_parts[-1]).suffix.casefold() not in allowed_suffixes
    ):
        raise CoworkPathError("Co-work documents must use .md or .markdown")
    return normalized, normalized_parts


def _assert_contained(root: Path, candidate: Path, *, path_label: str) -> None:
    try:
        common = Path(os.path.commonpath((str(root), str(candidate))))
    except ValueError as exc:
        raise CoworkPathError(
            f"{path_label} path is on a different filesystem root"
        ) from exc
    if common != root:
        raise CoworkPathError(f"{path_label} path escapes the selected folder")


def _path_key(normalized: str) -> str:
    # Windows path identity is case-insensitive even when a particular volume
    # happens to preserve case. POSIX identity remains case-sensitive.
    return normalized.casefold() if os.name == "nt" else normalized


def _resolve_relative_file_path(
    store_or_root: Any,
    relative_path: str,
    *,
    for_create: bool = False,
    path_label: str,
    allowed_suffixes: frozenset[str] | None = None,
) -> ResolvedRelativeFilePath:
    """Resolve one safe folder-relative file path.

    ``for_create`` permits the final path to be absent but requires every
    existing ancestor to be a real directory. Symlinks and Windows reparse
    points are refused in both modes so a later filesystem change cannot turn
    a previously contained lexical path into an external target. Existence is
    intentionally not enforced for reads: callers use the same resolver for
    typed missing-file diagnostics. Atomic create-if-absent remains the final
    race arbiter for creation.
    """

    root = _folder_root(store_or_root)
    normalized, parts = _validate_relative_path(
        relative_path,
        path_label=path_label,
        allowed_suffixes=allowed_suffixes,
    )
    lexical = root.joinpath(*parts)
    _assert_contained(root, lexical, path_label=path_label)

    cursor = root
    for index, part in enumerate(parts):
        cursor = cursor / part
        is_final = index == len(parts) - 1
        if not cursor.exists() and not cursor.is_symlink():
            if not is_final and not for_create:
                # A missing intermediate path is still a contained target; the
                # read caller will surface source_not_found. Stop before statting
                # descendants that cannot exist.
                break
            if not is_final and for_create:
                # Nested directories may be created later, but their nearest
                # existing parent has already been proven real and contained.
                continue
            break
        if _is_reparse_or_symlink(cursor):
            raise CoworkPathError(
                f"{path_label} path crosses a symlink or reparse point"
            )
        if not is_final and not cursor.is_dir():
            raise CoworkPathError(f"{path_label} path has a non-directory parent")

    # Resolve every existing prefix once more. ``strict=False`` follows any
    # alias that appeared between lstat calls; containment therefore fails
    # closed even during a narrow TOCTOU window. Mutators re-run under lock.
    resolved = lexical.resolve(strict=False)
    _assert_contained(root, resolved, path_label=path_label)
    return ResolvedRelativeFilePath(
        normalized=normalized,
        path=resolved,
        path_key=_path_key(normalized),
    )


def resolve_relative_file_path(
    store_or_root: Any,
    relative_path: str,
    *,
    for_create: bool = False,
) -> ResolvedRelativeFilePath:
    """Resolve a safe contained file path without imposing a source format."""

    return _resolve_relative_file_path(
        store_or_root,
        relative_path,
        for_create=for_create,
        path_label="File",
    )


def resolve_markdown_path(
    store_or_root: Any,
    relative_path: str,
    *,
    for_create: bool = False,
) -> ResolvedMarkdownPath:
    """Resolve one safe folder-relative Markdown path.

    This compatibility wrapper preserves the Markdown-only admission and error
    contract while sharing every containment check with the generic resolver.
    """

    resolved = _resolve_relative_file_path(
        store_or_root,
        relative_path,
        for_create=for_create,
        path_label="Markdown",
        allowed_suffixes=_MARKDOWN_SUFFIXES,
    )
    return ResolvedMarkdownPath(
        normalized=resolved.normalized,
        path=resolved.path,
        path_key=resolved.path_key,
    )


def resolve_document_source_path(
    store_or_root: Any,
    document: Any,
) -> ResolvedRelativeFilePath:
    """Resolve the external path associated with a persisted document.

    Ordinary file-backed documents remain Markdown-only. A detached import's
    path names its acquisition source and can therefore use any format admitted
    by the importer registry that created it.
    """

    from work_buddy.truth.documents import source_is_detached

    relative_path = getattr(document, "path", None)
    if source_is_detached(document):
        return resolve_relative_file_path(store_or_root, relative_path)
    return resolve_markdown_path(store_or_root, relative_path)


def resolve_writeback_target(
    store_or_root: Any,
    document: Any,
    *,
    for_create: bool = False,
) -> ResolvedMarkdownPath:
    """Resolve a Markdown Save target after enforcing source-writeback policy."""

    from work_buddy.truth.documents import source_is_detached

    if source_is_detached(document):
        raise CoworkPathError(
            "An imported source file cannot be used as a Co-work writeback target"
        )
    return resolve_markdown_path(
        store_or_root,
        getattr(document, "path", None),
        for_create=for_create,
    )


__all__ = [
    "CoworkPathError",
    "ResolvedMarkdownPath",
    "ResolvedRelativeFilePath",
    "resolve_document_source_path",
    "resolve_markdown_path",
    "resolve_relative_file_path",
    "resolve_writeback_target",
]
