"""Run the bounded pre-seal consolidated Vault refresh."""

from __future__ import annotations

import argparse
import json

from work_buddy.index.cutover_evidence import SEARCH_DOMAINS
from work_buddy.vault_index.cutover import refresh_vault_for_prospective_seal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--domain",
        action="append",
        choices=SEARCH_DOMAINS,
        required=True,
        help="Known domain whose configured legacy root must be detached.",
    )
    args = parser.parse_args()
    print(json.dumps(refresh_vault_for_prospective_seal(args.domain), sort_keys=True))


if __name__ == "__main__":
    main()
