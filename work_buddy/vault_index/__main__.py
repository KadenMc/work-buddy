"""CLI entry: ``python -m work_buddy.vault_index [--force] [--no-encode]``.

Builds the vault chunk index over the configured vaults AND encodes new chunks
into dense vectors, under the per-index advisory lock. Pass ``--no-encode`` for
the fast SQLite-only path (no model load).
"""
from __future__ import annotations

import argparse
import json

from work_buddy.vault_index.indexer import build_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="work_buddy.vault_index",
        description="Build the vault semantic index (chunk store + dense vectors).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Rebuild every reachable vault from scratch (never touches an offline vault).",
    )
    ap.add_argument(
        "--no-encode",
        action="store_true",
        help="Skip dense encoding — build the SQLite chunk index only (no model load).",
    )
    args = ap.parse_args(argv)
    stats = build_all(force=args.force, encode=not args.no_encode)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
