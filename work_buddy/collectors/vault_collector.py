"""Collect contract-relevant vault context via the native vault index.

For each active contract, runs a hybrid semantic search of the indexed
vault(s) using the contract's Claim (or title) as the query and renders the
top matching chunks as markdown. Runs entirely in work-buddy's own processes
against the disk-backed index, so it works whether or not Obsidian is open.
"""
from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_MAX_CONTRACTS = 3
_TOP_K = 5
_MIN_QUERY_CHARS = 5


def collect(cfg: dict[str, Any]) -> str:
    """Render contract-relevant vault notes via the native index.

    Returns an empty string when there are no active contracts or the index
    yields nothing — the bundle pipeline skips empty sections.
    """
    from work_buddy.contracts import active_contracts, get_contracts_dir
    from work_buddy.vault_index.search import search

    try:
        contracts = active_contracts(get_contracts_dir())
    except Exception as exc:
        logger.debug("vault_collector: loading contracts failed: %s", exc)
        return ""

    if not contracts:
        return ""

    lines: list[str] = ["## Contract-Relevant Vault Content", ""]
    found_any = False

    for contract in contracts[:_MAX_CONTRACTS]:
        path = contract.get("path")
        title = contract.get("title") or (path.stem if path is not None else "contract")
        claim = (contract.get("sections") or {}).get("Claim", "")
        query = (claim or title or "").strip()
        if len(query) < _MIN_QUERY_CHARS:
            continue

        try:
            results = search(query, top_k=_TOP_K, method="hybrid")
        except Exception as exc:
            logger.debug("vault_collector: search for %r failed: %s", title, exc)
            continue

        if not results:
            continue

        found_any = True
        lines.append(f"### {title}")
        lines.append(f'*Query: "{query[:80]}"*')
        lines.append("")
        for r in results:
            src = (r.get("metadata") or {}).get("source_path") or r.get("doc_id", "?")
            score = r.get("score", 0.0)
            lines.append(f"- `{src}` ({score:.3f})")
        lines.append("")

    return "\n".join(lines) if found_any else ""
