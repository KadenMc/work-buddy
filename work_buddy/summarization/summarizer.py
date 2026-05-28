"""The `Summarizer` composer — combines `Source × Strategy × Store`.

Construction validates coherence (see `_validate_coherence`). The public
methods are thin wrappers over the orchestrator's `run_refresh` and
single-item `refresh_one` paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from work_buddy.summarization.protocol import (
    DiscoveryWindow,
    IncoherentComposition,
    LLMCaller,
    Provenance,
    Source,
    Store,
    SummaryCapability,
    SummaryNode,
    SummaryStrategy,
)


@dataclass
class RefreshReport:
    """Outcome of a refresh pass — what was summarized, what was skipped, what
    errored. Mirrors the existing conv_obs `refresh_session_summaries` result
    dict so shims can map directly onto it."""

    summarizer: str
    summarized: int = 0
    skipped_fresh: int = 0
    errored: int = 0
    total_candidates: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def to_op_dict(self) -> dict:
        """Map to the `conversation_observability_summarize` op's result shape
        — the four-key dict consumers (sidecar job, MCP capability) expect."""
        return {
            "summarized": self.summarized,
            "skipped_fresh": self.skipped_fresh,
            "errored": self.errored,
            "total_candidates": self.total_candidates,
        }


@dataclass
class Summarizer:
    """The composer. `Source × Strategy × Store`, with construction-time
    coherence checks."""

    name: str
    source: Source
    strategy: SummaryStrategy
    store: Store

    def __post_init__(self) -> None:
        self._validate_coherence()
        # Initial sync — see `_sync_strategy_versions`.
        self._sync_strategy_versions()

    def _sync_strategy_versions(self) -> None:
        """Bridge the strategy's current prompt/schema versions into the
        store so its staleness check evaluates against them.

        Re-called at the start of every `refresh` / `refresh_one` so that
        test-time monkeypatches of `Strategy.prompt_version` are picked up
        (the store would otherwise be stuck with the values from
        construction).
        """
        setter = getattr(self.store, "set_strategy_versions", None)
        if callable(setter):
            setter(
                self.strategy.prompt_version,
                self.strategy.schema_version,
            )

    @property
    def capabilities(self) -> frozenset[SummaryCapability]:
        return (
            self.source.capabilities
            | self.strategy.capabilities
            | self.store.capabilities
        )

    def _validate_coherence(self) -> None:
        s_caps = self.strategy.capabilities
        st_caps = self.store.capabilities
        src_caps = self.source.capabilities

        # LAYERED strategy requires a tree-capable store.
        if (
            SummaryCapability.LAYERED in s_caps
            and SummaryCapability.PERSISTS_TREE not in st_caps
        ):
            raise IncoherentComposition(
                f"Summarizer {self.name!r}: layered strategy "
                f"{self.strategy.name!r} requires a PERSISTS_TREE store, but "
                f"{self.store.name!r} declares "
                f"{sorted(c.value for c in st_caps)}."
            )

        # FLAT strategy requires a store that can persist at least depth-1.
        if SummaryCapability.FLAT in s_caps and not (
            SummaryCapability.PERSISTS_FLAT in st_caps
            or SummaryCapability.PERSISTS_TREE in st_caps
        ):
            raise IncoherentComposition(
                f"Summarizer {self.name!r}: flat strategy "
                f"{self.strategy.name!r} requires a store declaring "
                f"PERSISTS_FLAT or PERSISTS_TREE, but {self.store.name!r} "
                f"declares {sorted(c.value for c in st_caps)}."
            )

        # BATCHED is all-or-nothing across source and strategy.
        s_batched = SummaryCapability.BATCHED in s_caps
        src_batched = SummaryCapability.BATCHED in src_caps
        if s_batched != src_batched:
            raise IncoherentComposition(
                f"Summarizer {self.name!r}: BATCHED must be declared on both "
                f"source and strategy or neither; "
                f"strategy={s_batched}, source={src_batched}."
            )

        # INCREMENTAL strategies require the source to support fresh-tail
        # rendering (`render_from`) and total-turns lookup (`total_turns`).
        # These are duck-typed because the framework Source Protocol stays
        # narrow; the orchestrator's incremental path will assert these
        # methods exist at runtime, but failing at construction is friendlier.
        if SummaryCapability.INCREMENTAL in s_caps:
            missing = [
                m for m in ("render_from", "total_turns")
                if not callable(getattr(self.source, m, None))
            ]
            if missing:
                raise IncoherentComposition(
                    f"Summarizer {self.name!r}: incremental strategy "
                    f"{self.strategy.name!r} requires source "
                    f"{self.source.name!r} to provide methods "
                    f"{missing} (duck-typed)."
                )
            # The store must support `apply_incremental` for the merge step.
            if not callable(getattr(self.store, "apply_incremental", None)):
                raise IncoherentComposition(
                    f"Summarizer {self.name!r}: incremental strategy "
                    f"requires the store to implement `apply_incremental`."
                )

    # ---------------------------------------------------------------- refresh

    def refresh(
        self,
        *,
        days: int = 7,
        max_items: int = 3,
        force: bool = False,
        llm_caller: LLMCaller | None = None,
        profile: str | None = None,
    ) -> RefreshReport:
        """Run a bounded refresh pass."""
        from work_buddy.summarization.orchestrator import (
            default_llm_caller,
            run_refresh,
        )

        self._sync_strategy_versions()
        window = DiscoveryWindow(days=days, max_items=max_items, force=force)
        caller = llm_caller if llm_caller is not None else default_llm_caller()
        return run_refresh(
            self, window=window, llm_caller=caller, profile=profile,
        )

    def refresh_one(
        self,
        item_id: str,
        *,
        force: bool = False,
        freshness_token: object | None = None,
        llm_caller: LLMCaller | None = None,
        profile: str | None = None,
    ) -> SummaryNode | None:
        """Refresh a single item by id (regardless of any discovery window).

        Short-circuits when `force=False` AND a `freshness_token` is provided
        AND `store.is_fresh(item_id, token)` returns True — in which case the
        stored tree is returned via `store.load`. Otherwise renders,
        calls the LLM, parses, and persists.

        On render returning `None` (e.g. an empty session) → returns `None`.
        On LLM or parse error → `store.record_error` is called and `None` is
        returned. Caller can distinguish "no content" from "error" via the
        store's record (status flag).
        """
        from work_buddy.summarization.orchestrator import (
            build_error_provenance,
            build_provenance,
            default_llm_caller,
        )

        self._sync_strategy_versions()

        if not force and freshness_token is not None:
            if self.store.is_fresh(item_id, freshness_token):
                return self.store.load(item_id)

        caller = llm_caller if llm_caller is not None else default_llm_caller()

        # v2 INCREMENTAL path routes through a dedicated module.
        if SummaryCapability.INCREMENTAL in self.strategy.capabilities:
            from work_buddy.summarization.incremental import (
                refresh_one_incremental,
            )
            return refresh_one_incremental(
                self,
                item_id,
                freshness_token=freshness_token,
                llm_caller=caller,
                profile=profile,
            )

        body = self.source.render(item_id)
        if body is None:
            return None

        result = caller.call(
            system=self.strategy.system_prompt,
            user=body,
            output_schema=self.strategy.output_schema,
            profile=profile,
            max_tokens=1024,
            trace_id=f"summarization.{self.name}",
        )

        if result.is_error():
            self.store.record_error(
                item_id,
                result.error or "llm error",
                build_error_provenance(self, profile),
            )
            return None

        try:
            node = self.strategy.parse(result.structured_output, result.content)
        except Exception as exc:
            self.store.record_error(
                item_id,
                f"parse error: {exc}",
                build_provenance(self, result, profile),
            )
            return None

        prov = build_provenance(self, result, profile)
        token = freshness_token if freshness_token is not None else prov.generated_at
        self.store.save(item_id, node, prov, token)
        return node

    # ---------------------------------------------------------------- read

    def get(self, item_id: str) -> SummaryNode | None:
        """Load the stored summary tree for `item_id`, or `None`."""
        return self.store.load(item_id)
