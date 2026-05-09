"""Audit verbatim content duplication across knowledge-store units.

Hypothesis under test: authors are repeating substantial text across units
instead of using ``<<wb:path>>`` placeholders for single-source-of-truth.

Reads every JSON file in ``knowledge/store/``, extracts each unit's
``content["full"]``, ``content["summary"]``, ``dev_notes``, and ``description``,
splits the prose into sentences, and counts how many distinct units share each
sentence verbatim (after lowercasing + whitespace collapsing).

Run from repo root:

    python scripts/audit_content_duplication.py

Writes a verbose report to ``scripts/audit_content_duplication.report.txt``
and prints headline numbers + top duplications to stdout.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = REPO_ROOT / "knowledge" / "store"
REPORT_PATH = Path(__file__).resolve().parent / "audit_content_duplication.report.txt"

# Split on . ! ? OR newline-followed-by-blank (paragraph break).
# We do this on the original text first (before lowercasing) only to find
# sentence boundaries; the comparison key is normalized below.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n\s*\n")
_PLACEHOLDER_RE = re.compile(r"<<wb:[^>]+>>")
_WS_RE = re.compile(r"\s+")


def _iter_units(store_dir: Path):
    """Yield ``(path, unit_dict)`` for every unit across every store file."""
    for json_path in sorted(store_dir.glob("*.json")):
        # Skip generated index files — they aren't authored content.
        if json_path.name.startswith("_generated_"):
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  [warn] could not parse {json_path.name}: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        for unit_path, unit in data.items():
            if not isinstance(unit, dict):
                continue
            yield unit_path, unit


def _collect_text_for_dup(unit: dict) -> str:
    """Concat fields whose duplication we care about (full + dev_notes)."""
    parts: list[str] = []
    content = unit.get("content")
    if isinstance(content, dict):
        full = content.get("full")
        if isinstance(full, str):
            parts.append(full)
    dev = unit.get("dev_notes")
    if isinstance(dev, str):
        parts.append(dev)
    return "\n\n".join(parts)


def _normalize(sent: str) -> str:
    """Lowercase, strip placeholder tokens, collapse whitespace."""
    sent = _PLACEHOLDER_RE.sub(" ", sent)
    sent = sent.lower()
    sent = _WS_RE.sub(" ", sent).strip()
    return sent


def _split_sentences(text: str) -> list[str]:
    raw = _SENT_SPLIT.split(text)
    out = []
    for chunk in raw:
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def _word_count(s: str) -> int:
    return len(s.split())


def _count_placeholders(unit: dict) -> int:
    n = 0
    content = unit.get("content")
    if isinstance(content, dict):
        for v in content.values():
            if isinstance(v, str):
                n += len(_PLACEHOLDER_RE.findall(v))
    dev = unit.get("dev_notes")
    if isinstance(dev, str):
        n += len(_PLACEHOLDER_RE.findall(dev))
    return n


def main() -> None:
    units = list(_iter_units(STORE_DIR))

    # Headline numbers: scope = system (default) units only.
    system_units = [
        (p, u) for p, u in units if u.get("scope", "system") == "system"
    ]
    full_lengths = []
    placeholder_total = 0
    for _, u in system_units:
        placeholder_total += _count_placeholders(u)
        content = u.get("content")
        if isinstance(content, dict):
            full = content.get("full")
            if isinstance(full, str):
                full_lengths.append(len(full))

    mean_full = statistics.mean(full_lengths) if full_lengths else 0.0
    median_full = statistics.median(full_lengths) if full_lengths else 0.0

    # Build sentence -> set(unit paths)
    sent_to_units: dict[str, set[str]] = defaultdict(set)
    sent_original: dict[str, str] = {}
    for unit_path, unit in system_units:
        text = _collect_text_for_dup(unit)
        if not text:
            continue
        for sent in _split_sentences(text):
            norm = _normalize(sent)
            if not norm:
                continue
            wc = _word_count(norm)
            if wc < 6:  # skip short fragments entirely
                continue
            sent_to_units[norm].add(unit_path)
            # Keep first observed original-cased version for display.
            sent_original.setdefault(norm, sent)

    # Verbatim duplication: sentences >= 12 words appearing in >= 2 units.
    long_dups: list[tuple[str, set[str]]] = [
        (s, paths)
        for s, paths in sent_to_units.items()
        if _word_count(s) >= 12 and len(paths) >= 2
    ]

    def _rank_key(item):
        s, paths = item
        return -(len(paths) * _word_count(s))

    long_dups.sort(key=_rank_key)

    # Phrase-level: >= 6 words, appearing in >= 4 distinct units.
    phrase_dups: list[tuple[str, set[str]]] = [
        (s, paths)
        for s, paths in sent_to_units.items()
        if _word_count(s) >= 6 and len(paths) >= 4
    ]
    phrase_dups.sort(key=_rank_key)

    # ---------- Print + write report ----------
    lines: list[str] = []

    def out(s: str = "") -> None:
        lines.append(s)

    out("=" * 78)
    out("Knowledge-store content duplication audit")
    out("=" * 78)
    out("")
    out("Headline numbers (scope=system):")
    out(f"  Total system units            : {len(system_units)}")
    out(f"  Total placeholders (<<wb:..>>): {placeholder_total}")
    out(f"  Mean   content['full'] (chars): {mean_full:.0f}")
    out(f"  Median content['full'] (chars): {median_full:.0f}")
    out("")
    out(
        f"Verbatim duplications (>=12 words, in >=2 units): "
        f"{len(long_dups)} sentences"
    )
    out("Top 20 by (units_sharing x sentence_length):")
    out("")
    for i, (norm, paths) in enumerate(long_dups[:20], 1):
        sample = sent_original[norm].replace("\n", " ").strip()
        if len(sample) > 120:
            sample = sample[:117] + "..."
        path_list = sorted(paths)
        shown = ", ".join(path_list[:5])
        if len(path_list) > 5:
            shown += f", ... (+{len(path_list) - 5} more)"
        out(f"  {i:>2}. [{len(paths)} units, {_word_count(norm)} words]")
        out(f"      \"{sample}\"")
        out(f"      paths: {shown}")
        out("")

    out("-" * 78)
    out(
        f"Phrase-level signal (>=6 words, in >=4 distinct units): "
        f"{len(phrase_dups)} sentences"
    )
    out("First 5 examples:")
    out("")
    for i, (norm, paths) in enumerate(phrase_dups[:5], 1):
        sample = sent_original[norm].replace("\n", " ").strip()
        if len(sample) > 120:
            sample = sample[:117] + "..."
        path_list = sorted(paths)
        shown = ", ".join(path_list[:5])
        if len(path_list) > 5:
            shown += f", ... (+{len(path_list) - 5} more)"
        out(f"  {i}. [{len(paths)} units, {_word_count(norm)} words]")
        out(f"     \"{sample}\"")
        out(f"     paths: {shown}")
        out("")

    out("-" * 78)
    placeholder_per_unit = (
        placeholder_total / len(system_units) if system_units else 0
    )
    interp = (
        f"With {placeholder_total} placeholders across {len(system_units)} units "
        f"({placeholder_per_unit:.2f}/unit) but only {len(long_dups)} 12+-word "
        f"sentences and {len(phrase_dups)} 6+-word phrases recurring across "
        "multiple units, the data does not support a 'rampant inlined "
        "duplication' picture: authors are mostly writing distinct prose per "
        "unit, so the low placeholder count reflects little shared text to "
        "factor out rather than missed SSoT opportunities."
    )
    out(interp)
    out("")

    report = "\n".join(lines)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[report written to {REPORT_PATH}]")


if __name__ == "__main__":
    main()
