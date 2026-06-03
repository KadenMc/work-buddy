"""Capability registry — discovers and indexes work-buddy functions and workflows.

The registry is built once at first access by scanning Python modules and
loading workflow definitions from the knowledge store. It powers the
``wb_search`` gateway tool.

Workflow DAG structure, step instructions, auto_run specs, and execution
policy live in ``kind: workflow`` units — one Markdown file per workflow under
``knowledge/store/`` (``steps`` in frontmatter, per-step prose in
``## <step-id>`` body sections). The conductor reads these at runtime via
``_discover_workflows_from_store()``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from work_buddy.frontmatter import parse_frontmatter

# Capability/workflow definitions get four Threads-FSM-related fields
# (is_action, available_in, intrinsic_amplifiers,
# parameter_schema_for_action, requires_post_review). The
# InvocationContext enum lives in work_buddy.threads.enums (a
# pure-data module with no other work_buddy deps, so this import is
# cycle-safe).
from work_buddy.threads.enums import InvocationContext

if TYPE_CHECKING:
    from work_buddy.control.gates import Gate

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent
_SLASH_CMD_DIR = _REPO_ROOT / ".claude" / "commands"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Capability:
    """A simple callable function exposed through the gateway."""

    name: str
    description: str
    category: str  # messaging, contracts, status, journal, memory, tasks, context
    parameters: dict[str, dict[str, Any]]  # {name: {type, description, required}}
    callable: Callable
    search_aliases: list[str] = field(default_factory=list)  # extra phrases for search scoring
    param_aliases: dict[str, str] = field(default_factory=dict)  # {alias: canonical} e.g. {"target_date": "target"}
    requires: list[str] = field(default_factory=list)  # tool/component IDs, e.g. ["obsidian", "hindsight"]
    # Names of other capabilities this capability calls directly. Used by
    # the control graph to resolve transitive component dependencies
    # (e.g. a workflow step invokes `task_toggle` which requires `obsidian`,
    # so the step and workflow inherit the `obsidian` dependency).
    # Empty list means "audited, no invocations"; missing entries are
    # treated the same — see tests/unit/test_registry_invariants.py.
    invokes: list[str] = field(default_factory=list)
    mutates_state: bool = False  # whether this capability modifies state
    retry_policy: str = "manual"  # "replay" | "verify_first" | "manual"
    # When True (default), the gateway auto-enqueues transient failures
    # of non-mutating capabilities for background retry. Capabilities
    # that represent real work with non-recoverable failure modes (e.g.
    # local-LLM calls where a timeout means the model is hung and
    # retrying wastes tokens and spams consent prompts) should set this
    # to False to keep the failure in the caller's face.
    auto_retry: bool = True
    slash_command: str | None = None  # e.g. "wb-journal-update"
    consent_operations: list[str] = field(default_factory=list)  # @requires_consent op IDs this capability may trigger
    # Carve-out for the workflow-blanket consent model: capabilities tagged
    # ``"high"`` are NEVER carried by a workflow grant, even inside an
    # approved workflow run — the per-op consent gate always fires for them.
    # The default ``"low"`` participates in workflow grants normally;
    # ``"moderate"`` matches the default risk semantics. Workflows that
    # invoke any ``"high"`` capability cannot be silently authorized end-to-
    # end; the workflow's plan/confirm step or an individual consent prompt
    # is the user's decision point for the high-weight op.
    consent_weight: str = "low"  # "low" | "moderate" | "high"
    # Effect manifest for multi-effect capabilities — used by
    # ``verify_post_write_effects`` to detect "some effects landed,
    # some didn't" partial states after a PostWriteUncertain. Capabilities
    # WITHOUT a manifest fall back to single-effect verify (the existing
    # behavior). See ``work_buddy.obsidian.effects.EffectSpec`` for the
    # schema and ``architecture/capability-registry`` for the picking
    # rule. Capabilities with a manifest MUST be idempotent on retry —
    # the partial-state recovery path retries the full capability.
    effects: list[Any] = field(default_factory=list)  # list[EffectSpec]

    # The ``op.<namespace>.<name>`` ID this Capability resolved against, when it
    # was loaded from an inert *declaration* (a ``kind: "capability"`` knowledge
    # unit with an ``op`` field) by
    # ``capability_loader.load_declared_capabilities`` — the standard path. None
    # for a Capability constructed directly (e.g. in tests).
    op_id: str | None = None

    # Optional mode-availability gate, resolved from the declaration's
    # ``available_when`` string by the capability loader. When set, ``wb_search``
    # hides and ``wb_run`` rejects this capability unless the gate is satisfied
    # by the session's active modes. None = always available (ungated).
    available_when: "Gate | None" = None

    # ---------------- Action Catalog fields (defaults are the legacy non-action shape) ----

    # Whether this capability appears in the Action Catalog (i.e.
    # whether action inference may propose it as the action to take
    # for a Thread). False by default; capabilities the FSM should
    # be able to dispatch as Standard Actions opt in by setting True.
    is_action: bool = False

    # Set of contexts where this capability is discoverable / callable.
    # The default mirrors what existing capabilities expect: every
    # context EXCEPT FSM_INTERNAL (which is reserved for FSM-engine-only
    # operations the agent should never see directly). Sensitive
    # capabilities and FSM internals override this set.
    available_in: set[InvocationContext] = field(
        default_factory=lambda: {
            InvocationContext.AGENT_CONVERSATION,
            InvocationContext.AGENT_AUTONOMOUS,
            InvocationContext.ACTION_PROPOSAL,
            InvocationContext.USER_INVOCATION,
        }
    )

    # Per DESIGN.md §10.4 — risk amplifiers intrinsic to the action
    # (regardless of caller / thread). Composed with Thread.risk_profile
    # at execution time. e.g. ``send_email`` → {"reversibility":
    # "irreversible", "regret_potential": "high"}.
    intrinsic_amplifiers: dict[str, str] = field(default_factory=dict)

    # If is_action=True: the JSONSchema (or simplified parameter
    # spec) the inference module proposes parameters against. Falls
    # back to the existing ``parameters`` field if not set, but
    # action templates with non-trivial parameter shapes should
    # provide an explicit schema.
    parameter_schema_for_action: dict[str, Any] = field(default_factory=dict)

    # Per DESIGN.md §7.2 / §7.7 (R7.6) — when the FSM dispatches this
    # action, should the resulting Thread enter `awaiting_review`
    # after `executing` succeeds? Most actions do NOT (False, default
    # — the Thread goes straight to `done`). Action templates that
    # produce output the user must validate (drafts, summaries,
    # decompositions) opt in by setting True.
    requires_post_review: bool = False

    # Wall-time budget for one gateway dispatch of this capability, owned by
    # the operation (never supplied by the caller). The gateway resolves it
    # to a concrete budget per dispatch, most-specific-wins:
    #   - a callable ``(params) -> float | None`` derives the budget from the
    #     actual invocation (for operations whose runtime scales with input);
    #   - a ``float`` is a fixed ceiling in seconds;
    #   - ``None`` (the default) means "unset" — the gateway applies the
    #     domain default (capabilities requiring the Obsidian bridge are
    #     self-retrying and run unbounded; everything else gets 30s).
    # A resolved budget of ``math.inf`` (or a callable/scalar that yields it)
    # means no gateway timeout. See ``mcp_server.dispatch_resilience``.
    timeout_seconds: "float | None | Callable[[Mapping[str, Any]], float | None]" = None


@dataclass
class AutoRun:
    """Specification for a step the conductor executes automatically.

    The callable is imported lazily at execution time. Only ``work_buddy.*``
    import paths are allowed (enforced by the conductor).

    ``input_map`` wires prior step results into kwargs. Each key is a kwarg
    name; each value is a step ID whose result becomes that kwarg's value.
    Example: ``input_map: {cfg: load-config}`` passes the ``load-config``
    step's result as the ``cfg`` keyword argument.
    """

    callable: str  # dotted Python path, e.g. "work_buddy.morning.get_morning_config"
    kwargs: dict[str, Any] = field(default_factory=dict)
    input_map: dict[str, str] = field(default_factory=dict)  # kwarg_name → step_id
    timeout: int = 30  # seconds


@dataclass
class ResultVisibility:
    """Controls what portion of a step result the agent sees inline.

    The full result is always stored in the DAG on disk.  This spec only
    affects what appears in the MCP response.  Agents can retrieve full
    results on-demand via ``wb_step_result``.

    Modes:
      full    — complete result (up to ``_STEP_RESULT_CAP``)
      summary — manifest with structural hints (keys, sizes)
      none    — bare status card (step_id, size, retrievable flag)
      auto    — full if <=10 KB serialized, else summary (default)
    """

    mode: str = "auto"  # "full" | "summary" | "none" | "auto"
    include_keys: list[str] = field(default_factory=list)   # for summary: only these keys inline
    exclude_keys: list[str] = field(default_factory=list)   # for summary: omit these keys


@dataclass
class WorkflowStep:
    """A single step in a workflow procedure."""

    id: str
    name: str
    instruction: str  # what to tell the agent
    step_type: str  # "code" or "reasoning"
    depends_on: list[str] = field(default_factory=list)
    execution: str = "main"  # "main" or "subagent"
    workflow_file: str | None = None  # sub-workflow reference
    optional: bool = False
    requires: list[str] = field(default_factory=list)  # tool IDs for conductor gating
    # Capability names this step calls. For auto_run steps, this is typically
    # a single capability owning the callable; for reasoning steps it's the
    # capabilities the agent is instructed to invoke. Populated via
    # `invokes: [...]` in the workflow unit's step frontmatter. The control-graph
    # capability resolver walks this to compute transitive component dependencies.
    invokes: list[str] = field(default_factory=list)
    auto_run: AutoRun | None = None  # conductor auto-executes this step
    result_schema: dict[str, Any] | None = None  # validate agent output before storing
    requires_individual_consent: bool = False  # if True, workflow blanket consent is suspended for this step
    visibility: ResultVisibility | None = None  # controls inline result exposure; None = auto


@dataclass
class WorkflowDefinition:
    """A multi-step workflow with DAG structure."""

    name: str
    description: str
    workflow_file: str  # relative path from repo root
    execution: str  # default execution policy
    allow_override: bool = True
    steps: list[WorkflowStep] = field(default_factory=list)
    context: str = ""  # philosophy, "What NOT to do" sections
    slash_command: str | None = None  # e.g. "wb-morning"
    # Computed at registry-build time: the union of all tool/component IDs
    # required by this workflow's steps — both `step.requires` directly and
    # the `requires` of capabilities named in `step.invokes`. Do not
    # hand-author; see `_compute_workflow_requires()`.
    requires: list[str] = field(default_factory=list)

    # Computed at registry-build time: the store path of the kind:directions
    # unit whose `workflow:` field targets this workflow (its bound directions
    # unit), or None if unbound. The conductor delivers this unit's rendered
    # content to the workflow's instruction-less reasoning steps so the
    # directions reach the agent on every entry path — not only the slash
    # command. Do not hand-author; see `_index_directions_by_workflow()`.
    bound_directions_path: str | None = None

    # Optional schema for caller-provided initial params, mirrors
    # ``Capability.parameters``: ``{name: {type, description, required}}``.
    # Workflows that declare no schema reject any non-empty params at
    # ``start_workflow`` time. Consumed by ``input_map`` via the
    # synthetic ``__params__`` source key (see conductor) and surfaced
    # to reasoning steps via ``initial_params`` in the first-step
    # response payload.
    params_schema: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Optional mode-availability gate, resolved from the ``WorkflowUnit``'s
    # ``available_when`` string. When set, ``wb_search`` hides and ``wb_run``
    # rejects this workflow unless the gate is satisfied by the session's
    # active modes. None = always available (ungated).
    available_when: "Gate | None" = None

    # ---------------- Action Catalog fields (defaults are the legacy non-action shape) ----
    # Workflows can also be Action Catalog
    # entries (i.e. a Standard Action whose execution dispatches
    # into the workflow conductor). The fields mirror Capability's.

    is_action: bool = False
    available_in: set[InvocationContext] = field(
        default_factory=lambda: {
            InvocationContext.AGENT_CONVERSATION,
            InvocationContext.AGENT_AUTONOMOUS,
            InvocationContext.ACTION_PROPOSAL,
            InvocationContext.USER_INVOCATION,
        }
    )
    intrinsic_amplifiers: dict[str, str] = field(default_factory=dict)
    parameter_schema_for_action: dict[str, Any] = field(default_factory=dict)
    requires_post_review: bool = False

    # Forward-looking: if a graduated improvised action got promoted
    # into the catalog, record the originating Thread for provenance.
    improvised_origin_thread_id: str | None = None


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Capability | WorkflowDefinition] | None = None

# Stash of full ``Capability`` objects for capabilities filtered out of the
# live registry by the `_build_registry` filter pass (because their tool
# requirements aren't met). Populated alongside ``DISABLED_CAPABILITIES``.
# Used by ``work_buddy.recovery.recheck_disabled_capability`` to restore a
# capability to the live registry without re-running the full registry
# build (~6s + sys.modules purge). Keys MUST stay in sync with
# ``DISABLED_CAPABILITIES`` keys; see invariant tests.
#
# Cleared at the top of every ``_build_registry()`` invocation so a stale
# Capability whose closure references a purged module never survives a
# reload (mcp_registry_reload purges work_buddy.* from sys.modules).
_DISABLED_REGISTRY: dict[str, Capability] = {}


def get_registry() -> dict[str, Capability | WorkflowDefinition]:
    """Return the registry, building it on first access."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def get_disabled_registry() -> dict[str, Capability]:
    """Return the stash of full Capability objects for disabled capabilities.

    Read-only access for ``work_buddy.recovery.recheck_disabled_capability``
    and observability tools. The dict is mutated only inside
    ``_build_registry()`` (cleared + repopulated) and inside the recovery
    module under its lock (popped on successful restore). Callers MUST
    NOT mutate it directly.
    """
    # Trigger a build if the registry hasn't been initialised — populates
    # _DISABLED_REGISTRY as a side effect.
    get_registry()
    return _DISABLED_REGISTRY


