"""Print read-only native-search and legacy-root detachment receipts."""

from __future__ import annotations

import argparse
import json

from work_buddy.index.cutover_evidence import SEARCH_DOMAINS, certify_search_cutover


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prospective-domain",
        action="append",
        choices=SEARCH_DOMAINS,
        default=[],
        help="Validate and include a configured root before its authority seal.",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            certify_search_cutover(prospective_domains=args.prospective_domain),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
