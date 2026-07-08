"""Validation command for the unified knowledge store.

Runs structural integrity checks on the store: DAG validity,
command-to-store mappings, required fields, kind-specific fields,
and thinned command format. Returns a structured report.

Usage:
    python -m work_buddy.knowledge.validate
"""

from __future__ import annotations

import re
from typing import Any

from work_buddy.knowledge.model import (
    CapabilityUnit,
    DirectionsUnit,
    PromptUnit,
    SystemUnit,
    WorkflowUnit,
    validate_dag,
)
from work_buddy.knowledge.store import load_store
from work_buddy.logging_config import get_logger
from work_buddy import paths

logger = get_logger(__name__)

_SLASH_CMD_DIR = paths.asset_root() / ".claude" / "commands"
_MAX_THIN_LINES = 7


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_dag(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 1: DAG integrity — parent/child referential integrity + cycles."""
    raw_errors = validate_dag(store)
    return [{"check": "dag_integrity", "path": "", "message": e} for e in raw_errors]


def _check_command_mapping(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 2: Every wb-*.md slash command should have a DirectionsUnit with matching command field."""
    errors: list[dict[str, str]] = []

    if not _SLASH_CMD_DIR.is_dir():
        return errors

    # Build reverse map: command name → store path
    command_to_path: dict[str, str] = {}
    for path, unit in store.items():
        cmd = None
        if isinstance(unit, DirectionsUnit) and unit.command:
            cmd = unit.command
        elif isinstance(unit, WorkflowUnit) and unit.command:
            cmd = unit.command
        if cmd:
            command_to_path[cmd] = path

    for cmd_file in sorted(_SLASH_CMD_DIR.glob("wb-*.md")):
        cmd_name = cmd_file.stem  # e.g. "wb-task-triage"
        if cmd_name not in command_to_path:
            errors.append({
                "check": "command_mapping",
                "path": cmd_name,
                "message": f"Slash command '{cmd_name}' has no matching unit with command={cmd_name!r}",
            })

    return errors


def _check_thinned_commands(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 3: Every wb-*.md command file should be thinned (< N lines)."""
    errors: list[dict[str, str]] = []

    if not _SLASH_CMD_DIR.is_dir():
        return errors

    for cmd_file in sorted(_SLASH_CMD_DIR.glob("wb-*.md")):
        lines = cmd_file.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_THIN_LINES:
            errors.append({
                "check": "thinned_commands",
                "path": cmd_file.stem,
                "message": f"Command file has {len(lines)} lines (max {_MAX_THIN_LINES}). Needs thinning.",
            })

    return errors


def _check_store_path_in_commands() -> list[dict[str, str]]:
    """Check 4: Store paths referenced in thinned commands should exist in the store."""
    errors: list[dict[str, str]] = []
    store = load_store()

    if not _SLASH_CMD_DIR.is_dir():
        return errors

    # Pattern: "path": "some/path" in agent_docs calls
    path_pattern = re.compile(r'"path"\s*:\s*"([^"]+)"')

    for cmd_file in sorted(_SLASH_CMD_DIR.glob("wb-*.md")):
        text = cmd_file.read_text(encoding="utf-8")
        for match in path_pattern.finditer(text):
            ref_path = match.group(1)
            if ref_path not in store:
                errors.append({
                    "check": "store_path_validity",
                    "path": cmd_file.stem,
                    "message": f"References store path '{ref_path}' which does not exist",
                })

    return errors


def _check_required_fields(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 5: Required fields on all units."""
    errors: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        if not unit.name:
            errors.append({
                "check": "required_fields",
                "path": path,
                "message": "Missing 'name'",
            })
        if not unit.description:
            errors.append({
                "check": "required_fields",
                "path": path,
                "message": "Missing 'description'",
            })

    return errors


def _check_directions_fields(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 5b: DirectionsUnit-specific required fields."""
    errors: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        if not isinstance(unit, DirectionsUnit):
            continue
        if not unit.trigger:
            errors.append({
                "check": "directions_fields",
                "path": path,
                "message": "DirectionsUnit missing 'trigger'",
            })
        if not unit.content.get("full"):
            errors.append({
                "check": "directions_fields",
                "path": path,
                "message": "DirectionsUnit missing content.full",
            })

    return errors


def _check_kind_specific_fields(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 6: Kind-specific required fields."""
    errors: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        if isinstance(unit, CapabilityUnit):
            if not unit.capability_name:
                errors.append({
                    "check": "kind_fields",
                    "path": path,
                    "message": "CapabilityUnit missing 'capability_name'",
                })
        elif isinstance(unit, WorkflowUnit):
            if not unit.workflow_name:
                errors.append({
                    "check": "kind_fields",
                    "path": path,
                    "message": "WorkflowUnit missing 'workflow_name'",
                })

    return errors


def _check_placeholder_duplicates(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 8: No placeholder target may appear more than once in a
    single unit's ``content["full"]``.

    Duplicate placeholders are a hard error, not a hint: the runtime
    per-unit-occurrence cap renders subsequent references as
    back-reference markers, so duplicates produce zero new content.
    They're never the right authorial choice. The editor rejects them
    before disk; this corpus-wide check catches any that slipped in
    via direct JSON edits, programmatic generators, or pre-rule legacy
    content.
    """
    # Imported here to avoid circular import — editor imports from this
    # module's siblings, and check_duplicate_placeholders is the
    # canonical implementation.
    from work_buddy.knowledge.editor import check_duplicate_placeholders

    errors: list[dict[str, str]] = []
    for path, unit in sorted(store.items()):
        full = unit.content.get("full", "")
        if not isinstance(full, str) or "<<wb:" not in full:
            continue
        duplicates = check_duplicate_placeholders(full)
        for dup in duplicates:
            errors.append({
                "check": "placeholder_duplicate",
                "path": path,
                "message": (
                    f"Placeholder for {dup['placeholder']!r} appears "
                    f"{dup['count']} times — duplicates render as "
                    "back-reference markers at read time; remove the extras."
                ),
            })
    return errors


def _check_capability_op_resolution(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 9: declaration-based capabilities resolve to a registered op.

    Only capability units carrying an ``op`` field are declaration-based;
    generated capability units have no ``op`` and are ignored. Emits
    *warnings* (not errors): the direct and declaration-based capability
    registration paths coexist, so an unresolved declaration is surfaced
    without failing the whole store. The loader is the single source of this
    logic; this check surfaces its findings corpus-wide.
    """
    has_declarations = any(
        isinstance(u, CapabilityUnit) and getattr(u, "op", "")
        for u in store.values()
    )
    if not has_declarations:
        return []

    from work_buddy.knowledge.capability_loader import load_declared_capabilities

    _caps, issues = load_declared_capabilities(store)
    return issues


# ---------------------------------------------------------------------------
# Durable-surfaces content audit (advisory)
# ---------------------------------------------------------------------------

# Transient-identifier patterns forbidden in durable surfaces (the rule
# lives at ``dev/durable-surfaces``). The pattern set was mined from the
# repo's own design corpus and validated against the live store for
# false-positive rate. Notable exclusions, chosen on measured evidence:
# bare ``vN`` version labels (hundreds of legitimate schema/interface
# uses), bare hex strings (collide with example ids and numerals), and
# ``Step N`` / broad lifecycle words like "deferred" (real workflow and
# planning vocabulary). Findings are warnings, not errors: several
# categories (tier references, example task ids) are acknowledged debt
# that clears in scheduled cleanup passes, and the warning list doubles
# as that backlog's inventory.
#
# Public: ``work_buddy.dev.commit.transient_check`` (the diff-scoped
# commit gate) consumes the same table so the two surfaces never drift.
TRANSIENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("stage_label", re.compile(
        r"\b(?:slice|phase|stage|milestone|wave|tier)[\s-]+\d+(?:\.\d+)?[a-z]?\b",
        re.IGNORECASE)),
    ("date", re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")),
    ("task_ref", re.compile(r"\bt-[0-9a-f]{8}\b")),
    ("pr_ref", re.compile(r"(?:\bPR\s*)?#\d{2,5}\b")),
    ("branch_ref", re.compile(r"\b(?:feat|fix|chore|docs|refactor)/[a-z0-9][\w./-]*")),
    ("commit_ref", re.compile(r"\bcommit\s+[0-9a-f]{7,40}\b", re.IGNORECASE)),
    ("archaeology_ident", re.compile(r"\b(?:legacy|deprecated)_[a-z]\w*")),
    ("session_tag", re.compile(r"\bAFK\b|\bovernight\s+(?:build|run|batch)\b")),
    ("migration_phrase", re.compile(
        r"ships?\s+inert|shipped\s+inert|staged\s+to\s+replace|\bfor\s+now\b"
        r"|the\s+new\s+approach|after\s+the\s+[\w\s-]{1,30}\s+migration"
        r"|\bpost-migration\b|retirement\s+deferred|temporary\s+workaround",
        re.IGNORECASE)),
]

# Units whose subject matter legitimately contains these patterns opt out
# with this tag: the durable-surfaces rule itself (quotes forbidden
# examples), and units documenting a real numbered interface (the
# summarization funnel's stage-1/stage-2 retrieval contract, the
# live-testing four-phase template).
_TRANSIENT_EXEMPT_TAG = "allow-transient-labels"


def _check_durable_surfaces(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 15: advisory scan for transient identifiers in durable surfaces.

    Scans every unit's prose fields (name, description, tags, summary,
    full content, dev_notes, plus capability parameter schemas and
    workflow step instructions) for stage labels, dates, VCS references,
    and migration-narrative phrasing. Emits *warnings* only. This is the
    repo-wide backstop the diff-scoped commit checks structurally cannot
    provide: archaeology in units no commit touches is invisible to a
    diff scan but shows up here on every run.
    """
    errors: list[dict[str, str]] = []
    for path, unit in sorted(store.items()):
        if _TRANSIENT_EXEMPT_TAG in (unit.tags or []):
            continue

        fields: list[tuple[str, str]] = [
            ("name", unit.name or ""),
            ("description", unit.description or ""),
            ("tags", " ".join(unit.tags or [])),
            ("summary", unit.content.get("summary", "") or ""),
            ("full", unit.content.get("full", "") or ""),
            ("dev_notes", unit.dev_notes or ""),
        ]
        # Kind-specific prose containers: capability parameter schemas
        # and workflow step instructions both carry user-facing text.
        for attr in ("parameters", "steps", "trigger"):
            value = getattr(unit, attr, None)
            if value:
                fields.append((attr, str(value)))

        per_category: dict[str, list[str]] = {}
        for field_name, text in fields:
            if not text:
                continue
            for category, pattern in TRANSIENT_PATTERNS:
                for match in pattern.finditer(text):
                    token = f"{match.group(0)!r} ({field_name})"
                    bucket = per_category.setdefault(category, [])
                    if token not in bucket:
                        bucket.append(token)

        for category, matches in sorted(per_category.items()):
            shown = ", ".join(matches[:5])
            extra = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
            errors.append({
                "check": "durable_surfaces",
                "path": path,
                "message": (
                    f"transient {category}: {shown}{extra}. Reword to stable "
                    f"domain terms describing current behavior, or tag the "
                    f"unit '{_TRANSIENT_EXEMPT_TAG}' if this names a real "
                    f"documented interface."
                ),
                "severity": "warning",
            })
    return errors


def _check_parent_child_symmetry(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 7: If A lists B as child, B should list A as parent (and vice versa)."""
    errors: list[dict[str, str]] = []

    for path, unit in sorted(store.items()):
        for child in unit.children:
            child_unit = store.get(child)
            if child_unit and path not in child_unit.parents:
                errors.append({
                    "check": "parent_child_symmetry",
                    "path": path,
                    "message": f"Lists '{child}' as child, but child doesn't list '{path}' as parent",
                })
        for parent in unit.parents:
            parent_unit = store.get(parent)
            if parent_unit and path not in parent_unit.children:
                errors.append({
                    "check": "parent_child_symmetry",
                    "path": path,
                    "message": f"Lists '{parent}' as parent, but parent doesn't list '{path}' as child",
                })

    return errors


def _check_workflow_step_dag(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 10: each workflow unit's internal ``steps`` DAG is well-formed —
    no duplicate step ids, every ``depends_on`` names an existing step, and
    no cycles.

    The conductor builds this DAG and raises on cycles / dangling deps, but
    only when a workflow *runs* (``work_buddy/workflow.py`` ``add_task``). This
    surfaces the same failures at author / commit time, before a broken steps
    DAG ever ships. Degrades gracefully (skips cycle detection) when networkx
    is unavailable, mirroring ``validate_dag``.
    """
    errors: list[dict[str, str]] = []
    try:
        import networkx as nx
    except ImportError:
        nx = None  # type: ignore[assignment]

    for path, unit in sorted(store.items()):
        if not isinstance(unit, WorkflowUnit):
            continue
        steps = unit.steps or []

        ids: list[str] = [
            s["id"] for s in steps if isinstance(s, dict) and s.get("id")
        ]
        seen: set[str] = set()
        for sid in ids:
            if sid in seen:
                errors.append({
                    "check": "workflow_step_dag",
                    "path": path,
                    "message": f"duplicate step id {sid!r}",
                })
            seen.add(sid)
        step_ids = set(ids)

        g = nx.DiGraph() if nx is not None else None
        if g is not None:
            for sid in step_ids:
                g.add_node(sid)

        for s in steps:
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            for dep in s.get("depends_on") or []:
                if dep not in step_ids:
                    errors.append({
                        "check": "workflow_step_dag",
                        "path": path,
                        "message": f"step {sid!r} depends_on unknown step {dep!r}",
                    })
                elif g is not None and sid:
                    g.add_edge(dep, sid)

        if g is not None and step_ids and not nx.is_directed_acyclic_graph(g):
            try:
                cyc = nx.find_cycle(g)
                chain = " -> ".join(a for a, _ in cyc) + f" -> {cyc[-1][1]}"
            except Exception:
                chain = "cycle present"
            errors.append({
                "check": "workflow_step_dag",
                "path": path,
                "message": f"step DAG has a cycle: {chain}",
            })

    return errors


def _check_workflow_step_consistency(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 11: workflow ``steps`` ↔ ``step_instructions`` consistency.

    - An orphan ``step_instructions`` key (no matching step id) is dead text
      that will drift into ``content.full`` on the next codec round-trip —
      flagged as an error.
    - A ``reasoning`` step with no instructions is flagged as a non-blocking
      warning — *unless* the workflow has a bound directions unit. By the house
      convention a reasoning step's behavioral prose lives in the directions
      unit whose ``workflow`` field targets this workflow (the single source),
      not the step body; those steps are intentionally instruction-less and
      must not warn. A workflow with no bound directions unit and a bare
      reasoning step is the real defect this check exists to surface: either
      the instruction was never written, or the step is miscategorized and
      should be a ``code`` step. (See ``architecture/workflows`` for the rule.)

    The suppression is safe because the binding is a runtime delivery contract,
    not merely documentation: the conductor delivers the bound directions unit's
    rendered content to an instruction-less reasoning step on every entry path
    (slash command, nested ``wb_run`` delegation, headless), so a bound bare
    step is never reached blind. ``directions_workflow_resolution`` guards that
    the binding resolves to a real workflow — a dangling binding would both
    silence this warning and leave the conductor nothing to deliver.
    """
    documented_workflows = {
        u.workflow
        for u in store.values()
        if isinstance(u, DirectionsUnit) and u.workflow
    }
    errors: list[dict[str, str]] = []
    for path, unit in sorted(store.items()):
        if not isinstance(unit, WorkflowUnit):
            continue
        step_ids = {
            s["id"] for s in (unit.steps or [])
            if isinstance(s, dict) and s.get("id")
        }
        instructions = unit.step_instructions or {}
        for key in instructions:
            if key not in step_ids:
                errors.append({
                    "check": "workflow_step_consistency",
                    "path": path,
                    "message": (
                        f"step_instructions has orphan key {key!r} — no such "
                        "step; it will drift into content.full on round-trip"
                    ),
                })
        # A bound directions unit is the documented home for this workflow's
        # reasoning-step behavior — its bare reasoning steps are intentional.
        if path in documented_workflows:
            continue
        for s in unit.steps or []:
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            if s.get("step_type") == "reasoning" and sid and sid not in instructions:
                errors.append({
                    "check": "workflow_step_consistency",
                    "path": path,
                    "message": f"reasoning step {sid!r} has no instructions",
                    "severity": "warning",
                })
    return errors


def _check_directions_workflow_resolution(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 13: a directions unit's ``workflow`` field resolves to a real workflow.

    ``DirectionsUnit.workflow`` names the workflow whose behavioral prose the
    directions unit owns — it is the single source the
    ``workflow_step_consistency`` suppression trusts. A value that does not
    resolve to a ``kind: workflow`` unit in the store is a dangling reference:
    the "Linked workflow" link renders to nothing in generated docs, and the
    consistency suppression silently fails to recognise the binding (so the
    bound workflow's bare reasoning steps would wrongly warn). Flagged as an
    error — like ``parent_child_symmetry``, a broken cross-reference is not a
    matter of degree.
    """
    workflow_paths = {
        p for p, u in store.items() if isinstance(u, WorkflowUnit)
    }
    errors: list[dict[str, str]] = []
    for path, unit in sorted(store.items()):
        if not isinstance(unit, DirectionsUnit):
            continue
        target = unit.workflow
        if target and target not in workflow_paths:
            errors.append({
                "check": "directions_workflow_resolution",
                "path": path,
                "message": (
                    f"workflow {target!r} does not resolve to a kind:workflow "
                    "unit in the store — dangling directions→workflow binding"
                ),
            })
    return errors


_WB_RUN_RE = re.compile(r"""wb_run\(\s*["']([A-Za-z0-9_\-]+)["']""")
"""Match a ``wb_run("<name>")`` / ``wb_run('<name>')`` call in prose."""

_WB_RUN_PARAMS_RE = re.compile(
    r"""wb_run\(\s*["']([A-Za-z0-9_\-]+)["']\s*,\s*(\{[^{}]*\})"""
)
"""Match ``wb_run("<name>", {<params>})`` — captures name + a single-level
(non-nested) params literal. Best-effort: multi-line or nested-brace params
are not parsed; the contract check that uses this degrades to a no-op for them."""

_PARAM_KEY_RE = re.compile(r"""["'](\w+)["']\s*:""")
"""Extract top-level keys from a ``{...}`` params literal."""


def _check_workflow_delegation_resolution(store: dict[str, PromptUnit]) -> list[dict[str, str]]:
    """Check 14: nested workflow→workflow delegations are sound.

    A workflow step may delegate to another workflow with a bare
    ``wb_run("<slug>")`` (in its ``invokes`` list or its step prose) and
    advance it to completion. That nested entry path skips the target's slash
    command, so it never loads the target's bound directions unit. The
    conductor closes this by *delivering* the bound directions to the target's
    instruction-less reasoning steps (see ``architecture/workflows``) — but
    that rescue only works when the target actually *has* a bound directions
    unit. This check surfaces, at author time, the two cases delivery cannot
    save:

    - **Dangling delegation** — the referenced name is workflow-shaped (kebab,
      contains ``-``) but resolves to no registered workflow and no declared
      capability. ``wb_run`` would return "Unknown workflow" at runtime. Error.
    - **Blind delegation** — the target is a real workflow whose reasoning
      steps are bare *and* it has no bound directions unit. A nested caller
      reaches those steps with no instructions and nothing to deliver. Error.
    - **Param-contract mismatch** — the delegation passes a param the target
      workflow does not declare in its ``params_schema`` (or the target
      declares no schema at all). The call is rejected at the start-gate
      (`_validate_workflow_params`) before any step runs — "declares no
      params_schema" / "Unknown param(s)". Error. (This is the bug class where
      a caller and callee silently disagree on the interface.)

    A delegation into a workflow whose bare reasoning steps *are* covered by a
    bound directions unit is intentional and silent — runtime delivery handles
    it. Snake_case unknown names are assumed to be capabilities (possibly
    op-registered without a store declaration) and are left to
    ``capability_op_resolution``; they are not flagged here. Param-contract
    parsing is best-effort (single-level, non-nested ``{...}`` literals in
    prose); multi-line or nested params are skipped rather than mis-flagged.
    """
    workflow_slugs: dict[str, str] = {}      # slug -> store path
    capability_names: set[str] = set()
    for p, u in store.items():
        if isinstance(u, WorkflowUnit) and u.workflow_name:
            workflow_slugs[u.workflow_name] = p
        elif isinstance(u, CapabilityUnit) and getattr(u, "capability_name", ""):
            capability_names.add(u.capability_name)
    bound_workflows = {
        u.workflow
        for u in store.values()
        if isinstance(u, DirectionsUnit) and u.workflow
    }

    def _bare_reasoning_ids(wf: WorkflowUnit) -> list[str]:
        instr = wf.step_instructions or {}
        return [
            s["id"] for s in (wf.steps or [])
            if isinstance(s, dict) and s.get("step_type") == "reasoning"
            and s.get("id") and s["id"] not in instr
        ]

    errors: list[dict[str, str]] = []
    for path, unit in sorted(store.items()):
        if not isinstance(unit, WorkflowUnit):
            continue

        # Collect referenced names from the structured `invokes` lists and the
        # `wb_run("...")` calls in step prose + workflow-level content.
        referenced: set[str] = set()
        for s in unit.steps or []:
            if isinstance(s, dict):
                for inv in s.get("invokes") or []:
                    if isinstance(inv, str):
                        referenced.add(inv)
        texts = [v for v in (unit.step_instructions or {}).values() if isinstance(v, str)]
        full = unit.content.get("full", "") if isinstance(unit.content, dict) else ""
        if isinstance(full, str):
            texts.append(full)
        # Params passed at each delegation site: workflow name -> set of keys.
        passed_params: dict[str, set[str]] = {}
        for t in texts:
            for m in _WB_RUN_RE.finditer(t):
                referenced.add(m.group(1))
            for m in _WB_RUN_PARAMS_RE.finditer(t):
                passed_params.setdefault(m.group(1), set()).update(
                    _PARAM_KEY_RE.findall(m.group(2))
                )

        for name in sorted(referenced):
            if name == unit.workflow_name:
                continue                       # self-reference ("Start via …")
            if name in capability_names:
                continue                       # a capability call, not a delegation
            if name in workflow_slugs:
                target = store[workflow_slugs[name]]
                bare = _bare_reasoning_ids(target)  # type: ignore[arg-type]
                if bare and workflow_slugs[name] not in bound_workflows:
                    errors.append({
                        "check": "workflow_delegation_resolution",
                        "path": path,
                        "message": (
                            f"delegates to workflow {name!r} whose reasoning "
                            f"steps {bare} are bare and have no bound directions "
                            "unit — a nested invocation reaches them with no "
                            "instructions and nothing for the conductor to "
                            "deliver. Bind a directions unit or write the step "
                            "bodies."
                        ),
                    })
                # bare + bound → covered by runtime delivery → intentional, silent

                # Param contract: every key the delegation passes must be
                # declared in the target's params_schema. A target with no
                # schema rejects ANY params at runtime; an undeclared key is an
                # "Unknown param(s)" rejection. Either way the call errors at
                # the start-gate before a single step runs.
                declared = set((getattr(target, "params_schema", None) or {}).keys())
                undeclared = sorted(passed_params.get(name, set()) - declared)
                if undeclared:
                    errors.append({
                        "check": "workflow_delegation_resolution",
                        "path": path,
                        "message": (
                            f"delegates to workflow {name!r} passing param(s) "
                            f"{undeclared} it does not declare in params_schema — "
                            "the call is rejected at the param gate before any "
                            "step runs. Declare the param(s) on the target "
                            "(and wire them via input_map) or drop them."
                        ),
                    })
            elif "-" in name:
                errors.append({
                    "check": "workflow_delegation_resolution",
                    "path": path,
                    "message": (
                        f"delegates via wb_run({name!r}) which resolves to no "
                        "registered workflow and no declared capability — "
                        "dangling delegation (would return 'Unknown workflow')"
                    ),
                })
            # snake_case unknown → assume a capability; left to capability_op_resolution
    return errors


# ---------------------------------------------------------------------------
# Main validation runner
# ---------------------------------------------------------------------------

_CHECKS = [
    ("dag_integrity", _check_dag),
    ("command_mapping", _check_command_mapping),
    ("thinned_commands", _check_thinned_commands),
    ("store_path_validity", _check_store_path_in_commands),
    ("required_fields", _check_required_fields),
    ("directions_fields", _check_directions_fields),
    ("kind_specific_fields", _check_kind_specific_fields),
    ("placeholder_duplicate", _check_placeholder_duplicates),
    ("durable_surfaces", _check_durable_surfaces),
    ("parent_child_symmetry", _check_parent_child_symmetry),
    ("capability_op_resolution", _check_capability_op_resolution),
    ("workflow_step_dag", _check_workflow_step_dag),
    ("workflow_step_consistency", _check_workflow_step_consistency),
    ("directions_workflow_resolution", _check_directions_workflow_resolution),
    ("workflow_delegation_resolution", _check_workflow_delegation_resolution),
]


def validate_store(
    *,
    checks: list[str] | None = None,
) -> dict[str, Any]:
    """Run all (or selected) validation checks on the knowledge store.

    Args:
        checks: List of check names to run. None = run all.

    Returns:
        Dict with ``passed``, ``failed``, ``warnings``, ``total_units``, an
        ``errors`` list (blocking, ``severity != "warning"``), and an
        ``issues`` list (every finding, errors and warnings together).

    Severity: a check may tag a finding with ``severity: "warning"`` to mark
    it non-blocking. Findings without a ``severity`` key default to ``error``,
    so existing checks are unaffected. ``passed`` and ``failed`` count blocking
    errors only.
    """
    store = load_store()
    all_errors: list[dict[str, str]] = []

    checks_run: list[str] = []
    for name, fn in _CHECKS:
        if checks and name not in checks:
            continue
        checks_run.append(name)

        # Some checks need the store, some don't
        try:
            import inspect
            sig = inspect.signature(fn)
            if sig.parameters:
                errs = fn(store)
            else:
                errs = fn()
        except Exception as e:
            all_errors.append({
                "check": name,
                "path": "",
                "message": f"Check raised exception: {e}",
            })
            continue

        all_errors.extend(errs)

    # Split blocking errors from non-blocking warnings. A finding with no
    # ``severity`` key defaults to "error" — existing checks are unchanged.
    errors = [e for e in all_errors if e.get("severity", "error") != "warning"]
    warnings = [e for e in all_errors if e.get("severity", "error") == "warning"]

    # Summarize by check (errors and warnings together)
    summary: dict[str, int] = {}
    for err in all_errors:
        summary[err["check"]] = summary.get(err["check"], 0) + 1

    return {
        "total_units": len(store),
        "checks_run": checks_run,
        "passed": len(errors) == 0,
        "failed": len(errors),
        "warnings": len(warnings),
        "summary": summary,
        "errors": errors,
        "issues": all_errors,
    }


# ---------------------------------------------------------------------------
# MCP-facing callable
# ---------------------------------------------------------------------------

def docs_validate(
    *,
    checks: str | None = None,
) -> dict[str, Any]:
    """Validate the knowledge store: DAG integrity, command mappings, required fields.

    Args:
        checks: Comma-separated check names to run. Empty = run all.
                 Available: dag_integrity, command_mapping, thinned_commands,
                 store_path_validity, required_fields, directions_fields,
                 kind_specific_fields, placeholder_duplicate,
                 durable_surfaces, parent_child_symmetry,
                 capability_op_resolution,
                 workflow_step_dag, workflow_step_consistency,
                 directions_workflow_resolution,
                 workflow_delegation_resolution
    """
    check_list = [c.strip() for c in checks.split(",") if c.strip()] if checks else None
    return validate_store(checks=check_list)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    result = validate_store()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