def invalidate_registry() -> None:
    """Clear the cached registry so it rebuilds on next access.

    Also purges ``work_buddy.*`` modules from ``sys.modules`` so deferred
    imports in capability builders re-read the current source code.
    Clears tool probe cache so tools are re-probed on rebuild.

    **Re-bootstraps the Threads FSM after the purge.** Purging
    ``work_buddy.threads.engine`` from sys.modules nukes the
    process-global ``_REGISTERED_SIDE_EFFECTS`` dict, which is where
    the FSM state-entry handlers (enqueue inference, publish
    Resolution Surface card, etc.) live. Without re-bootstrap, the
    next FSM transition after a registry reload would land in a
    wait state with no handlers registered, so spawn capabilities
    would silently dead-end at AWAITING_INFERENCE.
    """
    import sys
    from work_buddy.tools import invalidate_tool_status

    global _REGISTRY
    _REGISTRY = None
    invalidate_tool_status()

    # Purge work_buddy modules so next import picks up new code.
    # Exclude the MCP server's own module to avoid breaking the
    # running event loop.
    keep_prefixes = ("work_buddy.mcp_server.server",)
    to_remove = [
        k for k in sys.modules
        if k.startswith("work_buddy.") and not k.startswith(keep_prefixes)
    ]
    for k in to_remove:
        del sys.modules[k]

    # Re-bootstrap the Threads FSM in this subprocess. Best-effort: if
    # bootstrap fails (e.g. budget hook init issue), the registry is
    # still valid — capabilities will work, but FSM transitions on
    # Threads won't fire side effects. Fail loud so the user notices.
    try:
        from work_buddy.threads.bootstrap import bootstrap_for_subprocess
        bootstrap_for_subprocess(subprocess_name="mcp-gateway-reload")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Threads re-bootstrap after registry reload failed: %s. "
            "FSM state-entry handlers may be missing; spawn capabilities "
            "could dead-end. Restart the gateway to recover.",
            exc,
        )


