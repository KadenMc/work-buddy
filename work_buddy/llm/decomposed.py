"""Declarative decomposition of LLM judgment calls.

A *decomposed* judgment is one main LLM call preceded by N small specialized
sub-calls whose outputs feed the main call's prompt. Each sub-call is a
:class:`SubCall` declaration carrying invariants (system prompt, schema,
soft-fail policy, prompt builder); its operational dials (tier chain,
cache TTL, max_tokens, temperature) come from a dotted config path and
are resolved at call time.

Usage::

    DEADLINE_HINTS_SUBCALL = SubCall(
        name="deadline_hints",
        system_prompt=_DEADLINE_SYSTEM_PROMPT,
        user_prompt=lambda inputs: _build_deadline_user(inputs),
        output_schema=_DEADLINE_SCHEMA,
        config_key="triage.deadline_extract",
        fail_policy="soft",
        soft_fail_default={"has_deadline": False, "has_dependency": False, ...},
    )

    result = run_subcall(DEADLINE_HINTS_SUBCALL, inputs={"text": "...", "date": ...})

For the full main + sub-calls bundle::

    MAIN = MainCall(name="verdict", system_prompt=..., user_prompt=lambda w: ...,
                    output_schema=..., config_key="triage.verdict")

    JUDGMENT = DecomposedJudgment(
        name="journal_clarify",
        sub_calls=(DEADLINE_HINTS_SUBCALL, PROJECT_PICKER_SUBCALL),
        main=MAIN,
    )

    result = JUDGMENT.run(inputs={"text": ..., "context": ..., "active_projects": [...]})

What this system does NOT subsume:

- **Post-call semantic validation with tier escalation** (e.g. journal segmenter,
  clarify/verdict_call.call_for_verdict). Different shape — one call + retry on
  validation failure — not decomposition. Future extension.
- **Parallel sub-call execution.** Sub-calls run sequentially, in declaration
  order, so each one's output is available to the next via the ``working`` dict.

See the ``architecture/llm-runner/decomposed-judgment`` knowledge unit for the
mental model and design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from work_buddy.config import load_config
from work_buddy.llm.response import ErrorKind, LLMResponse, TierAttempt
from work_buddy.llm.runner_v2 import LLMRunner
from work_buddy.llm.tiers import ModelTier
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


# Default escalation kinds for sub-calls (and main calls). Includes
# SCHEMA_VIOLATION so a structured-output failure escalates before the
# soft-fail-default kicks in. Callers can override via the ``escalate_on``
# field on SubCall / MainCall.
_DEFAULT_ESCALATE_ON: tuple[ErrorKind, ...] = (
    ErrorKind.TIMEOUT,
    ErrorKind.EMPTY_CONTENT,
    ErrorKind.RATE_LIMITED,
    ErrorKind.BACKEND_UNAVAILABLE,
    ErrorKind.SCHEMA_VIOLATION,
    ErrorKind.MODEL_NOT_AVAILABLE,
    ErrorKind.MODEL_UNSUPPORTED,
)


# Last-resort fallback dials when no ``config_key`` is set or the config
# path resolves to nothing. Mirrors clarify/config.py:TRIAGE_DEFAULTS["segment"]
# fallback values so a brand-new SubCall doesn't crash in environments where
# the corresponding config block hasn't been wired up yet.
_FALLBACK_TIER_CHAIN: tuple[str, ...] = ("local_fast", "frontier_fast")
_FALLBACK_MAX_TOKENS: int = 1024
_FALLBACK_TEMPERATURE: float = 0.0
_FALLBACK_CACHE_TTL_MINUTES: int = 0


@dataclass(frozen=True)
class SubCall:
    """Declarative description of one sub-LLM call within a decomposition.

    Carries only invariants — the prompt, schema, fail policy, and a
    ``config_key`` pointer to where operational dials live. Tier chain,
    cache TTL, max_tokens, and temperature are resolved from config at
    call time so they're tunable in ``config.local.yaml`` without code
    changes.

    Attributes:
        name: Identifier used as the trace_id segment, the key in the
            ``working`` dict (so the main call can reference it as
            ``working["<name>"]``), and as the audit-record key.
        system_prompt: The static system prompt string.
        user_prompt: Callable taking the current ``working`` dict and
            returning the user prompt string. Lets the prompt access
            both the original inputs and any prior sub-call outputs.
        output_schema: JSON Schema dict for structured output. Anthropic
            strict structured-output mode is supported; if you need
            ``minimum`` / ``maximum`` constraints, validate them in a
            separate Python step (see ``project_picker._validate_candidates``).
        config_key: Dotted config path resolving to a dict with optional
            keys ``tier_chain``, ``max_tokens``, ``temperature``,
            ``cache_ttl_minutes``. Missing keys fall back to the framework
            defaults. Set to ``None`` to use the framework defaults
            wholesale (rare — almost every SubCall should declare a config_key
            so the deployment can tune it).
        escalate_on: Error kinds that trigger tier escalation within
            ``LLMRunner.call``. Defaults to the standard retryables.
        fail_policy: ``"soft"`` (default) means a failed sub-call substitutes
            ``soft_fail_default`` and lets the chain continue. ``"hard"``
            short-circuits the chain.
        soft_fail_default: Required when ``fail_policy="soft"``. The dict
            that becomes ``working[name]`` if every tier exhausts.
    """

    name: str
    system_prompt: str
    user_prompt: Callable[[dict[str, Any]], str]
    output_schema: dict[str, Any]
    config_key: str | None = None
    escalate_on: tuple[ErrorKind, ...] = _DEFAULT_ESCALATE_ON
    fail_policy: Literal["soft", "hard"] = "soft"
    soft_fail_default: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("SubCall.name must be a non-empty string.")
        if self.fail_policy not in ("soft", "hard"):
            raise ValueError(
                f"SubCall.fail_policy must be 'soft' or 'hard'; got {self.fail_policy!r}"
            )
        if self.fail_policy == "soft" and self.soft_fail_default is None:
            raise ValueError(
                f"SubCall {self.name!r}: fail_policy='soft' requires "
                "soft_fail_default to be set (a dict that substitutes for "
                "the sub-call output when every tier exhausts). Pass an "
                "empty dict if no fields are needed."
            )
        if not callable(self.user_prompt):
            raise ValueError(
                f"SubCall {self.name!r}: user_prompt must be callable "
                "(takes the working-dict, returns the user prompt string)."
            )


@dataclass(frozen=True)
class MainCall:
    """Declarative description of the main judgment call.

    Like :class:`SubCall` but always treated as ``hard`` — the main call
    is the deliverable; if it fails, the whole judgment fails.

    Attributes:
        name: Trace-id segment. The main call's output is the
            :attr:`DecomposedResult.main` field; it is NOT keyed into
            ``working``.
        system_prompt: The static system prompt.
        user_prompt: Callable taking the ``working`` dict (which by now
            contains all sub-call outputs under their names) and returning
            the user prompt string.
        output_schema: JSON Schema dict for structured output.
        config_key: Dotted config path; same semantics as ``SubCall.config_key``.
        escalate_on: Error kinds that trigger tier escalation. Defaults
            to the standard retryables. Set to e.g.
            ``(ErrorKind.VALIDATION_FAILED,)`` if the main call has a
            post-parse semantic validator that pushes
            ``error_kind=VALIDATION_FAILED`` into the response.
    """

    name: str
    system_prompt: str
    user_prompt: Callable[[dict[str, Any]], str]
    output_schema: dict[str, Any]
    config_key: str | None = None
    escalate_on: tuple[ErrorKind, ...] = _DEFAULT_ESCALATE_ON

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("MainCall.name must be a non-empty string.")
        if not callable(self.user_prompt):
            raise ValueError(
                f"MainCall {self.name!r}: user_prompt must be callable."
            )


@dataclass(frozen=True)
class SubcallAudit:
    """Per-sub-call audit record returned in :attr:`DecomposedResult.sub_audits`."""

    name: str
    output: dict[str, Any] | None
    tier_used: str
    elapsed_ms: int
    cached: bool
    failed_softly: bool
    error_kind: ErrorKind | None = None
    error: str | None = None


@dataclass(frozen=True)
class SubcallResult:
    """Return shape from :func:`run_subcall`.

    Use ``ok`` to gate downstream logic; ``output`` is always populated
    (with ``soft_fail_default`` if ``ok`` is False under soft policy).
    """

    name: str
    ok: bool
    output: dict[str, Any]                # always populated
    tier_used: str
    elapsed_ms: int
    cached: bool
    failed_softly: bool                   # True when soft-fail substituted
    error_kind: ErrorKind | None = None
    error: str | None = None
    tier_attempts: tuple[TierAttempt, ...] = ()


@dataclass(frozen=True)
class DecomposedResult:
    """Return shape from :meth:`DecomposedJudgment.run`.

    ``main`` is the main call's :class:`LLMResponse`, or ``None`` if a
    hard sub-call failed before the main could run (in which case
    ``exhausted_step`` names the failing sub-call).
    """

    name: str                                       # the chain name, for tracing
    main: LLMResponse | None
    sub_audits: dict[str, SubcallAudit] = field(default_factory=dict)
    exhausted_step: str | None = None
    tier_attempts: tuple[TierAttempt, ...] = ()

    def is_error(self) -> bool:
        if self.exhausted_step is not None:
            return True
        if self.main is None:
            return True
        return self.main.is_error()


@dataclass(frozen=True)
class DecomposedJudgment:
    """Bundle of N sub-calls + 1 main call running as one declarative pipeline.

    See module docstring for usage. The pipeline is:

    1. Start ``working = dict(inputs)``.
    2. For each sub-call (sequential, in declaration order):
       - Build user prompt from ``working``.
       - Invoke :meth:`LLMRunner.call` with the resolved tier chain.
       - On success: ``working[sub.name] = resp.structured_output``.
       - On error (soft): ``working[sub.name] = sub.soft_fail_default``.
       - On error (hard): short-circuit; main call is skipped.
    3. Build main user prompt from ``working`` (now contains all sub outputs).
    4. Invoke main call. Return :class:`DecomposedResult`.
    """

    name: str
    sub_calls: tuple[SubCall, ...]
    main: MainCall

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("DecomposedJudgment.name must be non-empty.")
        seen: set[str] = set()
        for sub in self.sub_calls:
            if sub.name in seen:
                raise ValueError(
                    f"DecomposedJudgment {self.name!r}: duplicate sub_call "
                    f"name {sub.name!r}. Each sub_call name must be unique "
                    "within a chain (it's used as the working-dict key)."
                )
            seen.add(sub.name)
        if self.main.name in seen:
            raise ValueError(
                f"DecomposedJudgment {self.name!r}: main.name {self.main.name!r} "
                "collides with a sub_call name."
            )

    def run(
        self,
        inputs: dict[str, Any],
        *,
        trace_id: str | None = None,
        runner: LLMRunner | None = None,
    ) -> DecomposedResult:
        runner = runner or _default_runner()
        working: dict[str, Any] = dict(inputs)
        sub_audits: dict[str, SubcallAudit] = {}
        all_attempts: list[TierAttempt] = []

        for sub in self.sub_calls:
            step_trace = _compose_trace_id(self.name, sub.name, trace_id)
            sr = run_subcall(sub, working, trace_id=step_trace, runner=runner)

            sub_audits[sub.name] = SubcallAudit(
                name=sub.name,
                output=sr.output if sr.ok or sr.failed_softly else None,
                tier_used=sr.tier_used,
                elapsed_ms=sr.elapsed_ms,
                cached=sr.cached,
                failed_softly=sr.failed_softly,
                error_kind=sr.error_kind,
                error=sr.error,
            )
            all_attempts.extend(sr.tier_attempts)

            if not sr.ok and not sr.failed_softly:
                # Hard fail: stop here.
                logger.warning(
                    "DecomposedJudgment %r: hard-fail at sub-call %r "
                    "(%s); main call skipped.",
                    self.name, sub.name, sr.error_kind,
                )
                return DecomposedResult(
                    name=self.name,
                    main=None,
                    sub_audits=sub_audits,
                    exhausted_step=sub.name,
                    tier_attempts=tuple(all_attempts),
                )

            # Both happy path AND soft-fail: keep going. The soft-fail
            # default is already in sr.output.
            working[sub.name] = sr.output

        # All sub-calls done. Run the main call.
        main_trace = _compose_trace_id(self.name, self.main.name, trace_id)
        main_resp = _call_one(
            name=self.main.name,
            system_prompt=self.main.system_prompt,
            user_prompt=self.main.user_prompt(working),
            output_schema=self.main.output_schema,
            config_key=self.main.config_key,
            escalate_on=self.main.escalate_on,
            trace_id=main_trace,
            runner=runner,
        )
        all_attempts.extend(main_resp.tier_attempts)

        return DecomposedResult(
            name=self.name,
            main=main_resp,
            sub_audits=sub_audits,
            exhausted_step=None,
            tier_attempts=tuple(all_attempts),
        )


# ---------------------------------------------------------------------------
# Single-sub-call entry point
# ---------------------------------------------------------------------------


def run_subcall(
    sub: SubCall,
    inputs: dict[str, Any],
    *,
    trace_id: str | None = None,
    runner: LLMRunner | None = None,
) -> SubcallResult:
    """Execute one :class:`SubCall` against an inputs dict.

    Use this for callers that want only the sub-call piece, without
    bundling a main call. The deadline pre-pass is the canonical example —
    it's a sub-call whose output is consumed by an existing verdict
    pipeline that's not (yet) wrapped in a :class:`DecomposedJudgment`.
    """
    runner = runner or _default_runner()
    user = sub.user_prompt(inputs)

    resp = _call_one(
        name=sub.name,
        system_prompt=sub.system_prompt,
        user_prompt=user,
        output_schema=sub.output_schema,
        config_key=sub.config_key,
        escalate_on=sub.escalate_on,
        trace_id=trace_id,
        runner=runner,
    )
    elapsed_ms = sum(a.elapsed_ms for a in resp.tier_attempts) or 0

    if resp.is_error():
        if sub.fail_policy == "soft":
            assert sub.soft_fail_default is not None  # validated in __post_init__
            return SubcallResult(
                name=sub.name,
                ok=False,
                output=dict(sub.soft_fail_default),
                tier_used=resp.tier_used or "",
                elapsed_ms=elapsed_ms,
                cached=resp.cached,
                failed_softly=True,
                error_kind=resp.error_kind,
                error=resp.error,
                tier_attempts=resp.tier_attempts,
            )
        # Hard fail.
        return SubcallResult(
            name=sub.name,
            ok=False,
            output={},
            tier_used=resp.tier_used or "",
            elapsed_ms=elapsed_ms,
            cached=resp.cached,
            failed_softly=False,
            error_kind=resp.error_kind,
            error=resp.error,
            tier_attempts=resp.tier_attempts,
        )

    return SubcallResult(
        name=sub.name,
        ok=True,
        output=dict(resp.structured_output or {}),
        tier_used=resp.tier_used or "",
        elapsed_ms=elapsed_ms,
        cached=resp.cached,
        failed_softly=False,
        tier_attempts=resp.tier_attempts,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_RUNNER: LLMRunner | None = None


def _default_runner() -> LLMRunner:
    """Module-level singleton — matches the deadline_extract / verdict pattern."""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = LLMRunner()
    return _RUNNER


def _call_one(
    *,
    name: str,
    system_prompt: str,
    user_prompt: str,
    output_schema: dict[str, Any] | None,
    config_key: str | None,
    escalate_on: tuple[ErrorKind, ...],
    trace_id: str | None,
    runner: LLMRunner,
) -> LLMResponse:
    """Resolve config dials, build the LLMRunner.call invocation, dispatch."""
    dials = _resolve_dials(config_key)
    tier_chain = dials["tier_chain"]
    max_tokens = dials["max_tokens"]
    temperature = dials["temperature"]
    cache_ttl_minutes = dials["cache_ttl_minutes"]

    if not tier_chain:
        # Empty chain — config explicitly set to []. Guard against the
        # caller silently no-op-ing; surface as a clear error.
        raise ValueError(
            f"Sub/main call {name!r}: tier_chain resolved to empty "
            f"(config_key={config_key!r}). Pass at least one tier."
        )

    primary = tier_chain[0]
    fallbacks = list(tier_chain[1:])

    try:
        return runner.call(
            tier=primary,
            system=system_prompt,
            user=user_prompt,
            output_schema=output_schema,
            escalate_on=list(escalate_on),
            escalate_to=fallbacks,
            max_tokens=max_tokens,
            temperature=temperature,
            cache_ttl_minutes=cache_ttl_minutes,
            trace_id=trace_id,
        )
    except Exception as exc:  # noqa: BLE001 — convert any backend exception into a soft failure
        # Convert raw exceptions to a synthetic error response so callers
        # don't have to wrap every call in try/except. The legacy
        # deadline_extract had its own try/except around runner.call
        # specifically for unknown-tier ValueError and network errors;
        # this preserves that behavior at the framework level.
        logger.warning(
            "decomposed: %s call %r raised at primary tier %s: %s; "
            "returning synthetic backend_unavailable response",
            "main" if name else "sub", name, primary, exc,
        )
        return LLMResponse(
            tier_used=primary.value if hasattr(primary, "value") else str(primary),
            error=str(exc),
            error_kind=ErrorKind.BACKEND_UNAVAILABLE,
        )


def _resolve_dials(config_key: str | None) -> dict[str, Any]:
    """Resolve operational dials from a dotted config path.

    Returns a dict with keys: ``tier_chain`` (list[ModelTier]),
    ``max_tokens`` (int), ``temperature`` (float),
    ``cache_ttl_minutes`` (int).

    Lookup order: ``load_config()`` walked by dotted path → in-code
    fallbacks. Unknown tier names are dropped with a warning.
    """
    block: dict[str, Any] = {}
    if config_key:
        try:
            cfg = load_config() or {}
        except Exception as exc:
            logger.warning(
                "decomposed: load_config() failed (%s); using fallback dials.",
                exc,
            )
            cfg = {}
        block = _walk_dotted(cfg, config_key) or {}

    # Distinguish "key absent" (use fallback) from "key explicitly []"
    # (loud error at the call site so a misconfigured config doesn't
    # silently no-op the call).
    raw_chain = block.get("tier_chain")
    if raw_chain is None:
        raw_chain = list(_FALLBACK_TIER_CHAIN)
    tier_chain: list[ModelTier] = []
    for entry in raw_chain:
        if isinstance(entry, ModelTier):
            tier_chain.append(entry)
            continue
        try:
            tier_chain.append(ModelTier(entry))
        except (ValueError, TypeError):
            logger.warning(
                "decomposed: ignoring unknown tier %r in tier_chain "
                "(config_key=%r).", entry, config_key,
            )

    return {
        "tier_chain": tier_chain,
        "max_tokens": int(block.get("max_tokens", _FALLBACK_MAX_TOKENS)),
        "temperature": float(block.get("temperature", _FALLBACK_TEMPERATURE)),
        "cache_ttl_minutes": int(
            block.get("cache_ttl_minutes", _FALLBACK_CACHE_TTL_MINUTES)
        ),
    }


def _walk_dotted(d: dict[str, Any], path: str) -> dict[str, Any] | None:
    """Walk a dotted config path. Returns the final dict or None."""
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur if isinstance(cur, dict) else None


def _compose_trace_id(chain: str, step: str, caller_trace: str | None) -> str:
    """Build the per-step trace_id used by LLMRunner's escalation log.

    Format: ``"<chain>::<step>::<caller-trace>"`` so a single grep on
    the escalation log can scope to one chain or one step within a chain.
    """
    return f"{chain}::{step}::{caller_trace or '-'}"


__all__ = [
    "SubCall",
    "MainCall",
    "DecomposedJudgment",
    "SubcallResult",
    "SubcallAudit",
    "DecomposedResult",
    "run_subcall",
]
