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
    scope: str


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One parsed human-readable coverage-map row."""

    design_ref: str
    scope: str
    references: tuple[tuple[str, str], ...]


ENGINE_CORE = "engine core"
RESIDENT_RENDERING_SCOPE = "engine core, resident rendering outside engine scope"
RETRIEVAL_INTEGRATION = "retrieval integration"


DURABLE_CONTRACT_INVENTORY = {
    "canonical-claim-hash": ContractSpec(
        "tms-glue.md II.5 canonical claim hash", ENGINE_CORE
    ),
    "append-only-triggers": ContractSpec(
        "tms-glue.md II.5 append-only schema", ENGINE_CORE
    ),
    "status-machine": ContractSpec("tms-glue.md II.5 status machine", ENGINE_CORE),
    "confirmation-gate": ContractSpec(
        "tms-glue.md II.5 confirmation gate", ENGINE_CORE
    ),
    "weakest-link": ContractSpec(
        "tms-glue.md II.5 weakest-link confirmation", ENGINE_CORE
    ),
    "supersession-closure": ContractSpec(
        "tms-glue.md II.5 supersession semantics", ENGINE_CORE
    ),
    "single-confirmed-successor": ContractSpec(
        "tms-glue.md II.5 single-confirmed-successor", ENGINE_CORE
    ),
    "dedup-by-canonical-hash": ContractSpec(
        "tms-glue.md II.5 canonical-hash dedup", ENGINE_CORE
    ),
    "redaction-co-status": ContractSpec(
        "tms-glue.md II.5 redaction co-status", ENGINE_CORE
    ),
    "blob-refcounting": ContractSpec(
        "tms-glue.md II.5 redaction blob lifecycle", ENGINE_CORE
    ),
    "fingerprint-scope": ContractSpec(
        "tms-glue.md II.5 fingerprint scope", ENGINE_CORE
    ),
    "trust-class-laws": ContractSpec(
        "tms-glue.md II.5 trust assignment laws", ENGINE_CORE
    ),
    "producer-identity": ContractSpec(
        "tms-glue.md II.5 producer identity", ENGINE_CORE
    ),
    "gesture-binding-use-freshness-context": ContractSpec(
        "tms-glue.md II.5 gesture binding", ENGINE_CORE
    ),
    "locator-verifiability": ContractSpec(
        "tms-glue.md II.4 locator verifiability matrix", ENGINE_CORE
    ),
    "reason-classed-rejection": ContractSpec(
        "tms-glue.md II.5 reason-classed rejection", ENGINE_CORE
    ),
    "three-clocks": ContractSpec("tms-glue.md II.5 three clocks", ENGINE_CORE),
    "claims-current-rebuild": ContractSpec(
        "tms-glue.md II.5 rebuildable claims_current projection", ENGINE_CORE
    ),
    "migration-versioning-open-refusal": ContractSpec(
        "tms-glue.md II.5 migration contract 1", ENGINE_CORE
    ),
    "migration-pre-bump-snapshot": ContractSpec(
        "tms-glue.md II.5 migration contract 2", ENGINE_CORE
    ),
    "migration-additive-history": ContractSpec(
        "tms-glue.md II.5 migration contract 3", ENGINE_CORE
    ),
    "migration-frozen-released-stores": ContractSpec(
        "tms-glue.md II.5 migration contract 4", ENGINE_CORE
    ),
    "migration-jsonl-escape": ContractSpec(
        "tms-glue.md II.5 migration contract 5", ENGINE_CORE
    ),
    "migration-document-regeneration": ContractSpec(
        "tms-glue.md II.5 migration contract 6",
        RESIDENT_RENDERING_SCOPE,
    ),
    "migration-profile-history": ContractSpec(
        "tms-glue.md II.5 migration contract 7", ENGINE_CORE
    ),
    "migration-permanent-identity": ContractSpec(
        "tms-glue.md II.5 migration contract 8", ENGINE_CORE
    ),
    "refutation-reentry": ContractSpec(
        "tms-glue.md II.10 refutation re-entry", RETRIEVAL_INTEGRATION
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
            scope=cells[2],
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
        assert row.scope == specification.scope


def test_every_engine_contract_maps_to_existing_named_tests() -> None:
    rows = _coverage_rows()
    for key, specification in DURABLE_CONTRACT_INVENTORY.items():
        references = rows[key].references
        if specification.scope == RETRIEVAL_INTEGRATION:
            assert not references, (
                f"retrieval integration contract {key!r} claims engine tests"
            )
            continue
        assert references, f"engine contract {key!r} has no named test"
        for relative_path, test_name in references:
            path = REPO_ROOT / relative_path
            assert path.is_file(), f"coverage target does not exist: {relative_path}"
            assert test_name in _test_functions(path), (
                f"coverage target does not exist: {relative_path}::{test_name}"
            )
