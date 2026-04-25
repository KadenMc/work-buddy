"""Anthropic pricing rates — the canonical table for the whole repo.

Vendored verbatim from ``claude-usage`` (MIT, Pawel Huryn, April 2026).
The structure splits cache_read / cache_write rates out of the generic
input/output rate so cost computation faithfully reproduces Anthropic's
published pricing.

The rates here describe **dollars per 1 million tokens**.
:func:`calc_cost` returns dollars.

As of the 2026-04-25 pricing consolidation, both consumers share this
table:

* :func:`work_buddy.llm.cost.log_call` writes ``estimated_cost_usd`` per
  API call against this table (with cache_read / cache_creation token
  splits captured from the response).
* :func:`work_buddy.llm.claude_code_usage.aggregator.get_claude_code_usage_summary`
  uses it to cost the transcript-derived turn data.

The migration ``scripts/migrate_priced_with_v2.py`` stamped every
pre-consolidation row with ``priced_with: "v2"``. Cost numbers did not
change because legacy rows lack the cache_read / cache_creation token
data needed to apply cache rates retroactively.
"""

from __future__ import annotations


PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-7": {"input": 3.00,  "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.00,  "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-7":  {"input": 1.00,  "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
    "claude-haiku-4-6":  {"input": 1.00,  "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
    "claude-haiku-4-5":  {"input": 1.00,  "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
}


def get_pricing(model: str | None) -> dict[str, float] | None:
    """Resolve a pricing dict for ``model`` using the upstream strategy.

    1. Exact match.
    2. Prefix match (e.g. ``claude-sonnet-4-6-extended-context`` falls
       back to ``claude-sonnet-4-6``).
    3. Keyword fallback (``opus``/``sonnet``/``haiku``) → the most recent
       family member.
    4. ``None`` for anything else (custom / non-Anthropic / local).
    """
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    m = model.lower()
    if "opus" in m:
        return PRICING["claude-opus-4-7"]
    if "sonnet" in m:
        return PRICING["claude-sonnet-4-6"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    return None


def is_billable(model: str | None) -> bool:
    """A model is billable if its name suggests an Anthropic frontier family."""
    if not model:
        return False
    m = model.lower()
    return ("opus" in m) or ("sonnet" in m) or ("haiku" in m)


def calc_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Estimate USD cost using the published Anthropic rates.

    Cache reads are billed at 90% off; cache creations carry a 25%
    premium on the base input rate.
    """
    if not is_billable(model):
        return 0.0
    p = get_pricing(model)
    if p is None:
        return 0.0
    return (
        input_tokens          * p["input"]       / 1_000_000
        + output_tokens       * p["output"]      / 1_000_000
        + cache_read_tokens   * p["cache_read"]  / 1_000_000
        + cache_creation_tokens * p["cache_write"] / 1_000_000
    )
