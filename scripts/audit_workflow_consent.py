"""Audit workflow consent posture.

Enumerates every workflow in the registry and reports its consent
fingerprint: declared ``consent_operations`` across constituent
capabilities, inferred consent weight (max risk over those ops), and a
recommendation for whether the workflow needs an explicit
``consent_weight`` declaration on its capabilities.

Run via::

    conda run -n work-buddy python -m scripts.audit_workflow_consent

Or, with the env's python directly::

    .../envs/work-buddy/python.exe -m scripts.audit_workflow_consent

Used to prioritize which workflows to convert (explicit
``consent_weight`` on their high-risk capabilities) versus which ride
the low-weight auto-bypass.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict


_RISK_ORDER = {"low": 0, "moderate": 1, "high": 2}


def _inferred_weight(workflow_name: str) -> dict:
    """Walk a workflow's DAG and compute its inferred consent weight.

    Returns a dict with: ``workflow_name``, ``declared_ops`` (list),
    ``max_risk``, ``invokes`` (sorted set of capability names), and
    ``status`` (``"safe_default"`` / ``"needs_declaration"`` /
    ``"requires_prompt"``).
    """
    from work_buddy.mcp_server import registry
    from work_buddy.consent import get_consent_metadata

    entry = registry.get_entry(workflow_name)
    if not isinstance(entry, registry.WorkflowDefinition):
        return {
            "workflow_name": workflow_name,
            "status": "not_a_workflow",
        }

    invokes: set[str] = set()
    for step in entry.steps:
        for cap_name in step.invokes:
            invokes.add(cap_name)

    declared_ops: list[str] = []
    seen: set[str] = set()
    max_risk = "low"
    for cap_name in sorted(invokes):
        cap_entry = registry.get_entry(cap_name)
        if not isinstance(cap_entry, registry.Capability):
            continue
        for op in cap_entry.consent_operations:
            if op in seen:
                continue
            seen.add(op)
            declared_ops.append(op)
            meta = get_consent_metadata(op) or {}
            op_risk = meta.get("risk", "moderate")
            if _RISK_ORDER.get(op_risk, 0) > _RISK_ORDER.get(max_risk, 0):
                max_risk = op_risk

    if not declared_ops or max_risk == "low":
        status = "safe_default"  # Auto-bypasses the workflow consent prompt.
    elif max_risk == "high":
        status = "requires_prompt"
    else:
        status = "requires_prompt"

    return {
        "workflow_name": workflow_name,
        "declared_ops": declared_ops,
        "max_risk": max_risk,
        "invokes": sorted(invokes),
        "step_count": len(entry.steps),
        "status": status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit composable-consent posture of every workflow.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of the human-readable table.",
    )
    parser.add_argument(
        "--name", default=None,
        help="Audit a single workflow by name (for spot-checking).",
    )
    args = parser.parse_args(argv)

    from work_buddy.mcp_server import registry as reg

    if args.name:
        records = [_inferred_weight(args.name)]
    else:
        names = sorted(
            n for n, e in reg.get_registry().items()
            if isinstance(e, reg.WorkflowDefinition)
        )
        records = [_inferred_weight(n) for n in names]

    if args.json:
        print(json.dumps(records, indent=2))
        return 0

    # Human-readable summary.
    by_status: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_status[r.get("status", "unknown")].append(r)

    print()
    print("=" * 70)
    print("Workflow consent posture audit")
    print("=" * 70)
    for status in ("requires_prompt", "needs_declaration", "safe_default"):
        rs = by_status.get(status, [])
        if not rs:
            continue
        print()
        print(f"## {status} ({len(rs)} workflow{'s' if len(rs) != 1 else ''})")
        for r in rs:
            print(f"  - {r['workflow_name']}  "
                  f"[max_risk={r.get('max_risk', '—')}]  "
                  f"[{r.get('step_count', '?')} steps]")
            for op in r.get("declared_ops", []):
                print(f"      * {op}")

    print()
    print(f"Total: {len(records)} workflows")
    print(f"  safe_default (low-weight auto-bypass): "
          f"{len(by_status.get('safe_default', []))}")
    print(f"  requires_prompt: "
          f"{len(by_status.get('requires_prompt', []))}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
