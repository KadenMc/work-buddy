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

One-time procedure for making LM Studio available for work-buddy's document-side passage encoder. The execution policy then chooses local-only, remote-with-local-fallback, or remote-required behavior.

## What this gets you

The passage encoder (`snowflake-arctic-embed-m-v1.5`, ~110M params, approximately
526 MB of weights) can run through LM Studio's `/v1/embeddings` endpoint, which may
place the model on a remote compute device via LM Link. In **Require LM Studio** mode,
the local embedding process never loads those document weights. **Prefer LM Studio**
retains deliberate local fallback. The process-private-commit reduction is not a fixed
526 MB: runtime buffers vary, and allocator high-water commit can remain until the
embedding process restarts.

## What this does NOT touch

- Query encoding: the smaller latency-sensitive encoders remain local.
- Search-consumer routing: this changes where document vectors are computed, not which
  index answers a search.
- The LLM stack: LM Studio's chat endpoints are unrelated to this policy.

All document callers still use work-buddy's local `/embed` endpoint. That endpoint is
the policy and observability boundary and then invokes either LM Studio or the local
provider.

## Prerequisites

- LM Studio installed on the machine that will serve the embeddings (main machine, or via LM Link from a remote compute device).
- Network access to HuggingFace for the initial GGUF download.
- Enough free memory and disk for the one-time drift comparison against the local
  sentence-transformers model.

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

### 5. Configure the endpoint and model alias

Edit `config.yaml` (or your `config.local.yaml`) to identify the transport and the exact
LM Studio model. `provider` and `on_error` also supply the first-run Settings default;
after a profile value exists, the Settings record is authoritative for execution mode.

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
      # Bootstrap the Settings policy as Prefer LM Studio.
      provider: lmstudio
      lmstudio_model: "text-embedding-snowflake-arctic-embed-m-v1.5"
      # fallback -> Prefer LM Studio; fail -> Require LM Studio.
      on_error: fallback
```

Then open **System → Embeddings** and choose:

- **Prefer LM Studio** to use the remote model with intentional local fallback.
- **Require LM Studio** to prevent any local document-model load and fail document
  embedding closed when the endpoint or an actual remote request is unavailable.
- **Local only** to keep document execution in work-buddy.

### 6. Restart the embedding service

The saved choice is restart-gated. Restart the embedding service (normally by restarting
the sidecar) and refresh the Embeddings page. The startup validator reports whether the
endpoint is reachable and whether the configured model ID is currently advertised:

```
LM Studio provider for embedding.models.leaf-ir verified — model
'text-embedding-snowflake-arctic-embed-m-v1.5' is loaded at http://localhost:1234.
```

### 7. Let scheduled indexes converge

Legacy IR refreshes and consolidated-index refreshes both cross the same `/embed` policy
boundary. Their next eligible runs use the active provider for new or changed documents.
Existing vectors remain compatible because the verified quantization drift is negligible;
offloading does not itself require rebuilding or deleting an index.

## Verification

- **System → Embeddings** reports the active policy from service health, independently
  from the configured/effective Settings values.
- **LM Studio** reads Reachable. **Configured remote model observation** may read
  Advertised now; if it is not advertised, LM Studio may still load it on demand, so the
  first actual document batch is the definitive route check.
- After one document batch, **Last document batch** reports LM Studio with no fallback.
- In Require mode, **Local document weights** remains Not loaded. In Prefer mode it may
  become loaded after a remote failure, by design.
- A model-alias configuration error or Settings-authority startup error appears as a
  hard alert before the first batch. On an authority read failure, document execution is
  forced remote and fail-closed so stale YAML cannot load the local passage model.

## Rollback

Choose **Local only**, save, and restart the embedding service. The endpoint and model
alias may remain configured for a later switch; they do not cause remote execution while
Local only is active. Existing LM-Studio-sourced vectors remain valid because the audited
remote model shares the local model's vector space.

## Troubleshooting

### "LM Studio not reachable" at startup

Check: (1) `curl http://<base_url>/v1/models` — is the server actually running? (2) Did you override `lmstudio.base_url` but not point it at a reachable address? (3) On LM Studio with LM Link to a remote device: is the link connected? (LM Studio's Connections panel on the main machine will say.)

### "not currently advertised by LM Studio" at startup

This is an observation, not a definitive failure: LM Studio can load a configured model
on demand. If the first document request fails, run `curl <base_url>/v1/models`, verify
the exact model id (usually prefixed `text-embedding-`), and compare it with
`lmstudio_model` in config.

### Drift test reports MARGINAL or FAIL

Do NOT proceed until you understand why. Common causes, ordered by likelihood: (1) Wrong model loaded in LM Studio — verify the id matches. (2) GGUF has bad pooling metadata — run `audit_lmstudio_gguf.py` again; if pooling_type is not CLS, pick a different quant. (3) Baseline model failed to load cleanly — delete the HF cache and retry. (4) LM Studio running a different `/v1/embeddings` implementation (self-hosted fork or a very old version) — upgrade LM Studio.

## Related code

- `work_buddy/embedding/providers/lmstudio.py` — provider module.
- `work_buddy/index/encode.py::ProviderRouter`: policy resolution, fallback, and fail-closed routing.
- `work_buddy/embedding/providers/lmstudio_config.py`: lightweight endpoint/model configuration for probes.
- `work_buddy/ir/dense.py::_encode_bulk_direct`: legacy IR document caller through `/embed`.
- `work_buddy/embedding/service.py::_validate_lmstudio_providers` — startup validator.
- `work_buddy/health/components.py` — `lmstudio` ComponentDef and `embedding.soft_depends_on`.
- `work_buddy/health/requirements.py` — `services/lmstudio/reachable` setup-time requirement (severity: recommended; fix_kind: agent_handoff).
