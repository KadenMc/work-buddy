"""A/B harness: consolidated knowledge index vs the live knowledge index (oracle).

Builds the ``knowledge`` partition into the SEPARATE ``db/index-consolidated`` (never
touches the live indexes' data), then runs a fixed query set through BOTH the live
``knowledge.search`` (oracle) and the new ``UnifiedIndex``, reporting overlap@k, top-1
agreement, and rank correlation. This is the F-EVAL evidence that gates any future
deletion of the bespoke index — it deletes nothing.

Run:  python -m work_buddy.index.ab   → writes .data/designs/index-consolidation/AB-RESULTS.md
"""

from __future__ import annotations

import time
from typing import Any

# Representative knowledge queries: subsystem/capability terms, dev-doc-scan vocabulary,
# alias-shaped phrasings, and how/why questions.
QUERIES = [
    "task triage",
    "consent system grants",
    "embedding service dense vectors",
    "morning routine",
    "vault writer section",
    "how to add a workflow",
    "reciprocal rank fusion",
    "broker admission priority",
    "knowledge store search index",
    "obsidian bridge",
    "dev document scan",
    "artifact lifecycle cleanup",
    "weekly task review",
    "contracts and stop rules",
    "chrome tab triage",
    "calendar provider",
    "websearch jina",
    "entity registry resolve",
]


def _oracle(query: str, top_n: int) -> list[str]:
    """Live knowledge index ranking (paths), system scope — the oracle."""
    from work_buddy.knowledge.search import search
    res = search(query=query, knowledge_scope="system", top_n=top_n, depth="index")
    return [r["path"] for r in (res.get("results") or [])]


def _new(ui, query: str, top_k: int) -> list[str]:
    """Consolidated index ranking (paths), system scope."""
    from work_buddy.index.model import Query
    hits = ui.search(
        Query(text=query, top_k=top_k, filters={"scope": "system"}),
        partitions=["knowledge"],
    )
    out = []
    for h in hits:
        out.append(h.doc_id.split(":", 1)[1] if ":" in h.doc_id else h.doc_id)
    return out


def _metrics(oracle: list[str], new: list[str], k: int) -> dict[str, Any]:
    sa, sb = set(oracle[:k]), set(new[:k])
    overlap = (len(sa & sb) / k) if k else 0.0
    top1 = 1.0 if (oracle and new and oracle[0] == new[0]) else 0.0
    common = list(sa & sb)
    rank_corr: float | None = None
    if len(common) >= 2:
        import numpy as np
        ra = [oracle.index(d) for d in common]
        rb = [new.index(d) for d in common]
        if np.std(ra) > 0 and np.std(rb) > 0:
            rank_corr = round(float(np.corrcoef(ra, rb)[0, 1]), 3)  # Spearman (rank Pearson)
        else:
            rank_corr = 1.0
    return {"overlap": round(overlap, 3), "top1": top1, "common": len(common), "rank_corr": rank_corr}


def run_ab(queries: list[str] | None = None, *, top_k: int = 10, build: bool = True) -> dict[str, Any]:
    from work_buddy.index.config import IndexConfig, load_index_config
    from work_buddy.index.partitioned import UnifiedIndex

    base = load_index_config()
    cfg = IndexConfig(enabled=True, db_path=base.db_path, partitions=base.partitions)
    ui = UnifiedIndex(config=cfg)

    build_stats = None
    build_secs = None
    if build:
        t0 = time.time()
        build_stats = ui.build("knowledge")
        build_secs = round(time.time() - t0, 1)

    queries = queries or QUERIES
    rows = []
    for q in queries:
        oracle = _oracle(q, top_k)
        new = _new(ui, q, top_k)
        rows.append({"query": q, "oracle": oracle, "new": new, **_metrics(oracle, new, top_k)})

    agg = _aggregate(rows)
    return {"build_stats": build_stats, "build_secs": build_secs, "rows": rows, "aggregate": agg, "top_k": top_k}


def _aggregate(rows: list[dict]) -> dict[str, Any]:
    n = len(rows) or 1
    overlaps = [r["overlap"] for r in rows]
    corrs = [r["rank_corr"] for r in rows if r["rank_corr"] is not None]
    return {
        "queries": len(rows),
        "mean_overlap": round(sum(overlaps) / n, 3),
        "top1_agreement": round(sum(r["top1"] for r in rows) / n, 3),
        "mean_rank_corr": round(sum(corrs) / len(corrs), 3) if corrs else None,
        "queries_with_empty_new": sum(1 for r in rows if not r["new"]),
    }


def render_md(result: dict[str, Any]) -> str:
    a = result["aggregate"]
    lines = [
        "# Consolidated Index — A/B vs live knowledge index (oracle)",
        "",
        "**Off the hot path.** Built the `knowledge` partition into the separate "
        "`db/index-consolidated`; the live indexes were not modified. This is F-EVAL "
        "evidence — nothing was deleted or re-pointed.",
        "",
        f"- build: {result.get('build_stats')}  ({result.get('build_secs')}s)",
        f"- top_k: {result['top_k']}",
        "",
        "## Aggregate",
        f"- queries: **{a['queries']}**",
        f"- mean overlap@{result['top_k']}: **{a['mean_overlap']}**",
        f"- top-1 agreement: **{a['top1_agreement']}**",
        f"- mean rank-corr (on shared docs): **{a['mean_rank_corr']}**",
        f"- queries where the new index returned nothing: **{a['queries_with_empty_new']}**",
        "",
        "## Per-query",
        "",
        "| query | overlap | top1 | shared | rank_corr | oracle[0] | new[0] |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in result["rows"]:
        o0 = r["oracle"][0] if r["oracle"] else "—"
        n0 = r["new"][0] if r["new"] else "—"
        lines.append(
            f"| {r['query']} | {r['overlap']} | {int(r['top1'])} | {r['common']} | "
            f"{r['rank_corr']} | {o0} | {n0} |"
        )
    lines += ["", "## Divergences (oracle top-5 vs new top-5)", ""]
    for r in result["rows"]:
        if r["overlap"] < 0.6:
            lines.append(f"- **{r['query']}**")
            lines.append(f"  - oracle: {r['oracle'][:5]}")
            lines.append(f"  - new:    {r['new'][:5]}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    from work_buddy.paths import repo_root
    out_dir = repo_root() / ".data" / "designs" / "index-consolidation"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = run_ab()
    md = render_md(result)
    (out_dir / "AB-RESULTS.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n[written to {out_dir / 'AB-RESULTS.md'}]")