def _disabled_reason(capability_name: str) -> str:
    """Human-readable reason a capability is disabled in the live registry.

    Returns a string like "Dependency unavailable: obsidian (probe says
    'Bridge unreachable', last probe Ns ago)" so an agent consuming
    ``wb_search`` results can distinguish "backing service is down"
    from "your session's ACL doesn't allow this" AND know HOW LONG
    the dep has been down + WHY — two very different problems that
    used to share a single ``unavailable: true`` flag and mislead
    reasoning models into the wrong conclusion.

    Post-CP-A5 the message also distinguishes three states per missing
    tool:

    1. **Probe still failing** — auto-recovery already tried (CP-A3) or
       the cool-down hasn't expired; tool is genuinely down. Format:
       "<tool> probe failed Ns ago: '<reason>'".
    2. **Probe now passing but cap still in DISABLED_CAPABILITIES** —
       rare race; suggest mcp_registry_reload. Format: "<tool> probe
       reports available but capability not yet in registry".
    3. **No probe data yet** — cold-start race; agent should retry or
       run mcp_registry_reload. Format: "<tool> probe hasn't completed
       yet".
    """
    try:
        from work_buddy.tools import DISABLED_CAPABILITIES, get_tool_status
        deps = DISABLED_CAPABILITIES.get(capability_name)
        if not deps:
            return "Not registered in the live capability set"

        # Pull fresh probe state per missing tool. get_tool_status returns
        # {tools: {tool_id: {available, probe_ms, reason, ...}}, ...}.
        tool_status = get_tool_status().get("tools", {})

        # Compute probe age once from the tool_status.json mtime. This is
        # cheaper than per-tool tracking and good enough for a human-
        # readable diagnostic. Falls back gracefully if the file is
        # missing or unreadable.
        probe_age_str = _format_probe_age()

        per_tool: list[str] = []
        for dep in deps:
            entry = tool_status.get(dep)
            if entry is None:
                # State 3: no probe data yet. Cold start.
                per_tool.append(
                    f"{dep} (no probe data yet — wait {probe_age_str} or "
                    f"run mcp_registry_reload)"
                )
                continue
            if entry.get("available"):
                # State 2: probe passing but cap still disabled. This
                # happens if the user calls a disabled cap WITHOUT going
                # through the wb_run dispatch path (which would auto-
                # recover via CP-A3) — e.g. wb_search hits.
                per_tool.append(
                    f"{dep} (probe reports available — run "
                    f"mcp_registry_reload to re-enable this capability)"
                )
                continue
            # State 1: probe still failing.
            reason = entry.get("reason") or "no reason recorded"
            per_tool.append(
                f"{dep} (probe failed {probe_age_str}: '{reason}')"
            )

        return "Dependency unavailable: " + "; ".join(per_tool)
    except Exception:
        # Defensive fallback: if anything in the enriched path fails,
        # don't crash wb_search — return a usable string.
        try:
            from work_buddy.tools import DISABLED_CAPABILITIES
            deps = DISABLED_CAPABILITIES.get(capability_name)
            if deps:
                return f"Dependency unavailable: {', '.join(deps)}"
        except Exception:
            pass
    return "Not registered in the live capability set"


