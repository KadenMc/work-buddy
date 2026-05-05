"""Filter routine probe traffic out of Werkzeug access logs.

The sidecar's HealthMonitor probes ``/health`` every 5 s and the message
poller hits ``/messages`` every 15 s. With Werkzeug's default access log
those two endpoints alone produce ~23 k lines/day per service, which is
the dominant cause of multi-hundred-MB ``<data_root>/runtime/service_logs/*.log``
files. The probes are by design and carry no diagnostic value, so we
silence them at the source.

Errors and non-probe traffic are unaffected — only successful GET/HEAD
requests to the listed endpoints are dropped.
"""

from __future__ import annotations

import logging
import re


def install_probe_log_filter(probe_paths: list[str]) -> None:
    """Drop Werkzeug access-log lines for the given probe endpoints.

    Matches the standard Werkzeug access-log format::

        127.0.0.1 - - [05/May/2026 03:14:15] "GET /health HTTP/1.1" 200 -

    The path is followed by either a query string (``?…``) or
    whitespace before ``HTTP/1.1``; both forms are matched.

    Args:
        probe_paths: list of URL paths to silence (e.g. ``["/health"]``).
    """
    if not probe_paths:
        return
    # Path must be followed by '?' (query-string poll) or whitespace
    # (bare GET /health). Earlier versions of this regex required '?'
    # or 'HTTP' immediately after the path, which missed the bare form.
    pattern = re.compile(
        r'"(GET|HEAD) (' + "|".join(re.escape(p) for p in probe_paths) + r')[?\s]'
    )

    class _ProbeFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return not pattern.search(record.getMessage())

    logging.getLogger("werkzeug").addFilter(_ProbeFilter())
