---
name: LM Studio Embedding Offload Setup
kind: directions
description: Procedure for offloading work-buddy's document-side passage encoder to LM Studio — download GGUF, verify metadata, run drift test, update config.
summary: 'Terminal-only procedure per machine: (0) install LM Studio + expose `lms` CLI; (1) `lms get -y "https://huggingface.co/yixuan-chia/snowflake-arctic-embed-m-v1.5-Q8_0-GGUF"` (full HF URL, NOT slug — slug fails CLI name-regex); (2) `scripts/audit_lmstudio_gguf.py` to verify pooling=CLS and architecture=bert; (3) `lms server start` to bring up the server; (4) `scripts/verify_lmstudio_embedding.py --mode all` for the drift test; (5) set embedding.models.<key>.provider=lmstudio in config; (6) restart sidecar; (7) ir-index-rebuild cron converges automatically.'
trigger: user wants to offload the embedding passage encoder to LM Studio, or asks how to set up LM Studio for embeddings
capabilities:
- setup_wizard
tags:
- lmstudio
- embedding
- gguf
- offload
- setup
- arctic-embed
- directions
aliases:
- lmstudio embedding setup
- offload embeddings to lmstudio
- gguf audit procedure
- embedding drift test
- setup lmstudio embedding
- configure lmstudio passage encoder
parents:
- features
- features
---

# LM Studio Embedding Offload — Setup

One-time procedure for offloading work-buddy's document-side passage encoder to LM Studio. The default sentence-transformers path remains a fallback — this procedure is additive, not a migration.

## What this gets you

The passage encoder (`snowflake-arctic-embed-m-v1.5`, ~110M params, ~500 MB RSS) normally loads into the work-buddy embedding service on the main machine. With offload enabled, bulk document encoding routes to LM Studio's `/v1/embeddings` endpoint instead — which can forward to a remote compute device via LM Link. Net effect: ~500 MB of model RSS no longer pinned on the main machine, without touching query latency (queries stay local).

## What this does NOT touch

- Query encoding — small, latency-sensitive, always local.
- The online `/embed` endpoint on the work-buddy embedding service — same reason.
- The LLM stack — LM Studio's chat endpoints are unrelated to this config.

## Prerequisites

- LM Studio installed on the machine that will serve the embeddings (main machine, or via LM Link from a remote compute device).
- Network access to HuggingFace for the initial GGUF download.
- The user's existing ir-index is healthy — if the current passage encoder is stalling, fix that first (task `t-ea501359` covers the ir-index cold-start checkpointing fix).

## Procedure

### 0. Install LM Studio + the `lms` CLI (once per machine)

If LM Studio isn't already on the target machine, install it and expose the CLI. The `lms` binary is what makes the rest of this procedure terminal-only (no GUI steps once it's installed):

- **Windows** (PowerShell):

  ```powershell
  winget install LMStudio.LMStudio
  # Open LM Studio once → System tab → "Install CLI" so `lms` is on PATH.
  ```

- **macOS** (any shell):

  ```bash
  brew install --cask lm-studio
  # Open LM Studio once → System tab → "Install CLI" so `lms` is on PATH.
  ```

- **Linux**: download the AppImage from <https://lmstudio.ai>, run it once, System tab → "Install CLI".

Verify:

```bash
lms --version   # should print a version string, no errors
```

### 1. Download the verified Q8_0 GGUF

One command, cross-platform. Use the **full HuggingFace URL** (not the slug), then `-y` to auto-approve:

```bash
lms get -y "https://huggingface.co/yixuan-chia/snowflake-arctic-embed-m-v1.5-Q8_0-GGUF"
```

`lms get` drops the file into the correct `~/.lmstudio/models/<publisher>/<repo>/` layout automatically. Recommended quant: `yixuan-chia/snowflake-arctic-embed-m-v1.5-Q8_0-GGUF` — audited on the reference machine with measured drift of 0.0002 cosine vs fp32.

