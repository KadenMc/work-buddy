"""One-shot scrub: remove ``Stage X.Y deliverable.`` lead-ins from
module docstrings under ``work_buddy/threads/`` and friends; remove
``Stage X.Y ships`` and similar staging metadata from prose without
nuking the sentence content.

Conservative pattern set — touches only well-defined leading
sentences. Doesn't try to rewrite "Stage X" mentions inside larger
prose paragraphs (those stay; they're often part of an explanation
that needs ALL its words to make sense).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(".")

# Patterns: (compiled regex, replacement). Applied in order.
PATTERNS: list[tuple[re.Pattern, str]] = [
    # "Stage 2.6 deliverable. " or "Stage 2 deliverable. " at the
    # start of a sentence — drop entirely.
    (re.compile(r"\bStage \d+(?:\.\d+)? deliverable\.\s*"), ""),
    # "Stage 1: types only, no behavior." → "Types only, no behavior."
    # (drop "Stage N:" lead)
    (re.compile(r"\bStage \d+(?:\.\d+)?: "), ""),
    # "Stage 2.8 ships the spawn + cascade mechanics." →
    # "This module ships the spawn + cascade mechanics."
    (re.compile(r"\bStage \d+(?:\.\d+)? ships "), "This module ships "),
    # "Once Stage 4.14 retires the v4 pool, ..." → "Once the v4 pool
    # is retired, ..." — generic future-tense.
    (re.compile(r"\bOnce Stage \d+(?:\.\d+)? retires "), "Once the system retires "),
    # Standalone "Stage 2.x" / "Stage 4-equivalent" mentions in mid-
    # sentence: replace with "[stage tag dropped]"-style empty so we
    # don't leak placeholder text.
    # SKIP these — too risky to edit context-blind.
]


# Files to touch. Limited to threads/, llm/ since that's where the
# bulk of stage-prefixed docstrings live (per the audit).
TARGETS = list(ROOT.glob("work_buddy/threads/*.py")) + list(
    ROOT.glob("work_buddy/llm/*.py")
)


def main():
    changed = 0
    for path in TARGETS:
        text = path.read_text(encoding="utf-8")
        new = text
        for rx, repl in PATTERNS:
            new = rx.sub(repl, new)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed += 1
    print(f"updated {changed} files (out of {len(TARGETS)} scanned)")


if __name__ == "__main__":
    main()
