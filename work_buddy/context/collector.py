"""Fetch raw context from registered :class:`ContextSource` implementations.

:class:`ContextCollector` is stateless — it dispatches by source name
to the process-wide registry. Each source gets one call per request,
gated by the cache: if a cached section exists, is within
``max_age_seconds``, and the source's ``is_stale()`` says no, we skip
the fetch. Otherwise we call ``source.collect()``, write the result
to the cache atomically, and return it.

Failures in one source don't abort the whole request — the collector
logs and skips. Callers see a :class:`Context` whose ``sections`` dict
omits the failing source; use :meth:`Context.has` to branch on
availability.
"""

from __future__ import annotations

from typing import Iterable

from work_buddy.context import cache as cache_mod
from work_buddy.context import registry
from work_buddy.context.types import (
    Context,
    ContextRequest,
    ContextSection,
    ContextSource,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class ContextCollector:
    """Fetch raw sections for a :class:`ContextRequest`.

    One instance per caller is fine — the collector holds no state.
    The only cross-call state lives on disk (the cache) and in the
    :mod:`work_buddy.context.registry` module.
    """

    def collect(self, request: ContextRequest) -> Context:
        """Fetch every requested source. Cache-aware.

        Logic per source:

        1. Compute ``bucket = cache.bucket_key(source, request)``.
        2. If ``request.max_age_seconds is not None`` and
           ``cache.is_fresh_enough(source, bucket, max_age_seconds)``
           AND ``not source.is_stale(cached, request)`` → return
           cached section, no fetch.
        3. Otherwise call ``source.collect(request)``, write to cache,
           return the fresh section.

        A source raising from ``collect()`` is caught, logged, and
        omitted from the returned ``Context``. Cache-read failures
        fall through to a fresh fetch.
        """
        target_sources = _resolve_targets(request)
        ctx = Context(request=request)

        for name in target_sources:
            source = registry.get(name)
            if source is None:
                logger.warning("ContextCollector: unknown source %r; skipping", name)
                continue

            try:
                section = self._collect_one(source, request)
            except Exception as exc:
                logger.exception(
                    "ContextCollector: source %r raised; omitting from context", name,
                )
                continue

            if section is not None:
                ctx.sections[name] = section

        return ctx

    # -- internal -----------------------------------------------------------

    def _collect_one(
        self,
        source: ContextSource,
        request: ContextRequest,
    ) -> ContextSection | None:
        """Cache-aware fetch for one source. Returns None on exception."""
        name = source.name
        bucket = cache_mod.bucket_key(name, request)

        # Fast path: cached + fresh-enough + source says not stale.
        if cache_mod.is_fresh_enough(name, bucket, request.max_age_seconds):
            cached = cache_mod.read_cached(name, bucket)
            if cached is not None:
                try:
                    stale = source.is_stale(cached, request)
                except Exception:
                    logger.exception(
                        "ContextCollector: source %r.is_stale raised; forcing refetch",
                        name,
                    )
                    stale = True
                if not stale:
                    logger.debug(
                        "ContextCollector: cache hit for %r (bucket=%s)",
                        name, bucket,
                    )
                    return cached

        # Fetch fresh.
        section = source.collect(request)
        try:
            cache_mod.write_cached(section, bucket)
        except Exception:
            # Cache write failures are non-fatal — we still return the
            # freshly-fetched section to the caller.
            logger.exception(
                "ContextCollector: cache write failed for %r (bucket=%s)",
                name, bucket,
            )
        return section


def _resolve_targets(request: ContextRequest) -> Iterable[str]:
    """Apply request.sources + request.exclude against the registry."""
    if request.sources is not None:
        return list(request.sources)
    excluded = set(request.exclude or ())
    return [n for n in registry.names() if n not in excluded]