def _format_probe_age() -> str:
    """Approximate "Ns ago" label for the most recent probe sweep.

    Reads the mtime of ``<data_root>/runtime/tool_status.json`` (written
    atomically by every ``probe_all`` and ``reprobe_one`` call). Returns
    a short human-readable interval like ``"3s ago"``, ``"2m ago"``,
    or ``"unknown"`` if the file is missing or unreadable.
    """
    try:
        import time
        from work_buddy.tools import _TOOL_STATUS_FILE

        mtime = _TOOL_STATUS_FILE.stat().st_mtime
        elapsed = max(0.0, time.time() - mtime)
        if elapsed < 60:
            return f"{int(elapsed)}s ago"
        if elapsed < 3600:
            return f"{int(elapsed / 60)}m ago"
        return f"{int(elapsed / 3600)}h ago"
    except Exception:
        return "(probe age unknown)"


def mode_gate_denial(entry: Any, active_modes: set[str]) -> dict[str, Any] | None:
    """Return a mode-gate denial when ``entry``'s gate is unmet, else ``None``.

    When ``entry.available_when`` is set and not satisfied by ``active_modes``,
    returns a dict carrying ``denied_by="mode_gate"`` plus ``required_modes``
    and the current ``active_modes``. Returns ``None`` when the entry passes —
    including when it declares no gate. A mode denial is distinct from a
    session-ACL denial and is recoverable agent-side by toggling a mode on.
    """
    gate = getattr(entry, "available_when", None)
    if gate is None:
        return None
    from work_buddy.control import gates

    if gates.evaluate(gate, active_modes):
        return None
    required = sorted(gates.referenced_components(gate))
    return {
        "denied_by": "mode_gate",
        "required_modes": required,
        "active_modes": sorted(active_modes),
    }


def filter_results_by_modes(
    results: list[dict[str, Any]],
    active_modes: set[str],
    lookup: Callable[[str | None], Any],
) -> list[dict[str, Any]]:
    """Drop search-result dicts whose named entry has an unmet mode gate.

    ``lookup`` maps a result's ``name`` to its registry entry (or ``None``).
    Results whose entry is ungated, unknown, or unnamed pass through.
    """
    from work_buddy.control import gates

    out: list[dict[str, Any]] = []
    for r in results:
        name = r.get("name") if isinstance(r, dict) else None
        entry = lookup(name) if name else None
        gate = getattr(entry, "available_when", None) if entry is not None else None
        if gate is None or gates.evaluate(gate, active_modes):
            out.append(r)
    return out


