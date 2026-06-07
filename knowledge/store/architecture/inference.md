---
name: Inference
kind: concept
description: Local inference subsystems — admission control, LLM backends, embedding service, and provider dispatch for LM Studio / LM Link.
summary: 'Parent grouping for local-inference subsystems: the LocalInferenceBroker (admission + priority + metrics), LLM backends (lmstudio_native, openai_compat), and the embedding provider path. Currently sparse; architecture/llm-runner, architecture/llm-with-tools, and architecture/embedding-service will move under this parent in a follow-up restructure (see task t-<pending>).'
tags:
- inference
- lmstudio
- lm-link
- broker
- admission-control
- local-inference
- llm
- embedding
aliases:
- local inference
- inference broker
- lmstudio routing
- lm link
- admission control
parents:
- architecture
- architecture
---

Local-inference subsystems — everything that decides **when**, **where**, and **how** a call to a local model happens.

## What lives here

- **[Broker](architecture_inference_broker.md)** — ``work_buddy.inference.broker.LocalInferenceBroker``. Per-profile slot limits, priority classes (INTERACTIVE / WORKFLOW / BACKGROUND), split queue-wait vs inference timeouts, and per-call metrics. Every outbound local-inference call (embedding or LLM) routes through it so work-buddy — not LM Studio — is the scheduler of record.
- **[Provenance](architecture_inference_provenance.md)** — ``work_buddy.llm.provenance.record_inference_call``. A stable ``call_id`` + plain-text description for every model call — local **and** cloud, completions **and** embeddings — written beside the cost ledger and surfaced as the Settings › Inference activity feed. Where the broker decides *when/where* a local call runs, provenance records *what* called a model and *why* across both providers; the two join by ``call_id`` so local rows carry their scheduler latency.

## What WILL live here (pending restructure)

Today these are flat siblings under ``architecture/``; a follow-up PR (``docs_move`` pass) will re-home them under ``architecture/inference/`` alongside the broker:

- ``architecture/llm-runner`` — unified LLM entry point + tier dispatch.
- ``architecture/llm-with-tools`` — legacy tool-call loop (kept for MCP-exposed capability).
- ``architecture/embedding-service`` — the Flask service on port 5124 + asymmetric / symmetric model registry.

Until that restructure lands, follow the flat-path links in ``architecture`` parent for those three.

## Why group these

All four concerns (broker, LLM calls, embedding calls, runner) share the same underlying infrastructure: LM Studio on localhost:1234 (possibly routing via LM Link to remote compute), per-profile slot accounting, and the same error vocabulary (``LocalInferenceError`` + its kinds in ``work_buddy/llm/backends/_errors.py``). Keeping them together makes it easy to find the right entry point for a new inference-adjacent feature without scanning a flat architecture/ index.
