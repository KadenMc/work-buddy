---
name: Hindsight Memory Server
kind: integration
description: External Hindsight server providing semantic memory storage and reflection
external_system: Hindsight
bridge_module: work_buddy.memory
tags:
- memory
- hindsight
- external-service
- pgvector
- llm
aliases:
- hindsight
- hindsight-api
- hindsight-api-slim
- hindsight-client
parents:
- memory
- memory
---

Hindsight is the external memory server backing the work-buddy memory subsystem (see `memory`). It provides storage, semantic search, retention, and LLM-powered reflection over a personal memory bank.

## Server components

- `hindsight-api-slim` — the HTTP server, managed via Poetry
- `hindsight-client` — the Python SDK, managed via Poetry
- External PostgreSQL with the `pgvector` extension (for semantic search)

## Configuration

Required environment variables for the Hindsight API:

- `HINDSIGHT_API_LLM_PROVIDER` — typically `anthropic`
- `HINDSIGHT_API_LLM_API_KEY` — provider API key (Anthropic key for the default config)
- `HINDSIGHT_API_LLM_MODEL` — e.g. `claude-haiku-4-5-20251001`

## Starting the server

```
HINDSIGHT_API_LLM_PROVIDER=anthropic \
HINDSIGHT_API_LLM_API_KEY=$ANTHROPIC_API_KEY \
HINDSIGHT_API_LLM_MODEL=claude-haiku-4-5-20251001 \
hindsight-api
```

## Costs

Every `memory_write` call costs LLM tokens (the Hindsight server runs an LLM-side embedding + summarization pass on retain). `memory_read` is cheap (embedding similarity only — no token cost beyond the input). `memory_reflect` is the most expensive — it runs LLM reasoning over the recall set.