def search_registry(
    query: str,
    category: str | None = None,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Search capabilities and workflows.

    Searches the unified knowledge store first (includes directions,
    system docs, capabilities, and workflows). Falls back to the legacy
    registry-only search if the store is unavailable.

    Empty query returns all entries (browse mode). Category filter is
    applied after scoring.
    """
    reg = get_registry()

    # Exact name match — check registry first, then store
    normalized = query.replace("-", "_").replace(" ", "_")
    exact = reg.get(query) or reg.get(normalized)
    if exact is not None:
        result = _entry_to_dict(exact)
        result["search_score"] = 1.0
        return [result]

    # Exact name not in registry (maybe filtered out) — check store by path
    try:
        from work_buddy.knowledge.store import load_store
        from work_buddy.knowledge.model import CapabilityUnit, WorkflowUnit
        store = load_store()
        # Search store for a CapabilityUnit with this exact capability_name
        for path, unit in store.items():
            if isinstance(unit, CapabilityUnit) and unit.capability_name == query:
                result = {
                    "name": unit.capability_name,
                    "description": unit.description,
                    "category": unit.category,
                    "type": "function",
                    "parameters": unit.parameters,
                    "search_score": 1.0,
                    "disabled": True,
                    "disabled_reason": _disabled_reason(unit.capability_name),
                    # Back-compat alias — remove after 2026-Q3
                    "unavailable": True,
                }
                return [result]
            if isinstance(unit, WorkflowUnit) and unit.workflow_name == query:
                # Exact-name hit in the store only means the registry
                # didn't have it — same "tool deps unmet" condition
                # as the CapabilityUnit branch above. Flag it.
                result = {
                    "name": unit.workflow_name,
                    "description": unit.description,
                    "category": "workflow",
                    "type": "workflow",
                    "search_score": 1.0,
                    "disabled": True,
                    "disabled_reason": _disabled_reason(unit.workflow_name),
                    # Back-compat alias — remove after 2026-Q3
                    "unavailable": True,
                }
                return [result]
    except Exception:
        pass  # Store unavailable, continue to search

    # Try unified store search (richer: includes directions + system docs)
    try:
        store_results = _search_via_store(query, category, top_n)
        if store_results is not None:
            return store_results
    except Exception:
        pass  # Fall through to legacy search

    # Legacy: Empty query = browse mode
    if not query:
        results = []
        for entry in reg.values():
            if category:
                entry_cat = entry.category if isinstance(entry, Capability) else "workflow"
                if entry_cat != category:
                    continue
            results.append(_entry_to_dict(entry))
        return results

    # Legacy: Hybrid search over registry only
    import time
    from work_buddy.mcp_server.search import hybrid_search, _log_to_file, _get_search_log
    _lf = _get_search_log()
    _log_to_file(_lf, f"Starting hybrid search for: {query!r}")
    t_start = time.time()
    scored = hybrid_search(query, reg, top_n=top_n)
    _log_to_file(_lf, f"Total search time: {time.time()-t_start:.2f}s")

    results = []
    for item in scored:
        name = item["name"]
        entry = reg.get(name)
        if entry is None:
            continue
        if category:
            entry_cat = entry.category if isinstance(entry, Capability) else "workflow"
            if entry_cat != category:
                continue
        result = _entry_to_dict(entry)
        result["search_score"] = item["score"]
        results.append(result)

    return results


def _search_via_store(
    query: str,
    category: str | None,
    top_n: int,
) -> list[dict[str, Any]] | None:
    """Search the unified knowledge store, returning registry-compatible dicts.

    Returns None if the store is unavailable or empty, triggering fallback.
    For capability/workflow results, enriches with registry execution metadata
    (parameters, steps) so wb_search callers get the same shape they expect.
    """
    from work_buddy.knowledge.search import search as store_search
    from work_buddy.knowledge.model import CapabilityUnit, WorkflowUnit

    # Map category to store kind for filtering
    kind = None
    if category == "workflow":
        kind = "workflow"

    r = store_search(query=query, kind=kind, depth="summary", top_n=top_n)
    if r.get("count", 0) == 0 and not query:
        return None  # Empty store, fall back

    results = []
    reg = get_registry()

    for hit in r.get("results", []):
        unit_kind = hit.get("kind", "")
        unit_name = hit.get("name", "")
        score = hit.get("score", 0.0)

        # For capabilities: return registry-compatible dict
        if unit_kind == "capability":
            cap_name = hit.get("capability_name", "")
            entry = reg.get(cap_name)
            if entry is not None:
                result = _entry_to_dict(entry)
                result["search_score"] = score
                results.append(result)
                continue
            # Capability not in registry (tool requirements unmet) — show from store
            result = {
                "name": cap_name,
                "description": hit.get("description", ""),
                "category": hit.get("category", ""),
                "type": "function",
                "parameters": hit.get("parameters", {}),
                "search_score": score,
                "disabled": True,
                "disabled_reason": _disabled_reason(cap_name),
                # Back-compat alias — remove after 2026-Q3
                "unavailable": True,
            }
            if category and result["category"] != category:
                continue
            results.append(result)

        # For workflows: return registry-compatible dict
        elif unit_kind == "workflow":
            wf_name = hit.get("workflow_name", "")
            entry = reg.get(wf_name)
            if entry is not None:
                result = _entry_to_dict(entry)
                result["search_score"] = score
                results.append(result)
                continue
            # Workflow in the store but not registered live — mirror
            # the CapabilityUnit branch above and flag it clearly so
            # agents don't try to call a workflow whose dependencies
            # aren't met.
            result = {
                "name": wf_name,
                "description": hit.get("description", ""),
                "category": "workflow",
                "type": "workflow",
                "search_score": score,
                "disabled": True,
                "disabled_reason": _disabled_reason(wf_name),
                # Back-compat alias — remove after 2026-Q3
                "unavailable": True,
            }
            results.append(result)

        # For directions/system: new result type
        else:
            result = {
                "name": hit.get("path", unit_name),
                "description": hit.get("description", ""),
                "category": unit_kind,
                "type": unit_kind,
                "search_score": score,
            }
            content = hit.get("content", "")
            if content:
                result["content_preview"] = content[:500]
            if category and result["category"] != category:
                continue
            results.append(result)

    return results if results else None


def get_entry(name: str) -> Capability | WorkflowDefinition | None:
    """Look up a single registry entry by exact name."""
    return get_registry().get(name)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _entry_to_dict(entry: Capability | WorkflowDefinition) -> dict[str, Any]:
    """Convert a registry entry to a JSON-friendly dict.

    Discriminates on shape (``.callable`` for Capability, ``.steps`` for
    WorkflowDefinition) rather than ``isinstance``.  Across an
    ``mcp_registry_reload`` the class identity of ``Capability`` /
    ``WorkflowDefinition`` changes (``sys.modules`` is purged and the
    classes are re-imported), so entries created before the reload no
    longer match ``isinstance`` against the post-reload classes.  When
    that happens, the workflow branch tries ``entry.execution`` on a
    Capability and raises ``AttributeError: 'Capability' object has no
    attribute 'execution'`` — an unhelpful error that leaks through the
    gateway's parameter-error reporting path.  Duck typing avoids this
    failure mode regardless of how stale the entry's class identity is.
    """
    is_capability = hasattr(entry, "callable") and not hasattr(entry, "steps")
    if is_capability:
        d = {
            "name": entry.name,
            "description": entry.description,
            "category": entry.category,
            "type": "function",
            "parameters": entry.parameters,
        }
        if entry.mutates_state:
            d["mutates_state"] = True
            d["retry_policy"] = entry.retry_policy
        if entry.slash_command:
            d["slash_command"] = entry.slash_command
        return d
    else:
        d = {
            "name": entry.name,
            "description": entry.description,
            "category": "workflow",
            "type": "workflow",
            "execution": entry.execution,
            "steps": [
                {"id": s.id, "name": s.name, "step_type": s.step_type,
                 "execution": s.execution, "depends_on": s.depends_on,
                 "workflow_file": s.workflow_file}
                for s in entry.steps
            ],
        }
        if entry.slash_command:
            d["slash_command"] = entry.slash_command
        return d


# ---------------------------------------------------------------------------
# Knowledge index warm-up
# ---------------------------------------------------------------------------

def _warm_knowledge_index() -> None:
    """Build the knowledge search index eagerly during registry init.

    BM25 builds inline (~50ms). Dense vectors are built in a background
    thread because the embedding service may still be loading models
    when the MCP server starts. If dense fails, BM25 is still ready.
    """
    import threading
    from work_buddy.knowledge.store import load_store
    from work_buddy.knowledge.index import get_index

    idx = get_index()
    if idx.is_built:
        return  # already warm

    store = load_store(scope="all")
    if not store:
        return

    # Build BM25 inline (fast, no external deps)
    idx.build(store, skip_dense=True)
    gen = idx._generation  # snapshot for the background thread

    # Build dense vectors in background (embedding service may be slow).
    # Two parallel signals: content (asymmetric 768-d) and aliases (symmetric
    # 1024-d). Each is independent — if one fails, the other still lands.
    #
    # Cold-start race fix (2026-05-04): the warmup thread previously fired
    # embed batches immediately after the registry build, which was often
    # 30-40s before the embedding service finished its first model load.
    # The batches timed out, the service returned None, and the user saw
    # ``Embedding service unavailable during knowledge alias dense build``
    # warnings on every cold sidecar start. Now we poll
    # ``embedding.client.wait_until_available`` (~30s budget) before either
    # build fires. If the wait times out we log an INFO line and return —
    # search still works via BM25 fallback; the next periodic rebuild
    # picks up the dense signals once the service warms up.
    def _build_dense() -> None:
        try:
            from work_buddy.embedding.client import wait_until_available
        except Exception as e:  # defensive — embedding module shouldn't fail to import
            logger.info(
                "knowledge-dense-warmup: embedding client unavailable "
                "(%s); skipping dense build for this cycle.", e,
            )
            return
        if not wait_until_available(timeout_s=30.0, interval_s=0.5):
            logger.info(
                "knowledge-dense-warmup: embedding service didn't reach "
                "'ok' within 30s; skipping dense build. Search will use "
                "BM25-only ranking until the next periodic rebuild.",
            )
            return
        try:
            idx._build_content_vectors(expected_generation=gen)
        except Exception:
            pass  # logged inside the builder
        try:
            idx._build_alias_vectors(expected_generation=gen)
        except Exception:
            pass  # logged inside the builder

    thread = threading.Thread(
        target=_build_dense,
        name="knowledge-dense-warmup",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------

def _build_registry() -> dict[str, Capability | WorkflowDefinition]:
    """Scan modules and workflow files to populate the registry.

    !! IMPORT DEADLOCK RISK !!
    Capability callables run via asyncio.to_thread(). Any callable that
    does a deferred import of a C-extension module (numpy, sqlite3, etc.)
    can permanently deadlock the MCP server. See the knowledge store unit
    architecture/mcp-import-discipline for the full explanation.
    When adding capabilities, ensure callables only use
    lightweight imports (urllib, json, pathlib) or HTTP calls to services.
    """
    import time
    from work_buddy.mcp_server.search import _log_to_file, _get_search_log
    from work_buddy.tools import (
        _register_default_probes, probe_all, is_tool_available,
        obsidian_backed_tools, DISABLED_CAPABILITIES,
    )
    _lf = _get_search_log()
    _log_to_file(_lf, "Registry build starting...")

    _build_start = time.time()
    _section_times: dict[str, float] = {}

    # --- Tool probes ---
    t = time.time()
    _register_default_probes()
    tool_status = probe_all(force=True)
    available = [tid for tid, s in tool_status.items() if s["available"]]
    unavailable = [tid for tid, s in tool_status.items() if not s["available"]]
    _section_times["tool_probes"] = time.time() - t
    _log_to_file(_lf, f"  tool_probes: {_section_times['tool_probes']:.2f}s — "
                       f"available={available}, unavailable={unavailable}")

    registry: dict[str, Capability | WorkflowDefinition] = {}

    # --- Capabilities (resolved from declarations) ---
    # Every capability is a declaration: a ``kind: "capability"`` knowledge
    # unit carrying an ``op`` field. The loader resolves each against the Op
    # registry and returns ready-to-dispatch Capability objects, resolved
    # before the tool-requirements filter below so capabilities with unmet
    # ``requires`` are filtered out by the same pass. A name appearing in two
    # declarations is a mistake we surface, not silently shadow.
    t = time.time()
    try:
        from work_buddy.knowledge.capability_loader import load_declared_capabilities
        declared, decl_issues = load_declared_capabilities()
        for cap in declared:
            if cap.name in registry:
                logger.error(
                    "capability declaration conflict for %r — keeping the "
                    "existing registry entry, ignoring the declaration",
                    cap.name,
                )
                continue
            registry[cap.name] = cap
        for issue in decl_issues:
            logger.warning("capability declaration issue: %s", issue)
        _log_to_file(_lf, f"  declared_capabilities: {time.time()-t:.2f}s "
                           f"({len(declared)} loaded, {len(decl_issues)} issues)")
    except Exception as e:
        _log_to_file(_lf, f"  declared_capabilities: FAILED in {time.time()-t:.2f}s — {e}")
        logger.exception("declaration-based capability loading failed")

    # --- Filter capabilities with unmet tool requirements ---
    t = time.time()
    # Auto-extract requires from @requires_tool decorated callables
    for cap in list(registry.values()):
        if isinstance(cap, Capability):
            inferred = getattr(cap.callable, '_requires_tools', [])
            if inferred and not cap.requires:
                cap.requires = list(inferred)

    DISABLED_CAPABILITIES.clear()
    # CP-A1: also clear the full-Capability stash. Critical for
    # closure-correctness across mcp_registry_reload (which purges
    # sys.modules); a Capability stashed during the previous build
    # would dereference a now-dead module if it survived.
    _DISABLED_REGISTRY.clear()
    bridge_tools = obsidian_backed_tools()
    bridge_down = not is_tool_available("obsidian")
    for name in list(registry):
        entry = registry[name]
        if isinstance(entry, Capability) and entry.requires:
            missing = [t_id for t_id in entry.requires if not is_tool_available(t_id)]
            # Obsidian-bridge availability is governed at runtime by a circuit
            # breaker on the gateway dispatch, not by this build-time flip. A
            # transient bridge probe failure must not disable every bridge-
            # dependent capability (the bridge itself AND its in-Obsidian
            # plugins: datacore, smart_connections, ...) for the whole session;
            # an admitted capability whose bridge is down fails fast per call
            # and recovers the instant the bridge returns (no registry reload).
            # Transitive-only: we only skip the hard-disable when the bridge
            # ITSELF is down (so the plugin is unavailable *because of* the
            # bridge). If the bridge is up but a plugin is genuinely missing —
            # or a non-bridge dep (calendar, hindsight, thunderbird, ...) is
            # absent — keep the hard-disable; a breaker is wrong for a
            # dependency that will not appear within the session.
            if bridge_down:
                missing = [t_id for t_id in missing if t_id not in bridge_tools]
            if missing:
                DISABLED_CAPABILITIES[name] = missing
                # CP-A1: stash the full Capability object so the recovery
                # module can restore it without rebuilding the registry.
                _DISABLED_REGISTRY[name] = entry
                del registry[name]

    if DISABLED_CAPABILITIES:
        _log_to_file(_lf, f"  filtered: {len(DISABLED_CAPABILITIES)} capabilities "
                           f"disabled due to missing tools")
    else:
        _log_to_file(_lf, f"  filtered: 0 capabilities disabled (all tools available)")
    _log_to_file(_lf, f"  filter_pass: {time.time()-t:.2f}s")

    t = time.time()
    for wf in _discover_workflows_from_store():
        registry[wf.name] = wf
    _log_to_file(_lf, f"  workflows (store): {time.time()-t:.2f}s")

    # Compute WorkflowDefinition.requires as the union of each step's
    # `requires` plus the `requires` of capabilities named in step.invokes.
    # Computed, never hand-authored.
    t = time.time()
    _compute_workflow_requires(registry)
    _log_to_file(_lf, f"  workflow_requires: {time.time()-t:.2f}s")

    # Populate slash_command on all entries from .claude/commands/ frontmatter
    t = time.time()
    slash_index = _build_slash_command_index()
    for target_name, cmd_stem in slash_index.items():
        entry = registry.get(target_name)
        if entry is not None:
            entry.slash_command = cmd_stem
    _log_to_file(_lf, f"  slash_commands: {time.time()-t:.2f}s ({len(slash_index)} mapped)")
    _log_to_file(_lf, f"Registry built: {len(registry)} entries")

    # --- Warm up the knowledge search index ---
    # BM25 builds inline (~50ms), dense vectors build in a background thread
    # since the embedding service may still be loading models at this point.
    t = time.time()
    try:
        _warm_knowledge_index()
        _section_times["knowledge_index"] = time.time() - t
        _log_to_file(_lf, f"  knowledge_index: {_section_times['knowledge_index']:.2f}s (BM25 inline, dense in background)")
    except Exception as e:
        _section_times["knowledge_index"] = time.time() - t
        _log_to_file(_lf, f"  knowledge_index: FAILED in {_section_times['knowledge_index']:.2f}s — {e}")

    # --- Slow-rebuild warning ---
    # The registry rebuild blocks whoever triggered it. If it happens on
    # the MCP event loop (a missing asyncio.to_thread around a registry
    # call), /health will also block — which historically made the sidecar
    # supervisor mark mcp_gateway unhealthy and auto-restart it, dropping
    # every Claude Code SSE stream in the process. Surface the duration
    # with a WARNING + per-section breakdown so that regression is
    # visible in sidecar logs rather than silent.
    total = time.time() - _build_start
    if total > 5.0:
        slowest = sorted(_section_times.items(), key=lambda kv: -kv[1])[:5]
        breakdown = ", ".join(f"{k}={v:.1f}s" for k, v in slowest)
        logger.warning(
            "Registry build slow: %.1fs total (%s). If this ran on the "
            "asyncio event loop, /health is blocked for the duration. "
            "Check architecture/mcp-import-discipline for async hygiene.",
            total, breakdown,
        )
    else:
        logger.debug("Registry build: %.2fs total", total)

    return registry


# ---------------------------------------------------------------------------
# Function capabilities (unchanged)
# ---------------------------------------------------------------------------

def _setup_help_component_param_description() -> str:
    """Build the setup_help component-id param description from the live
    COMPONENT_CATALOG so the available list never goes stale when a new
    component is registered. Falls back to a generic line if the catalog
    isn't yet populated (e.g. during a partial import)."""
    try:
        from work_buddy.health.components import COMPONENT_CATALOG
        ids = sorted(COMPONENT_CATALOG.keys())
    except Exception:
        ids = []
    base = "Component ID to diagnose, or 'all' for overview."
    if not ids:
        return base
    return f"{base} Available: {', '.join(ids)}"


def _context_block(
    sources: list[str] | None = None,
    exclude: list[str] | None = None,
    depth: str = "normal",
    per_source_depth: dict[str, str] | None = None,
    target_date: str | None = None,
    window_days: int = 1,
    max_chars: int | None = None,
    max_age_seconds: int | None = None,
    custom: dict[str, dict] | None = None,
    format: str = "markdown",
) -> dict[str, Any]:
    """MCP callable for the ``context_block`` capability.

    Top-level so the capability's ``callable`` reference stays stable
    across registry rebuilds. Returns a dict with ``rendered`` (the
    block) and ``sources`` (per-source item counts + metadata) so MCP
    clients can inspect what was included.
    """
    from datetime import date as _date

    from work_buddy.context import (
        ContextCollector,
        ContextCurator,
        ContextDepth,
        ContextRequest,
    )
    from work_buddy.context import sources as _sources_pkg  # registers sources
    _ = _sources_pkg  # silence unused-import warning

    try:
        depth_enum = ContextDepth[depth.upper()]
    except KeyError:
        return {"error": f"depth must be one of: brief, normal, deep; got {depth!r}"}

    per_depth: dict[str, ContextDepth] | None = None
    if per_source_depth:
        try:
            per_depth = {k: ContextDepth[v.upper()] for k, v in per_source_depth.items()}
        except KeyError as exc:
            return {"error": f"per_source_depth value invalid: {exc}"}

    target: _date | None = None
    if target_date:
        try:
            target = _date.fromisoformat(target_date)
        except ValueError:
            return {"error": f"target_date must be YYYY-MM-DD; got {target_date!r}"}

    if format not in ("markdown", "json"):
        return {"error": f"format must be 'markdown' or 'json'; got {format!r}"}

    req = ContextRequest(
        sources=sources,
        exclude=exclude,
        depth=depth_enum,
        per_source_depth=per_depth,
        target_date=target,
        window_days=window_days,
        max_chars=max_chars,
        max_age_seconds=max_age_seconds,
        custom=custom,
    )

    ctx = ContextCollector().collect(req)
    rendered = ContextCurator().curate(
        ctx,
        depth=depth_enum,
        per_source_depth=per_depth,
        max_chars=max_chars,
        format=format,
    )

    sources_manifest = {
        name: {
            "item_count": len(section.items),
            "metadata": section.metadata,
            "fetched_at": section.fetched_at.isoformat(),
        }
        for name, section in ctx.sections.items()
    }

    return {
        "rendered": rendered,
        "sources": sources_manifest,
        "format": format,
    }


def _context_drill_down(source: str, item_id: str, field: str) -> dict[str, Any]:
    """MCP callable for ``context_drill_down``."""
    from work_buddy.context import registry as _ctx_registry
    from work_buddy.context import sources as _sources_pkg  # registers sources
    _ = _sources_pkg

    src = _ctx_registry.get(source)
    if src is None:
        return {
            "error": f"Unknown source {source!r}. Registered: {_ctx_registry.names()}",
        }

    try:
        return src.drill_down(item_id, field)
    except NotImplementedError as exc:
        return {"error": str(exc), "error_kind": "not_implemented"}
    except KeyError as exc:
        return {"error": str(exc), "error_kind": "not_found"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "error_kind": "unknown"}


def _claude_code_usage_scan(*, full_rebuild: bool = False) -> dict[str, Any]:
    """Trigger the vendored Claude-Code-usage scanner."""
    from work_buddy.dashboard.costs_claude_code_usage import (
        rescan_claude_code_usage,
    )
    return rescan_claude_code_usage(full_rebuild=full_rebuild)


def _llm_costs_query(**kwargs: Any) -> dict[str, Any]:
    """Dispatch to the unified cost-query module."""
    from work_buddy.llm.cost_query import llm_costs_query
    return llm_costs_query(**kwargs)


def _escalation_recent(
    *,
    limit: int = 50,
    trace_id: str | None = None,
    final_outcome: str | None = None,
    source: str | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """Read recent records from the LLM-escalation log.

    See :mod:`work_buddy.llm.escalation_log` for the record shape.
    """
    from work_buddy.llm.escalation_log import (
        read_escalations,
        summarize_escalations,
    )
    if summary:
        return {
            "summary": summarize_escalations(limit=None),
            "applied_filters": {},
        }
    records = read_escalations(
        limit=limit,
        trace_id=trace_id,
        final_outcome=final_outcome,
        source=source,
    )
    return {
        "records": records,
        "count": len(records),
        "applied_filters": {
            "limit": limit,
            "trace_id": trace_id,
            "final_outcome": final_outcome,
            "source": source,
        },
    }


def _build_slash_command_index() -> dict[str, str]:
    """Scan .claude/commands/wb-*.md for `workflow:` frontmatter.

    Returns mapping of registry entry name → slash command stem.
    E.g. {"update-journal": "wb-journal-update", "task_briefing": "wb-task-briefing"}
    """
    index: dict[str, str] = {}
    if not _SLASH_CMD_DIR.exists():
        return index
    for md_file in sorted(_SLASH_CMD_DIR.glob("wb-*.md")):
        try:
            meta, _ = parse_frontmatter(md_file)
            target = meta.get("workflow")
            if target and isinstance(target, str):
                index[target] = md_file.stem
        except Exception:
            continue
    return index


def _compute_workflow_requires(
    registry: dict[str, Capability | WorkflowDefinition],
) -> None:
    """Populate ``WorkflowDefinition.requires`` in-place.

    For each workflow, unions:
      - Every step's own ``requires`` (tool/component IDs).
      - The ``requires`` of every capability named in ``step.invokes``.

    Invoked capabilities that are not (yet) in the registry (e.g. filtered
    out by tool availability) are skipped silently — the workflow will
    show an incomplete dependency set, which is recoverable once the
    upstream component comes back.

    Transitive closure (capability A.invokes = [B], B.requires = [obsidian])
    is followed one hop. Multi-hop chains (A invokes B invokes C) are not
    resolved here; the control-graph resolver in
    ``work_buddy.control.capability_resolver`` handles the full closure on
    demand without bloating the workflow dataclass.
    """
    for entry in registry.values():
        if not isinstance(entry, WorkflowDefinition):
            continue
        seen: set[str] = set()
        for step in entry.steps:
            for t_id in step.requires:
                seen.add(t_id)
            for cap_name in step.invokes:
                cap = registry.get(cap_name)
                if isinstance(cap, Capability):
                    for t_id in cap.requires:
                        seen.add(t_id)
        entry.requires = sorted(seen)


def _index_directions_by_workflow(store: dict[str, Any]) -> dict[str, str]:
    """Map a workflow's store path -> the path of its bound directions unit.

    A ``kind: directions`` unit binds to a workflow via its ``workflow:``
    frontmatter field, which holds the workflow's **store path** (e.g.
    ``daily-journal/update-journal``), not its slug. This reverse index lets
    the registry stamp each ``WorkflowDefinition`` with its bound directions
    path at build time, so the conductor can deliver that unit's content to
    the workflow's instruction-less reasoning steps without a store scan.

    Mirrors the ``documented_workflows`` set built in
    ``work_buddy.knowledge.validate._check_workflow_step_consistency``. The
    mapping is 1:1 in practice (a directions unit owns one workflow); on the
    pathological case of two directions units naming the same workflow, the
    last one wins — harmless, since the conductor only needs *some* bound
    directions content and the ``directions_workflow_resolution`` validator
    check guards the bindings themselves.
    """
    from work_buddy.knowledge.model import DirectionsUnit

    idx: dict[str, str] = {}
    for path, u in store.items():
        if isinstance(u, DirectionsUnit) and u.workflow:
            idx[u.workflow] = path
    return idx


def _resolve_mode_gate(raw: str | None, source: str) -> "Gate | None":
    """Resolve an ``available_when`` gate-DSL string to a ``Gate`` AST.

    Returns ``None`` when unset. On a malformed expression or a reference to an
    unknown mode id, logs a warning and returns ``None`` — a surface with a
    broken gate stays visible rather than silently hidden forever. (The
    capability loader handles its own resolution so it can surface the failure
    as a hard, count-checked issue instead.)
    """
    if not raw:
        return None
    from work_buddy.control import gates
    from work_buddy.modes.registry import get_known_mode_ids

    try:
        gate = gates.parse_gate(raw)
        gates.validate(gate, get_known_mode_ids())
        return gate
    except ValueError as exc:
        logger.warning("ignoring invalid available_when %r on %s: %s", raw, source, exc)
        return None


def _discover_workflows_from_store() -> list[WorkflowDefinition]:
    """Load workflow definitions from the knowledge store.

    Replaces ``_discover_workflows()`` (file-based). The store's
    ``WorkflowUnit`` entries contain the full DAG structure, step
    instructions, auto_run specs, and execution policy — everything
    the conductor needs.
    """
    from work_buddy.knowledge.store import load_store
    from work_buddy.knowledge.model import WorkflowUnit

    store = load_store()
    directions_by_workflow = _index_directions_by_workflow(store)
    workflows: list[WorkflowDefinition] = []

    for _path, unit in store.items():
        if not isinstance(unit, WorkflowUnit):
            continue
        if not unit.steps:
            continue

        wf_execution = unit.execution or "main"

        steps: list[WorkflowStep] = []
        for s in unit.steps:
            step_id = s.get("id", "")

            # Reconstruct AutoRun from dict
            auto_run_raw = s.get("auto_run")
            auto_run: AutoRun | None = None
            if isinstance(auto_run_raw, dict) and "callable" in auto_run_raw:
                auto_run = AutoRun(
                    callable=auto_run_raw["callable"],
                    kwargs=auto_run_raw.get("kwargs") or {},
                    input_map=auto_run_raw.get("input_map") or {},
                    timeout=auto_run_raw.get("timeout", 30),
                )

            # Reconstruct ResultVisibility from dict
            vis_raw = s.get("visibility")
            vis: ResultVisibility | None = None
            if isinstance(vis_raw, dict):
                vis = ResultVisibility(
                    mode=vis_raw.get("mode", "auto"),
                    include_keys=vis_raw.get("include_keys") or [],
                    exclude_keys=vis_raw.get("exclude_keys") or [],
                )

            steps.append(WorkflowStep(
                id=step_id,
                name=s.get("name", step_id),
                instruction=unit.step_instructions.get(step_id, ""),
                step_type=s.get("step_type", "reasoning"),
                depends_on=s.get("depends_on", []),
                execution=s.get("execution", wf_execution),
                workflow_file=s.get("workflow_ref"),
                optional=s.get("optional", False),
                requires=s.get("requires", []),
                invokes=s.get("invokes", []),
                auto_run=auto_run,
                result_schema=s.get("result_schema"),
                requires_individual_consent=s.get("requires_individual_consent", False),
                visibility=vis,
            ))

        # Extract context from content["full"] if present
        context = ""
        if unit.content and isinstance(unit.content, dict):
            context = unit.content.get("full", "")

        workflows.append(WorkflowDefinition(
            name=unit.workflow_name,
            description=unit.description,
            workflow_file=f"store:{_path}",  # provenance marker
            execution=wf_execution,
            allow_override=unit.allow_override,
            steps=steps,
            context=context,
            slash_command=unit.command,
            params_schema=unit.params_schema or {},
            bound_directions_path=directions_by_workflow.get(_path),
            available_when=_resolve_mode_gate(unit.available_when, f"store:{_path}"),
        ))

    return workflows

