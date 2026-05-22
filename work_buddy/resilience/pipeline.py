"""ResiliencePipeline, builder, and registry — composing strategies.

A pipeline is an ordered composition of strategies plus the classification
config for a guarded call. Build one with ``ResiliencePipelineBuilder``,
reuse it across calls (its strategies' state — circuit breakers, bulkheads —
is shared, which is the point), and optionally register it by name in the
process-global ``ResiliencePipelineRegistry``.

See ``.data/designs/resilience-framework/DESIGN.md`` §7.
"""

from __future__ import annotations

import threading
from typing import Awaitable, Callable, TypeVar, Union

from work_buddy.resilience.deadline import Deadline
from work_buddy.resilience.outcome import Outcome
from work_buddy.resilience.seam import (
    Classifier,
    ResultClassifier,
    ResilienceStrategy,
    default_classify,
    guarded_call,
)
from work_buddy.resilience.strategies import (
    BulkheadStrategy,
    CircuitBreakerStrategy,
    FallbackStrategy,
    RateLimiterStrategy,
    RetryStrategy,
    TimeoutStrategy,
)

T = TypeVar("T")


class ResiliencePipeline:
    """An ordered composition of strategies, executed via ``guarded_call``.

    Strategies run outermost-first in declaration order. Reuse one pipeline
    across calls — its strategies' state is shared, which is the point of a
    circuit breaker or bulkhead.
    """

    def __init__(
        self,
        strategies: list[ResilienceStrategy],
        *,
        name: str = "pipeline",
        classify: Classifier = default_classify,
        result_classifier: ResultClassifier | None = None,
        passthrough_exceptions: tuple[type[BaseException], ...] = (),
    ) -> None:
        self.name = name
        self._strategies = list(strategies)
        self._classify = classify
        self._result_classifier = result_classifier
        self._passthrough = passthrough_exceptions

    async def execute(
        self,
        fn: Callable[[], Union[T, Awaitable[T]]],
        *,
        operation_key: str | None = None,
        deadline: Deadline | None = None,
    ) -> Outcome:
        """Run ``fn`` through this pipeline.

        ``operation_key`` defaults to the pipeline's name.
        """
        return await guarded_call(
            operation_key or self.name,
            fn,
            deadline=deadline,
            strategies=self._strategies,
            classify=self._classify,
            result_classifier=self._result_classifier,
            passthrough_exceptions=self._passthrough,
        )

    @property
    def strategies(self) -> list[ResilienceStrategy]:
        """A copy of the strategy list, outermost-first."""
        return list(self._strategies)


class ResiliencePipelineBuilder:
    """Fluent builder for a :class:`ResiliencePipeline`.

    Strategy-adding calls accumulate in declaration order = outermost-first.
    Recommended order (DESIGN §7): overall ``timeout`` → ``rate_limiter`` /
    ``bulkhead`` → ``retry`` → ``circuit_breaker`` → per-attempt ``timeout``.
    ``build()`` is terminal.
    """

    def __init__(self, name: str = "pipeline") -> None:
        self._name = name
        self._strategies: list[ResilienceStrategy] = []
        self._classify: Classifier = default_classify
        self._result_classifier: ResultClassifier | None = None
        self._passthrough: tuple[type[BaseException], ...] = ()

    def add(self, strategy: ResilienceStrategy) -> "ResiliencePipelineBuilder":
        """Append a pre-built strategy (for custom strategies)."""
        self._strategies.append(strategy)
        return self

    def timeout(self, timeout_s: float) -> "ResiliencePipelineBuilder":
        return self.add(TimeoutStrategy(timeout_s))

    def retry(self, **kwargs: object) -> "ResiliencePipelineBuilder":
        return self.add(RetryStrategy(**kwargs))  # type: ignore[arg-type]

    def circuit_breaker(
        self, **kwargs: object,
    ) -> "ResiliencePipelineBuilder":
        return self.add(CircuitBreakerStrategy(**kwargs))  # type: ignore[arg-type]

    def bulkhead(self, **kwargs: object) -> "ResiliencePipelineBuilder":
        return self.add(BulkheadStrategy(**kwargs))  # type: ignore[arg-type]

    def rate_limiter(self, **kwargs: object) -> "ResiliencePipelineBuilder":
        return self.add(RateLimiterStrategy(**kwargs))  # type: ignore[arg-type]

    def fallback(
        self, fn: Callable[[], object],
    ) -> "ResiliencePipelineBuilder":
        return self.add(FallbackStrategy(fn))

    def classify(
        self, classifier: Classifier,
    ) -> "ResiliencePipelineBuilder":
        self._classify = classifier
        return self

    def result_classifier(
        self, rc: ResultClassifier,
    ) -> "ResiliencePipelineBuilder":
        self._result_classifier = rc
        return self

    def passthrough(
        self, *excs: type[BaseException],
    ) -> "ResiliencePipelineBuilder":
        self._passthrough = excs
        return self

    def build(self) -> ResiliencePipeline:
        return ResiliencePipeline(
            self._strategies,
            name=self._name,
            classify=self._classify,
            result_classifier=self._result_classifier,
            passthrough_exceptions=self._passthrough,
        )


class ResiliencePipelineRegistry:
    """Named pipelines, lazily instantiated and cached.

    ``register`` takes a factory; ``get`` builds it on first access and
    caches the instance so its strategies' state is shared across all
    callers that ask for that name.
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], ResiliencePipeline]] = {}
        self._cache: dict[str, ResiliencePipeline] = {}
        self._lock = threading.Lock()

    def register(
        self, name: str, factory: Callable[[], ResiliencePipeline],
    ) -> None:
        """Register a pipeline factory under ``name``. Re-registering
        invalidates any cached instance."""
        with self._lock:
            self._factories[name] = factory
            self._cache.pop(name, None)

    def get(self, name: str) -> ResiliencePipeline:
        """Return the named pipeline, building + caching it on first access.

        Raises ``KeyError`` if no factory is registered for ``name``.
        """
        with self._lock:
            cached = self._cache.get(name)
            if cached is not None:
                return cached
            factory = self._factories.get(name)
            if factory is None:
                raise KeyError(
                    f"no resilience pipeline registered as {name!r}"
                )
            pipeline = factory()
            self._cache[name] = pipeline
            return pipeline

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._factories)


_REGISTRY: ResiliencePipelineRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_pipeline_registry() -> ResiliencePipelineRegistry:
    """The process-global pipeline registry."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = ResiliencePipelineRegistry()
        return _REGISTRY


def _reset_pipeline_registry_for_tests() -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None
