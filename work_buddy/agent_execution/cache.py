"""Small thread-safe TTL cache used by provider probes."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

ValueT = TypeVar("ValueT")


@dataclass(slots=True)
class _Entry(Generic[ValueT]):
    value: ValueT
    expires_at: float


class ProbeCache(Generic[ValueT]):
    """Cache slow installation/auth/model probes for a short bounded window."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._entries: dict[str, _Entry[ValueT]] = {}
        self._lock = threading.RLock()

    def get_or_load(
        self,
        key: str,
        loader: Callable[[], ValueT],
        *,
        refresh: bool = False,
    ) -> ValueT:
        """Return a live entry or load exactly once under the cache lock."""

        with self._lock:
            now = self._clock()
            current = self._entries.get(key)
            if not refresh and current is not None and current.expires_at > now:
                return current.value
            value = loader()
            self._entries[key] = _Entry(
                value=value,
                expires_at=now + self._ttl_seconds,
            )
            return value

    def clear(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)
