"""CPU stress-test functions for subprocess isolation testing.

Pure computation, no side effects, no external dependencies.
Used by the ``testing/stress-test`` workflow (defined in knowledge/store/workflows.json) to validate
that auto_run subprocess isolation keeps the MCP server responsive.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any


def compute_primes(limit: int = 100_000) -> dict[str, Any]:
    """Find all primes up to *limit* using a sieve of Eratosthenes.

    Takes ~3-8s for ``limit=1_000_000`` on typical hardware.
    Returns count and timing info (not the full list).
    """
    start = time.monotonic()

    sieve = [True] * (limit + 1)
    sieve[0] = sieve[1] = False
    for i in range(2, int(math.isqrt(limit)) + 1):
        if sieve[i]:
            for j in range(i * i, limit + 1, i):
                sieve[j] = False

    count = sum(sieve)
    largest = max(i for i in range(limit, -1, -1) if sieve[i])
    elapsed = time.monotonic() - start

    return {
        "prime_count": count,
        "largest_prime": largest,
        "limit": limit,
        "elapsed_seconds": round(elapsed, 3),
    }


def compute_checksums(iterations: int = 5_000_000) -> dict[str, Any]:
    """Iterative SHA-256 hashing — linear CPU scaling.

    Alternative stress test when you want predictable duration.
    """
    start = time.monotonic()
    digest = b"seed"
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()

    elapsed = time.monotonic() - start
    return {
        "iterations": iterations,
        "final_hash": digest.hex()[:16],
        "elapsed_seconds": round(elapsed, 3),
    }
