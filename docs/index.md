# work-buddy

**The AI assistant that understands how you actually work.**

work-buddy is a local-first personal-agent runtime for knowledge workers, built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Obsidian](https://obsidian.md/). Your notes, tasks, browser tabs, and projects are scattered across tools that a general AI assistant cannot see or coordinate. work-buddy is the missing layer that can. It gives your agent structured multi-step workflows, memory that survives across sessions, deep integration with the tools your work already lives in, and a dashboard that keeps you in the loop.

It runs on your existing Claude Code subscription, with no separate service fees, and everything stays on your machine.

## What you can do

- Start the day with a briefing and a plan drawn from your journal, tasks, calendar, and recent work.
- Turn a screen full of open browser tabs into a short list of decisions.
- Route captured notes into the right place instead of letting a scratchpad grow.
- Keep projects honest with explicit claims, evidence, and stop rules.
- Coordinate long-running agent sessions through threads, notifications, and approvals you can answer from anywhere.

## Get started

work-buddy ships as a native installer that bundles its own Python and wires itself into Claude Code for you. The full walkthrough, including the one prerequisite (Claude Code itself), lives in the [README](https://github.com/KadenMc/work-buddy#readme). If you would rather install from source or start building, [CONTRIBUTING](https://github.com/KadenMc/work-buddy/blob/main/CONTRIBUTING.md) has the developer setup.

## Architecture at a glance

Under the hood, work-buddy is a local gateway that extends Claude Code, a conductor that runs multi-step workflows, a handful of supervised background services, and deep integrations with Obsidian, your calendar, and your browser. The [architecture page](architecture.md) lays out the whole topology in one diagram.

## How the documentation is organized

work-buddy's reference is a **handbook**, and the handbook is generated from the same knowledge store your agent reads at runtime. There is no second copy of the truth to keep in sync. When your agent gains a new capability or workflow, the published docs describe it too.

- **[Handbook](handbook/index.md)**: every capability, workflow, and direction, grouped by domain. The honest "everything" list.
- **[Architecture](architecture.md)**: one human-readable view of how the gateway, conductor, services, and integrations fit together.
- **[CLAUDE.md](https://github.com/KadenMc/work-buddy/blob/main/CLAUDE.md)**: how an agent orients itself inside work-buddy, plus the MCP gateway reference.
- **[Changelog](https://github.com/KadenMc/work-buddy/blob/main/CHANGELOG.md)**: release history.

## Key concepts

| Concept | Description |
|---------|-------------|
| **Capabilities** | Single functions exposed through the MCP gateway. Discoverable via `wb_search`, executable via `wb_run`. |
| **Workflows** | Multi-step graphs with dependency ordering, auto-run steps, and persistent state. |
| **Knowledge store** | The interlinked units work-buddy reads at runtime, and the source these docs are generated from. |
| **Consent system** | Session-scoped approvals delivered to every surface at once (Obsidian, Telegram, dashboard). |
| **Sidecar** | The background supervisor that keeps services, scheduled jobs, and retry queues running. |

## Contributing and community

work-buddy is built to be extended, and contributions of every kind are welcome.

- **Contributing guide**: [CONTRIBUTING.md](https://github.com/KadenMc/work-buddy/blob/main/CONTRIBUTING.md)
- **Questions and ideas**: [GitHub Discussions](https://github.com/KadenMc/work-buddy/discussions)
- **Bugs and feature requests**: [Issues](https://github.com/KadenMc/work-buddy/issues)
- **Support the project**: [GitHub Sponsors](https://github.com/sponsors/KadenMc)

## License

work-buddy is free software under the [GNU General Public License v3.0](https://github.com/KadenMc/work-buddy/blob/main/LICENSE) (`GPL-3.0-only`). Use it, study it, modify it, and share it, and anyone you pass a modified version to receives the same freedoms.
