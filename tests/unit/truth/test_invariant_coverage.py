"""Machine checks for the durable truth-layer design contract inventory."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


MAP_PATH = Path(__file__).with_name("INVARIANT_COVERAGE.md")
REPO_ROOT = Path(__file__).parents[3]
REFERENCE_RE = re.compile(
    r"`(?P<path>tests/unit/truth/[a-z0-9_]+\.py)::(?P<test>test_[a-z0-9_]+)`"
)


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """One independently inventoried durable design commitment."""

    design_ref: str
    stage: str


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One parsed human-readable coverage-map row."""

    design_ref: str
    stage: str
    references: tuple[tuple[str, str], ...]


DURABLE_CONTRACT_INVENTORY = {
    "canonical-claim-hash": ContractSpec("tms-glue.md II.5 canonical claim hash", "K0"),
    "append-only-triggers": ContractSpec("tms-glue.md II.5 append-only schema", "K0"),
    "status-machine": ContractSpec("tms-glue.md II.5 status machine", "K0"),
    "confirmation-gate": ContractSpec("tms-glue.md II.5 confirmation gate", "K0"),
    "weakest-link": ContractSpec("tms-glue.md II.5 weakest-link confirmation", "K0"),
    "supersession-closure": ContractSpec(
        "tms-glue.md II.5 supersession semantics", "K0"
    ),
    "single-confirmed-successor": ContractSpec(
        "tms-glue.md II.5 single-confirmed-successor", "K0"
    ),
    "dedup-by-canonical-hash": ContractSpec(
        "tms-glue.md II.5 canonical-hash dedup", "K0"
    ),
    "redaction-co-status": ContractSpec("tms-glue.md II.5 redaction co-status", "K0"),
    "blob-refcounting": ContractSpec("tms-glue.md II.5 redaction blob lifecycle", "K0"),
    "fingerprint-scope": ContractSpec("tms-glue.md II.5 fingerprint scope", "K0"),
    "trust-class-laws": ContractSpec("tms-glue.md II.5 trust assignment laws", "K0"),
    "producer-identity": ContractSpec("tms-glue.md II.5 producer identity", "K0"),
    "gesture-binding-use-freshness-context": ContractSpec(
        "tms-glue.md II.5 gesture binding", "K0"
    ),
    "locator-verifiability": ContractSpec(
        "tms-glue.md II.4 locator verifiability matrix", "K0"
    ),
    "reason-classed-rejection": ContractSpec(
        "tms-glue.md II.5 reason-classed rejection", "K0"
    ),
    "three-clocks": ContractSpec("tms-glue.md II.5 three clocks", "K0"),
    "claims-current-rebuild": ContractSpec(
        "tms-glue.md II.5 rebuildable claims_current projection", "K0"
    ),
    "migration-versioning-open-refusal": ContractSpec(
        "tms-glue.md II.5 migration contract 1", "K0"
    ),
    "migration-pre-bump-snapshot": ContractSpec(
        "tms-glue.md II.5 migration contract 2", "K0"
    ),
    "migration-additive-history": ContractSpec(
        "tms-glue.md II.5 migration contract 3", "K0"
    ),
    "migration-frozen-released-stores": ContractSpec(
        "tms-glue.md II.5 migration contract 4", "K0"
    ),
    "migration-jsonl-escape": ContractSpec(
        "tms-glue.md II.5 migration contract 5", "K0"
    ),
    "migration-document-regeneration": ContractSpec(
        "tms-glue.md II.5 migration contract 6",
        "K0 foundation; K2 remainder deferred",
    ),
    "migration-profile-history": ContractSpec(
        "tms-glue.md II.5 migration contract 7", "K0"
    ),
    "migration-permanent-identity": ContractSpec(
        "tms-glue.md II.5 migration contract 8", "K0"
    ),
    "refutation-reentry": ContractSpec(
        "tms-glue.md II.10 refutation re-entry", "Deferred K3/K4"
    ),
}


def _coverage_rows() -> dict[str, CoverageRow]:
    rows: dict[str, CoverageRow] = {}
    for line in MAP_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 5, f"malformed invariant coverage row: {line}"
        key = cells[0].strip("`")
        references = tuple(
            (match.group("path"), match.group("test"))
            for match in REFERENCE_RE.finditer(cells[4])
        )
        assert key not in rows, f"duplicate invariant coverage key {key!r}"
        rows[key] = CoverageRow(
            design_ref=cells[1].strip("`"),
            stage=cells[2],
            references=references,
        )
    return rows


def _test_functions(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def test_map_matches_complete_durable_design_inventory() -> None:
    rows = _coverage_rows()
    assert len(DURABLE_CONTRACT_INVENTORY) == 27
    assert set(rows) == set(DURABLE_CONTRACT_INVENTORY)
    assert {
        spec.design_ref
        for spec in DURABLE_CONTRACT_INVENTORY.values()
        if spec.design_ref.startswith("tms-glue.md II.5 migration contract ")
    } == {f"tms-glue.md II.5 migration contract {number}" for number in range(1, 9)}

    for key, specification in DURABLE_CONTRACT_INVENTORY.items():
        row = rows[key]
        assert row.design_ref == specification.design_ref
        assert row.stage == specification.stage


def test_every_k0_contract_maps_to_existing_named_tests() -> None:
    rows = _coverage_rows()
    for key, specification in DURABLE_CONTRACT_INVENTORY.items():
        references = rows[key].references
        if specification.stage == "Deferred K3/K4":
            assert not references, f"deferred contract {key!r} claims K0 tests"
            continue
        assert references, f"K0 contract {key!r} has no named test"
        for relative_path, test_name in references:
            path = REPO_ROOT / relative_path
            assert path.is_file(), f"coverage target does not exist: {relative_path}"
            assert test_name in _test_functions(path), (
                f"coverage target does not exist: {relative_path}::{test_name}"
            )
