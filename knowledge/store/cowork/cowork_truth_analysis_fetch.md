---
name: Co-work Truth Analysis Fetch
kind: capability
description: Fetch one server-admitted search hit through the guarded public-network boundary and persist an exact run-owned source receipt.
capability_name: cowork_truth_analysis_fetch
category: cowork
op: op.wb.cowork_truth_analysis_fetch
schema_version: wb-capability/v1
parameters:
  run_id:
    type: str
    description: Server-issued Truth-analysis run id. It must match the gateway-injected worker session.
    required: true
  hit_id:
    type: str
    description: Stable opaque hit id returned by cowork_truth_analysis_search for this exact run; arbitrary URLs are not accepted.
    required: true
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- cowork
- truth
- analysis
- fetch
- constrained-agent
aliases:
- fetch truth analysis source
- fetch admitted evidence lead
- capture bounded web source
parents:
- cowork
---

Authority comes from the exact `<run-id>-cowork-truth-analysis` transport
session and the run-owned admitted `hit_id`. There is deliberately no URL
parameter. A hit from another run, an invented ID, or a terminal run cannot
widen this capability into a general fetch surface. At most five admitted hits
may be fetched in one run.

Before each request and redirect, the broker permits only HTTP or HTTPS,
rejects credentials and local names, resolves every destination, and rejects
the entire destination if any answer is private, loopback, link-local,
multicast, reserved, or otherwise non-public. The socket is pinned to a
validated public address while TLS verification and the Host header retain the
original hostname, preventing a second DNS lookup from reopening the boundary.
Only standard port 80 for HTTP and port 443 for HTTPS are allowed. Fetches are
bounded to five redirects, twenty seconds total, ten seconds per request, and
512 KiB of identity-encoded response content. Non-identity content encoding is
rejected.

A successful receipt preserves the requested and final source URLs, title,
exact captured text and digest, redirect chain, HTTP and media metadata,
extractor, provider, and acquisition limits. Model-facing captured text is
bounded to 64 KiB of UTF-8; when a longer extracted source is cut, the receipt
records the full extracted byte count and digest, captured byte count and
digest, and `text_truncated=true`. A completed receipt may later support an
exact `web_fetch` evidence candidate, but remains external quarantined source
material until the human selects an exact passage. It is not a fact.

The model worker's $2.00 hard ceiling is separate from fetch-provider behavior.
Web search and fetch currently have no enforced monetary ceiling; the URL,
port, redirect, time, byte, and per-run limits are safety and activity bounds,
not a promise about provider charges.

Replaying the same admitted hit returns the original fetch receipt without
network egress. A stale pending operation becomes non-retryable
`research_outcome_unknown`; the broker never guesses whether an interrupted
request completed and never silently repeats uncertain egress.
