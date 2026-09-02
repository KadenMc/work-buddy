"""Checkpoint configured search-cutover databases before immutable evidence."""

from __future__ import annotations

import argparse
import json

from work_buddy.index.cutover_checkpoint import checkpoint_search_cutover_databases
from work_buddy.index.cutover_evidence import SEARCH_DOMAINS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--domain",
        action="append",
        choices=SEARCH_DOMAINS,
        default=[],
        help=(
            "Checkpoint a configured native domain database. Repeat as needed; "
            "omitting the flag checkpoints all four domains."
        ),
    )
    args = parser.parse_args()
    domains = args.domain or list(SEARCH_DOMAINS)
    print(
        json.dumps(
            checkpoint_search_cutover_databases(domains=domains),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