**Gotcha: don't use the bare slug form** (`lms get -y yixuan-chia/snowflake-arctic-embed-m-v1.5-Q8_0-GGUF` without the URL). LM Studio's CLI applies a name-validator regex that rejects dots (`v1.5`), underscores (`Q8_0`), and uppercase (`GGUF`) in the path segments when the argument is treated as an LM-Studio-catalog shortname. The HuggingFace-URL code path bypasses that validator. Observed empirically on a fresh install (LM Studio CLI commit `0b2a176` — the slug form errored with `validation: regex, path: target.name`).

If you need to build the GGUF yourself (different model, newer quantization), use HuggingFace's `gguf-my-repo` Space on the source `sentence-transformers` repo. The conversion runs in ~5 minutes and produces a file with correct pooling metadata automatically. Then `lms import <path-to-gguf>` to register it.

**Fallback if `lms get` from URL also fails** (older LM Studio versions): download via `huggingface-cli` and then `lms import`:

```bash
pip install huggingface_hub    # if not already installed
huggingface-cli download yixuan-chia/snowflake-arctic-embed-m-v1.5-Q8_0-GGUF \
    --local-dir /tmp/gguf-cache
lms import /tmp/gguf-cache/snowflake-arctic-embed-m-v1.5-q8_0.gguf
# lms import prompts for publisher attribution — pick yixuan-chia from the list.
```

### 2. Audit the GGUF metadata

Verify the GGUF was converted with the correct pooling mode and architecture before using it. A mismatched GGUF produces silently-wrong vectors — the endpoint works, the numbers look plausible, but retrieval quality falls off a cliff.

```bash
# From the work-buddy repo root:
python scripts/audit_lmstudio_gguf.py ~/.lmstudio/models/yixuan-chia/snowflake-arctic-embed-m-v1.5-Q8_0-GGUF/snowflake-arctic-embed-m-v1.5-q8_0.gguf
```

Windows PowerShell equivalent:

```powershell
python scripts\audit_lmstudio_gguf.py "$env:USERPROFILE\.lmstudio\models\yixuan-chia\snowflake-arctic-embed-m-v1.5-Q8_0-GGUF\snowflake-arctic-embed-m-v1.5-q8_0.gguf"
```

Required verdicts:

- `architecture: bert` (the llama.cpp BERT embedding path supports the sentence-transformers encoders we use)
- `bert.pooling_type: 2 (CLS)` — Arctic-embed and mdbr-leaf use CLS. MEAN or NONE means the GGUF was converted wrong; refuse it and find a different quant.
- `bert.embedding_length: 768` — matches our index dimensionality. A different value means the GGUF is from a different model entirely.

If the audit prints `FAIL`, do NOT proceed — pick a different repo from the HuggingFace search results for `snowflake-arctic-embed-m-v1.5 gguf` and audit again.

### 3. Start LM Studio's local server

```bash
lms server start          # default port 1234
curl http://127.0.0.1:1234/v1/models
```

The `/v1/models` response should list an id starting with `text-embedding-` for the GGUF you just downloaded. Note the exact id — you'll put it in config as `lmstudio_model` in step 5. (LM Studio may lazy-load on first request; if it isn't in the list yet, `lms load text-embedding-snowflake-arctic-embed-m-v1.5` forces eager load.)

### 4. Run the drift test

Before flipping any config, verify the GGUF produces vectors numerically compatible with the sentence-transformers baseline:

```bash
python scripts/verify_lmstudio_embedding.py --mode all
```

The script encodes 30 representative texts through both paths (fp32 sentence-transformers and Q8 LM Studio) and reports per-pair cosine similarity.

Interpretation:

- **`PASS` (mean ≥ 0.98, nothing below 0.95)** — offload is safe. Proceed to step 5. Observed drift in the reference machine: 0.9998 mean, 0.9997 min.
- **`MARGINAL` (mean 0.95–0.98, or outliers)** — usually a tokenization edge case. Inspect the `LOW` pairs. Proceed only if you understand why those specific texts drift.
- **`FAIL` (mean < 0.95)** — stop. Something is wrong. Most common causes: wrong model loaded in LM Studio; GGUF converted with bad pooling; the baseline model isn't loading correctly. Fix before continuing.

