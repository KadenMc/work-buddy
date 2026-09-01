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


# These source names contain legacy Obsidian branches.  Some also have native
# database routes after their domain seal, so opt-out admission is decided by
# the source rather than suppressing the whole adapter unconditionally.
# Filesystem-native ``vault`` and provider-neutral ``calendar`` are deliberately
# absent from this set.
_OBSIDIAN_APP_SOURCES = frozenset({
    "obsidian",
    "obsidian_tasks",
    "obsidian_wellness",
    "day_planner",
    "datacore",
})


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
        obsidian_opted_out = _obsidian_is_opted_out()

        for name in target_sources:
            source = registry.get(name)
            if source is None:
                logger.warning("ContextCollector: unknown source %r; skipping", name)
                continue
            if (
                obsidian_opted_out
                and name in _OBSIDIAN_APP_SOURCES
                and not _serves_native_without_obsidian(source, request)
            ):
                logger.debug(
                    "ContextCollector: legacy-only source %r disabled by Obsidian "
                    "opt-out; skipping without cache or collector access",
                    name,
                )
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
        """Cache-aware fetch for one source. Returns None on exception.

        Authority-aware sources may expose ``collection_guard(request)``.  A
        legacy Journal guard holds the same SQLite writer barrier as sealing,
        and deliberately spans cache lookup, the filesystem snapshot, and
        atomic cache publication.
        """
        guard_factory = getattr(source, "collection_guard", None)
        if callable(guard_factory):
            try:
                with guard_factory(request):
                    return self._collect_one_admitted(source, request)
            except BaseException:
                # A guard can detect an authority transition on exit, after a
                # fresh section was atomically published.  Remove that bucket
                # before propagating so legacy content cannot survive the
                # failed admission attempt.
                cache_mod.evict(
                    source.name,
                    cache_mod.bucket_key(source.name, request),
                )
                raise
        return self._collect_one_admitted(source, request)

    def _collect_one_admitted(
        self,
        source: ContextSource,
        request: ContextRequest,
    ) -> ContextSection | None:
        """Perform one cache/read/publication sequence after admission."""
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


def _obsidian_is_opted_out() -> bool:
    """Return whether compatibility sources are fenced by preference.

    Preference lookup is intentionally lazy: the context primitives remain
    usable in small/test deployments that do not import the health subsystem.
    An unreadable preference is treated as undecided, preserving the existing
    explicit legacy path rather than silently disabling it.
    """
    try:
        from work_buddy.health.preferences import is_wanted

        return is_wanted("obsidian") is False
    except Exception:
        return False


def _serves_native_without_obsidian(
    source: ContextSource,
    request: ContextRequest,
) -> bool:
    """Ask a mixed source whether its native authority route is available.

    Unknown adapters remain legacy-only.  Authority inspection failures also
    suppress the adapter under opt-out, which is safer than probing a retired
    bridge or archive as an implicit fallback.
    """

    check = getattr(source, "serves_native_without_obsidian", None)
    if not callable(check):
        return False
    try:
        return bool(check(request))
    except Exception:
        logger.exception(
            "ContextCollector: native authority check for %r failed; "
            "legacy route remains suppressed",
            source.name,
        )
        return False
