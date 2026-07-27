"""Safe folder-relative Markdown path resolution for Co-work.

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
class ResolvedMarkdownPath:
    """Immutable result of a contained Markdown path resolution."""

    normalized: str
    path: Path
    path_key: str


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


def _validate_relative_path(relative_path: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(relative_path, str):
        raise CoworkPathError("Markdown path must be a string")
    if not relative_path:
        raise CoworkPathError("Markdown path cannot be empty")
    if relative_path != relative_path.strip():
        raise CoworkPathError("Markdown path cannot have leading or trailing whitespace")
    if "\\" in relative_path:
        raise CoworkPathError("Markdown path must use forward slashes")
    if relative_path.startswith(("/", "//")) or _DRIVE_PREFIX.match(relative_path):
        raise CoworkPathError("Markdown path must be folder-relative")
    if any(ord(character) < 32 or ord(character) == 127 for character in relative_path):
        raise CoworkPathError("Markdown path contains a control character")

    raw_parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise CoworkPathError("Markdown path contains an empty, dot, or dot-dot segment")
    if any(part.casefold() in _RESERVED_COMPONENTS for part in raw_parts):
        raise CoworkPathError("Markdown path enters a Work Buddy managed namespace")
    if any("\x00" in part for part in raw_parts):
        raise CoworkPathError("Markdown path contains NUL")
    if any(any(character in _WINDOWS_FORBIDDEN for character in part) for part in raw_parts):
        raise CoworkPathError("Markdown path contains a reserved filename character")
    if any(part.endswith((" ", ".")) for part in raw_parts):
        raise CoworkPathError("Markdown path has a segment ending in a space or dot")
    if any(part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES for part in raw_parts):
        raise CoworkPathError("Markdown path uses a reserved device name")

    normalized_parts = tuple(unicodedata.normalize("NFC", part) for part in raw_parts)
    normalized = "/".join(normalized_parts)
    if Path(normalized_parts[-1]).suffix.casefold() not in _MARKDOWN_SUFFIXES:
        raise CoworkPathError("Co-work documents must use .md or .markdown")
    return normalized, normalized_parts


def _assert_contained(root: Path, candidate: Path) -> None:
    try:
        common = Path(os.path.commonpath((str(root), str(candidate))))
    except ValueError as exc:
        raise CoworkPathError("Markdown path is on a different filesystem root") from exc
    if common != root:
        raise CoworkPathError("Markdown path escapes the selected folder")


def _path_key(normalized: str) -> str:
    # Windows path identity is case-insensitive even when a particular volume
    # happens to preserve case. POSIX identity remains case-sensitive.
    return normalized.casefold() if os.name == "nt" else normalized


def resolve_markdown_path(
    store_or_root: Any,
    relative_path: str,
    *,
    for_create: bool = False,
) -> ResolvedMarkdownPath:
    """Resolve one safe folder-relative Markdown path.

    ``for_create`` permits the final path to be absent but requires every
    existing ancestor to be a real directory. Symlinks and Windows reparse
    points are refused in both modes so a later filesystem change cannot turn
    a previously contained lexical path into an external target. Existence is
    intentionally not enforced for reads: callers use the same resolver for
    typed missing-file diagnostics. Atomic create-if-absent remains the final
    race arbiter for creation.
    """

    root = _folder_root(store_or_root)
    normalized, parts = _validate_relative_path(relative_path)
    lexical = root.joinpath(*parts)
    _assert_contained(root, lexical)

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
            raise CoworkPathError("Markdown path crosses a symlink or reparse point")
        if not is_final and not cursor.is_dir():
            raise CoworkPathError("Markdown path has a non-directory parent")

    # Resolve every existing prefix once more. ``strict=False`` follows any
    # alias that appeared between lstat calls; containment therefore fails
    # closed even during a narrow TOCTOU window. Mutators re-run under lock.
    resolved = lexical.resolve(strict=False)
    _assert_contained(root, resolved)
    return ResolvedMarkdownPath(
        normalized=normalized,
        path=resolved,
        path_key=_path_key(normalized),
    )


__all__ = [
    "CoworkPathError",
    "ResolvedMarkdownPath",
    "resolve_markdown_path",
]
