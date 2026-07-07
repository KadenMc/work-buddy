"""Single source of truth for the work-buddy version: pyproject.toml.

The installer version, the git release tag, and the package version must agree.
Everything derives the version from here instead of hardcoding it, and the release
CI verifies the tag matches. Usage: python packaging/version.py [--root <repo>]
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path


def read_version(root: Path | None = None) -> str:
    """Return the version string from ``pyproject.toml`` under ``root``."""
    root = root or Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print the work-buddy version from pyproject.toml.")
    ap.add_argument("--root", default=None, help="repo root (default: two levels up)")
    args = ap.parse_args(argv)
    print(read_version(Path(args.root) if args.root else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