### 5. Configure work-buddy

Edit `config.yaml` (or your `config.local.yaml`):

```yaml
# Optional top-level — defaults to http://localhost:1234 if omitted.
lmstudio:
  base_url: "http://localhost:1234"

embedding:
  models:
    leaf-ir:
      name: "MongoDB/mdbr-leaf-ir-asym"
      dims: 768
      eager: false
      # Opt in to LM Studio for bulk document encoding.
      provider: lmstudio
      lmstudio_model: "text-embedding-snowflake-arctic-embed-m-v1.5"
      # fallback: silently fall back to sentence_transformer on LM Studio
      #   errors. Observed drift is 0.0002 cosine, so mixed-provenance
      #   vectors in the same index cluster correctly.
      # fail: re-raise the error. Use for research workflows that
      #   require single-provenance vectors.
      on_error: fallback
```

### 6. Restart the sidecar

The embedding service reads the provider config at startup. Restart it so the new config takes effect. Watch the logs — the startup validator will print either a verification line or a WARN if LM Studio or the model id look wrong:

```
LM Studio provider for embedding.models.leaf-ir verified — model
'text-embedding-snowflake-arctic-embed-m-v1.5' is loaded at http://localhost:1234.
```

### 7. Let ir-index-rebuild converge

The next scheduled `ir-index-rebuild` cron run (every 5 minutes by default) will start using the new provider for any new or changed documents. Existing vectors stay on disk and continue to match because the drift is negligible. There is no need for a one-shot re-encode unless you specifically want single-provenance vectors — in which case, delete `~/.claude/projects/work_buddy_ir.<source>.*.npz` and let the cron rebuild from scratch.

## Verification

- Settings page: LM Studio appears as a reachable external component. The embedding component shows `lmstudio` under soft dependencies with the "falls back to local sentence-transformers" note.
- Embedding service logs: bulk encodes print `via LM Studio` instead of the `Encoded N/N documents...` progress line.
- Task Manager / Activity Monitor: the passage encoder model is no longer loaded by the work-buddy embedding service process.

## Rollback

To disable offloading, remove the three lines (`provider`, `lmstudio_model`, `on_error`) from the `leaf-ir` model config and restart the sidecar. The sentence_transformers path resumes without any other cleanup. Existing LM-Studio-sourced vectors stay valid — they're numerically compatible with the fp32 baseline, so there's no index corruption.

## Troubleshooting

### "LM Studio not reachable" at startup

Check: (1) `curl http://<base_url>/v1/models` — is the server actually running? (2) Did you override `lmstudio.base_url` but not point it at a reachable address? (3) On LM Studio with LM Link to a remote device: is the link connected? (LM Studio's Connections panel on the main machine will say.)

### "not in LM Studio's loaded models" at startup

The configured `lmstudio_model` id doesn't match what LM Studio is serving. Run `curl <base_url>/v1/models` and copy the exact id (usually prefixed `text-embedding-`) into config.

### Drift test reports MARGINAL or FAIL

Do NOT proceed until you understand why. Common causes, ordered by likelihood: (1) Wrong model loaded in LM Studio — verify the id matches. (2) GGUF has bad pooling metadata — run `audit_lmstudio_gguf.py` again; if pooling_type is not CLS, pick a different quant. (3) Baseline model failed to load cleanly — delete the HF cache and retry. (4) LM Studio running a different `/v1/embeddings` implementation (self-hosted fork or a very old version) — upgrade LM Studio.

## Related code

- `work_buddy/embedding/providers/lmstudio.py` — provider module.
- `work_buddy/ir/dense.py::_encode_bulk_direct` — dispatch.
- `work_buddy/embedding/service.py::_validate_lmstudio_providers` — startup validator.
- `work_buddy/health/components.py` — `lmstudio` ComponentDef and `embedding.soft_depends_on`.
- `work_buddy/health/requirements.py` — `services/lmstudio/reachable` setup-time requirement (severity: recommended; fix_kind: agent_handoff).
