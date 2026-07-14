"""Machine checks for the durable K0 invariant coverage map."""

from __future__ import annotations

import ast
import re
from pathlib import Path


MAP_PATH = Path(__file__).with_name("INVARIANT_COVERAGE.md")
REPO_ROOT = Path(__file__).parents[3]
EXPECTED_KEYS = frozenset(
    {
        "canonical-claim-hash",
        "append-only-triggers",
        "status-machine",
        "confirmation-gate",
        "weakest-link",
        "supersession-closure",
        "single-confirmed-successor",
        "dedup-by-canonical-hash",
        "redaction-co-status",
        "blob-refcounting",
        "fingerprint-scope",
        "trust-class-laws",
        "producer-identity",
        "gesture-binding-use-freshness-context",
        "locator-verifiability",
        "reason-classed-rejection",
        "three-clocks",
    }
)
REFERENCE_RE = re.compile(
    r"`(?P<path>tests/unit/truth/[a-z0-9_]+\.py)::(?P<test>test_[a-z0-9_]+)`"
)


def _coverage_rows() -> dict[str, tuple[tuple[str, str], ...]]:
    rows: dict[str, tuple[tuple[str, str], ...]] = {}
    for line in MAP_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        key = cells[0].strip("`")
        references = tuple(
            (match.group("path"), match.group("test"))
            for match in REFERENCE_RE.finditer(cells[2])
        )
        assert key not in rows, f"duplicate invariant coverage key {key!r}"
        rows[key] = references
    return rows


def _test_functions(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def test_every_folded_invariant_maps_to_existing_named_tests() -> None:
    rows = _coverage_rows()
    assert frozenset(rows) == EXPECTED_KEYS

    for key, references in rows.items():
        assert references, f"invariant {key!r} has no named test"
        for relative_path, test_name in references:
            path = REPO_ROOT / relative_path
            assert path.is_file(), f"coverage target does not exist: {relative_path}"
            assert test_name in _test_functions(path), (
                f"coverage target does not exist: {relative_path}::{test_name}"
            )
