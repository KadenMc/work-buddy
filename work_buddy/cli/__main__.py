"""Entry point: ``python -m work_buddy.cli``."""

from work_buddy.cli.dispatch import main

if __name__ == "__main__":
    raise SystemExit(main())
