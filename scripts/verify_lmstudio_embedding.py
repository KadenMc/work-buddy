"""Verify LM Studio's embedding endpoint against the sentence-transformers baseline.

Runs the documented drift test for the work-buddy LM Studio offload
path (``docs/handbook/features_lmstudio-offload-setup.md``). Encodes
the same set of texts through both:

1. The local sentence-transformers model (fp32 baseline).
2. LM Studio's ``/v1/embeddings`` endpoint (Q8 GGUF or whatever is
   loaded).

Then reports per-pair cosine similarity plus aggregate stats. Mean
cosine ≥ 0.98 is the go/no-go threshold — higher is better. Observed
drift in the reference machine: 0.9998 mean, 0.9997 min.

Usage:
    # 30-text default suite (curated for work-buddy's IR corpus)
    python scripts/verify_lmstudio_embedding.py --mode all

    # Custom texts (one per line)
    python scripts/verify_lmstudio_embedding.py --mode all --texts my_texts.txt

    # Split modes (useful if LM Studio isn't ready yet)
    python scripts/verify_lmstudio_embedding.py --mode baseline
    python scripts/verify_lmstudio_embedding.py --mode lmstudio
    python scripts/verify_lmstudio_embedding.py --mode compare

Configuration via flags (defaults match the standard offload setup):
    --base-url    http://127.0.0.1:1234
    --lm-model    text-embedding-snowflake-arctic-embed-m-v1.5
    --hf-model    Snowflake/snowflake-arctic-embed-m-v1.5
    --cache-dir   data/lmstudio-drift-test  (relative to repo root)

Exit code:
    0 — PASS (mean cosine ≥ 0.98, no pairs below 0.95)
    1 — MARGINAL (mean cosine 0.95–0.98, or some pairs between 0.95 and 0.98)
    2 — FAIL (mean cosine < 0.95 — investigate before proceeding)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "lmstudio-drift-test"


# 30 representative texts spanning work-buddy's IR corpus: conversations,
# journal entries, task descriptions, code/doc prose, error messages,
# short/ambiguous, and research-flavored prose. Written to surface
# distributional drift rather than easy-case behavior.
DEFAULT_TEXTS = [
    # Conversation-like
    "Can you rank my open work-buddy tasks by computational expense?",
    "The MCP gateway restarted three times in the last hour — what's wrong?",
    "I don't want LM Studio to become a required dependency.",
    "How did you do that without me granting consent?",
    "Walk me through the triage pipeline end-to-end.",
    # Journal-like
    "Shipped phase 8 of the LLM refactor. Deprecation notes landed but deletion deferred.",
    "Spent two hours debugging why inline_triage_scan wasn't visible — turned out to be a stale MCP gateway.",
    "Consent blanket leaked after workflow orphaning; all eight task_create calls skipped prompts.",
    "Re-encoded the conversation index with incremental checkpoints; cold start no longer stalls.",
    # Task-description-like
    "Add repo_paths to project schema; enable file-path to project resolution and commit attribution.",
    "Harden MCP-gateway/triage pipeline reliability: per-capability timeout, dispatch logging, registry warmup.",
    "Fix read-modify-write race in _find_and_replace_task_line via app.vault.process() atomic callback.",
    "Offload document-side embedding to LM Studio while keeping sentence_transformers as fallback.",
    "Split sidecar_jobs into system vs user jobs so users can schedule cron tasks without polluting the tracked repo.",
    # Technical / code-adjacent
    "def _encode_bulk_direct(texts: list[str], batch_size: int, kind: str) -> np.ndarray:",
    "CREATE TABLE IF NOT EXISTS projects (slug TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT, ...)",
    "asyncio.wait_for(asyncio.to_thread(entry.callable, **parsed_params), timeout=30.0)",
    'POST /v1/embeddings with body {"input": ["text1", "text2"], "model": "..."}',
    # README / doc-like prose
    "Work-buddy is a personal agent framework built on Claude Code and MCP. It orchestrates tasks, manages workflows, and coordinates across projects.",
    "The knowledge store uses progressive disclosure: index, summary, full. Each unit has parents and children, forming a navigable DAG.",
    "Capabilities declare consent_operations. When a capability runs, the executor checks whether the user has granted matching grants before proceeding.",
    # Error messages / diagnostic lines
    "MCP error -32001: Request timed out",
    "EditorConflict: Tasks plugin rejected write — file is dirty in an open editor",
    "RepositoryNotFoundError: 401 Client Error — Repository Not Found for url https://huggingface.co/api/...",
    "ModuleNotFoundError: No module named 'gguf'",
    # Research-flavored prose
    "Retrieval-augmented generation performance depends critically on the embedding model's alignment with the downstream query distribution.",
    "Arctic-embed uses CLS pooling with an explicit query-side prompt prefix for asymmetric retrieval.",
    "The MTEB benchmark ranks embedding models across 56 tasks spanning retrieval, clustering, and classification.",
    # Short / ambiguous
    "yes",
    "this looks correct",
]


def _load_texts(path: Path | None) -> list[str]:
    if path is None:
        return list(DEFAULT_TEXTS)
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln]


def encode_baseline(texts: list[str], hf_model: str) -> np.ndarray:
    print(f"[baseline] Loading {hf_model} via sentence-transformers...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(hf_model, trust_remote_code=False)
    print(f"[baseline] Encoding {len(texts)} texts (document side — no prompt prefix)...")
    # Document side of asymmetric encoders has no prefix; the library
    # handles CLS pooling + L2 normalization automatically via the
    # model's shipping config.
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        batch_size=8,
    )
    print(f"[baseline] Shape: {vectors.shape}, dtype: {vectors.dtype}")
    return vectors


def encode_lmstudio(
    texts: list[str],
    *,
    base_url: str,
    lm_model: str,
) -> np.ndarray:
    print(
        f"[lmstudio] Posting to {base_url}/v1/embeddings "
        f"with model={lm_model!r}..."
    )
    import httpx
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{base_url}/v1/embeddings",
            json={"input": texts, "model": lm_model},
        )
    if response.status_code != 200:
        print(
            f"[lmstudio] HTTP {response.status_code}: {response.text[:500]}",
            file=sys.stderr,
        )
        response.raise_for_status()

    data = response.json()["data"]
    data.sort(key=lambda d: d.get("index", 0))
    vectors = np.array([d["embedding"] for d in data], dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1)
    print(f"[lmstudio] Shape: {vectors.shape}, dtype: {vectors.dtype}")
    print(
        f"[lmstudio] Norms: mean={norms.mean():.4f}, "
        f"min={norms.min():.4f}, max={norms.max():.4f}"
    )
    # Server-side L2 norm should leave these at ~1.0; normalize defensively.
    if not np.allclose(norms, 1.0, atol=1e-3):
        print("[lmstudio] WARN: vectors not unit-normalized; re-normalizing.")
        vectors = vectors / norms[:, None]
    return vectors


def compare(a: np.ndarray, b: np.ndarray) -> dict:
    assert a.shape == b.shape, f"Shape mismatch: {a.shape} vs {b.shape}"
    cos = np.sum(a * b, axis=1)  # both unit-normalized → cosine == dot
    return {
        "n": len(cos),
        "mean": float(cos.mean()),
        "median": float(np.median(cos)),
        "min": float(cos.min()),
        "max": float(cos.max()),
        "std": float(cos.std()),
        "below_0_98": int((cos < 0.98).sum()),
        "below_0_95": int((cos < 0.95).sum()),
        "per_pair": cos.tolist(),
    }


def print_stats(stats: dict, texts: list[str]) -> str:
    print()
    print("=== Drift stats (cosine, fp32 baseline vs LM Studio GGUF) ===")
    print(f"  n:         {stats['n']}")
    print(f"  mean:      {stats['mean']:.4f}")
    print(f"  median:    {stats['median']:.4f}")
    print(f"  min:       {stats['min']:.4f}")
    print(f"  max:       {stats['max']:.4f}")
    print(f"  std:       {stats['std']:.4f}")
    print(f"  < 0.98:    {stats['below_0_98']} / {stats['n']}")
    print(f"  < 0.95:    {stats['below_0_95']} / {stats['n']}")

    pairs = sorted(zip(stats["per_pair"], texts), key=lambda x: x[0])
    print()
    print("=== Per-pair (sorted ascending) ===")
    for cos, text in pairs[:5]:
        print(f"  {cos:.4f}  [LOW]  {text[:80]!r}")
    if len(pairs) > 8:
        print("  ...")
    for cos, text in pairs[-3:]:
        print(f"  {cos:.4f}  [HIGH] {text[:80]!r}")

    print()
    if stats["mean"] >= 0.98 and stats["below_0_95"] == 0:
        verdict = "PASS"
        msg = "drift within expected Q8-quantization bounds. Safe to offload."
    elif stats["mean"] >= 0.95:
        verdict = "MARGINAL"
        msg = (
            "above the fallback threshold but some outliers. Inspect the "
            "LOW pairs above — usually a tokenization or prefix-handling "
            "edge case. Proceed only if you understand why those "
            "specific texts drift."
        )
    else:
        verdict = "FAIL"
        msg = (
            "drift is too large. Something is configured wrong. Check: "
            "(1) is the right model loaded in LM Studio? (2) does the "
            "GGUF have the right pooling (run audit_lmstudio_gguf.py)? "
            "(3) did the HF baseline model finish loading correctly? "
            "Do NOT proceed with offloading until this is resolved."
        )
    print(f"VERDICT: {verdict} — {msg}")
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["baseline", "lmstudio", "compare", "all"],
        default="all",
        help="Which pass to run. 'all' = baseline → lmstudio → compare.",
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:1234",
        help="LM Studio base URL (no /v1 suffix)",
    )
    parser.add_argument(
        "--lm-model",
        default="text-embedding-snowflake-arctic-embed-m-v1.5",
        help="Model id as exposed by LM Studio's GET /v1/models",
    )
    parser.add_argument(
        "--hf-model",
        default="Snowflake/snowflake-arctic-embed-m-v1.5",
        help="HuggingFace model id for the fp32 baseline",
    )
    parser.add_argument(
        "--texts", type=Path, default=None,
        help="Optional file with one text per line. Default: 30 curated texts.",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
        help="Where to store cached .npz vectors between runs.",
    )
    args = parser.parse_args()

    texts = _load_texts(args.texts)
    cache_dir = args.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    baseline_npz = cache_dir / "vectors_baseline.npz"
    lmstudio_npz = cache_dir / "vectors_lmstudio.npz"

    if args.mode in ("baseline", "all"):
        vecs = encode_baseline(texts, args.hf_model)
        np.savez(
            baseline_npz, vectors=vecs,
            texts=np.array(texts, dtype=object),
        )
        print(f"[baseline] Saved to {baseline_npz}")

    if args.mode in ("lmstudio", "all"):
        vecs = encode_lmstudio(
            texts, base_url=args.base_url, lm_model=args.lm_model,
        )
        np.savez(
            lmstudio_npz, vectors=vecs,
            texts=np.array(texts, dtype=object),
        )
        print(f"[lmstudio] Saved to {lmstudio_npz}")

    if args.mode in ("compare", "all"):
        if not baseline_npz.exists():
            print(
                f"ERROR: missing {baseline_npz}. Run --mode baseline first.",
                file=sys.stderr,
            )
            return 2
        if not lmstudio_npz.exists():
            print(
                f"ERROR: missing {lmstudio_npz}. Run --mode lmstudio first.",
                file=sys.stderr,
            )
            return 2
        a = np.load(baseline_npz, allow_pickle=True)["vectors"]
        b = np.load(lmstudio_npz, allow_pickle=True)["vectors"]
        stats = compare(a, b)
        verdict = print_stats(stats, texts)
        return {"PASS": 0, "MARGINAL": 1, "FAIL": 2}[verdict]

    return 0


if __name__ == "__main__":
    sys.exit(main())
