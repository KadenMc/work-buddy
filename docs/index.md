# Work Buddy

**Personal agent framework built on Claude Code and MCP.** Orchestrates tasks, manages workflows, coordinates across projects — so you can focus on your actual work.

---

## What is work-buddy?

work-buddy turns [Claude Code](https://docs.anthropic.com/en/docs/claude-code) into a persistent, workflow-driven system. It gives your AI agent structured multi-step workflows, memory that survives across sessions, and deep integration with the tools you already use — Obsidian, Chrome, Telegram, Google Calendar.

It runs locally, uses your own API keys, and stores everything on your machine.

## Documentation

### Getting Started

- **[README](https://github.com/KadenMc/work-buddy#readme)** — Quick start, installation, and feature overview
- **[Setup Guide](https://github.com/KadenMc/work-buddy/blob/main/SETUP.md)** — Detailed configuration walkthrough
- **[Contributing](https://github.com/KadenMc/work-buddy/blob/main/CONTRIBUTING.md)** — How to extend work-buddy

### Reference

- **[Knowledge Handbook](handbook/index.md)** — Auto-generated reference for all 207 agent-facing units: directions, system docs, capabilities, and workflows
- **[CLAUDE.md](https://github.com/KadenMc/work-buddy/blob/main/CLAUDE.md)** — Agent orientation and MCP gateway reference
- **[Changelog](https://github.com/KadenMc/work-buddy/blob/main/CHANGELOG.md)** — Release history

### Key Concepts

| Concept | Description |
|---------|-------------|
| **Capabilities** | Single functions exposed through the MCP gateway. Discoverable via `wb_search`, executable via `wb_run`. |
| **Workflows** | Multi-step DAGs with dependency ordering, auto-run steps, and persistent state. |
| **Knowledge Store** | Typed JSON registry with hierarchical navigation. Agents query it at runtime. |
| **Consent System** | Session-scoped grants with multi-surface delivery (Obsidian, Telegram, Dashboard). |
| **Sidecar** | Background supervisor managing services, scheduled jobs, and retry queues. |
