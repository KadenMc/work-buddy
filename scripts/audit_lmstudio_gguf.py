"""Audit a GGUF embedding model's metadata before loading it in LM Studio.

The LM Studio embedding offload path (see
``docs/handbook/features_lmstudio-offload-setup.md``) is safe only when
the GGUF on disk was converted with the correct pooling mode and
architecture for the source sentence-transformers model. A mismatched
GGUF produces silently-wrong vectors — queries and documents land in
different cluster geometries, retrieval quality degrades, and nothing
flags it because the endpoint itself works fine.

This script parses the GGUF header (metadata-only, no tensor read) and
prints the fields that matter, with a verdict line for each. Zero
dependencies beyond the Python stdlib — runs on any work-buddy env or
even a plain Python install.

Usage:
    python scripts/audit_lmstudio_gguf.py <path-to.gguf>

Expected output for a valid ``snowflake-arctic-embed-m-v1.5`` Q8 GGUF::

    === Verdicts ===
      architecture: bert  OK
      bert.pooling_type: 2 (CLS)  OK (CLS)
      bert.embedding_length: 768

If pooling_type is absent or not CLS, refuse to use the GGUF and look
for a different quantization (or build one yourself via gguf-my-repo).

Reference: https://github.com/ggml-org/ggml/blob/master/docs/gguf.md
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


# GGUF value-type enum
(
    UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL,
    STRING, ARRAY, UINT64, INT64, FLOAT64,
) = range(13)

_SCALAR_FMT = {
    UINT8: ("B", 1), INT8: ("b", 1),
    UINT16: ("H", 2), INT16: ("h", 2),
    UINT32: ("I", 4), INT32: ("i", 4),
    FLOAT32: ("f", 4), BOOL: ("?", 1),
    UINT64: ("Q", 8), INT64: ("q", 8), FLOAT64: ("d", 8),
}

# Pooling enum from llama.cpp (LLAMA_POOLING_TYPE_*). We care about CLS
# for BERT-based IR encoders; MEAN for nomic/bge; NONE for token-level.
POOLING_NAMES = {
    -1: "UNSPECIFIED",
    0: "NONE",
    1: "MEAN",
    2: "CLS",
    3: "LAST",
    4: "RANK",
}


def _read_string(f) -> str:
    (n,) = struct.unpack("<Q", f.read(8))
    return f.read(n).decode("utf-8", errors="replace")


def _read_value(f, value_type):
    if value_type == STRING:
        return _read_string(f)
    if value_type == ARRAY:
        (arr_type,) = struct.unpack("<I", f.read(4))
        (n,) = struct.unpack("<Q", f.read(8))
        preview = []
        # Sample the first few entries for display; skip the rest.
        for _ in range(min(n, 8)):
            preview.append(_read_value(f, arr_type))
        for _ in range(max(0, n - 8)):
            _read_value(f, arr_type)
        return {"_array_type": arr_type, "_len": n, "_preview": preview}
    if value_type in _SCALAR_FMT:
        fmt, size = _SCALAR_FMT[value_type]
        (v,) = struct.unpack("<" + fmt, f.read(size))
        return v
    raise ValueError(f"Unknown GGUF value type: {value_type}")


def parse_gguf_header(path: Path) -> dict:
    """Return ``{version, tensor_count, kv_count, metadata}``.

    Streams the header only — the tensor table at the end of the file
    is not read, so this is O(1) on file size.
    """
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"GGUF":
            raise ValueError(f"Not a GGUF file: magic={magic!r}")
        (version,) = struct.unpack("<I", f.read(4))
        (tensor_count,) = struct.unpack("<Q", f.read(8))
        (kv_count,) = struct.unpack("<Q", f.read(8))
        metadata: dict = {}
        for _ in range(kv_count):
            key = _read_string(f)
            (value_type,) = struct.unpack("<I", f.read(4))
            metadata[key] = _read_value(f, value_type)
        return {
            "version": version,
            "tensor_count": tensor_count,
            "kv_count": kv_count,
            "metadata": metadata,
        }


def _format_value(v) -> str:
    if isinstance(v, dict) and "_array_type" in v:
        return f"<array len={v['_len']} type={v['_array_type']}>  preview={v['_preview'][:3]}"
    return repr(v)


def audit(path: Path, *, expected_pooling: str = "CLS") -> int:
    """Print a metadata audit for ``path``. Returns an exit code.

    ``0`` — all checks pass (safe to proceed).
    ``1`` — one or more checks failed (DO NOT proceed; pick a different
            quant or rebuild).
    """
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    try:
        header = parse_gguf_header(path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    md = header["metadata"]
    size_mb = path.stat().st_size / 1_000_000
    print(f"File: {path.name} ({size_mb:.1f} MB)")
    print(
        f"GGUF version: {header['version']}, "
        f"tensors: {header['tensor_count']}, "
        f"kv pairs: {header['kv_count']}"
    )
    print()

    print("=== Key metadata ===")
    keys = [
        "general.architecture",
        "general.name",
        "general.quantization_version",
        "general.file_type",
        "general.size_label",
        "general.base_model.0.name",
        "general.base_model.0.repo_url",
        "general.license",
    ]
    keys += sorted(k for k in md if k.startswith("bert."))
    keys += sorted(k for k in md if "pooling" in k.lower())
    keys += [k for k in md if k.startswith("tokenizer.ggml.model")]
    seen = set()
    for k in keys:
        if k in seen or k not in md:
            continue
        seen.add(k)
        print(f"  {k}: {_format_value(md[k])}")

    print()
    print("=== Verdicts ===")
    failures = 0

    arch = md.get("general.architecture")
    if arch == "bert":
        print(f"  architecture: {arch}  OK")
    else:
        print(f"  architecture: {arch!r}  WARN (expected 'bert')")
        failures += 1

    pool_keys = [k for k in md if "pooling" in k.lower()]
    if not pool_keys:
        print("  pooling_type: <NOT PRESENT in metadata>  WARN")
        failures += 1
    else:
        for pk in pool_keys:
            pv = md[pk]
            name = POOLING_NAMES.get(pv, f"<unknown {pv}>")
            ok = name == expected_pooling
            print(
                f"  {pk}: {pv} ({name})  "
                f"{'OK' if ok else f'WARN (expected {expected_pooling})'}"
            )
            if not ok:
                failures += 1

    for dk in (k for k in md if k.endswith(".embedding_length")):
        print(f"  {dk}: {md[dk]}")

    if failures == 0:
        print("\nVerdict: PASS — GGUF is safe to load.")
        return 0
    print(
        f"\nVerdict: FAIL — {failures} check(s) warrant refusing this GGUF. "
        f"Pick a different quantization or rebuild with the correct "
        f"pooling/architecture flags."
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit a GGUF embedding model's metadata before loading in "
            "LM Studio. Prints pooling, architecture, and dimensions."
        ),
    )
    parser.add_argument("path", type=Path, help="Path to the .gguf file")
    parser.add_argument(
        "--expected-pooling", default="CLS",
        choices=["NONE", "MEAN", "CLS", "LAST", "RANK"],
        help=(
            "Expected pooling mode. Default CLS matches Snowflake "
            "Arctic Embed and mdbr-leaf-* models. Use MEAN for "
            "nomic/bge and NONE for token-level models."
        ),
    )
    args = parser.parse_args()
    return audit(args.path, expected_pooling=args.expected_pooling)


if __name__ == "__main__":
    sys.exit(main())
