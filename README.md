<p align="center">
    <img src="docs/logo.svg" width="180" />
</p>

<h1 align="center">work-buddy</h1>

<p align="center">
    Your AI doesn't understand how you actually operate. Meet <b>work-buddy</b>.
</p>

<p align="center">
    <a href="https://docs.work-buddy.ai"><img src="https://img.shields.io/badge/docs-work--buddy.ai-E47150" alt="Docs"></a>
    <a href="https://docs.anthropic.com/en/docs/claude-code"><img src="https://img.shields.io/badge/built%20on-Claude%20Code-D97757" alt="Built on Claude Code"></a>
    <img src="https://img.shields.io/badge/status-beta-yellow" alt="Beta">
    <a href="https://github.com/KadenMc/work-buddy/actions"><img src="https://img.shields.io/github/actions/workflow/status/KadenMc/work-buddy/tests.yml?label=tests" alt="Tests"></a>
    <a href="https://codecov.io/gh/KadenMc/work-buddy"><img src="https://img.shields.io/codecov/c/github/KadenMc/work-buddy?label=coverage" alt="Coverage"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--only-blue" alt="License: GPL-3.0-only"></a>
    <a href="https://github.com/sponsors/KadenMc"><img src="https://img.shields.io/badge/sponsor-%E2%9D%A4-ea4aaa" alt="Sponsor"></a>
</p>

<h3 align="center"><a href="https://work-buddy.ai/">Website</a></h3>

<p align="center">
    <a href="https://docs.work-buddy.ai">Docs</a> &bull;
    <a href="#get-started">Get Started</a> &bull;
    <a href="#how-it-works">How It Works</a> &bull;
    <a href="#documentation">Documentation</a> &bull;
    <a href="#community">Community</a> &bull;
    <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

**work-buddy** is the AI assistant for knowledge workers — a local-first personal-agent runtime built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Obsidian](https://obsidian.md/) that organizes the work *around* the work: backlogged notes, scattered tasks, open browser tabs, and the projects they belong to. It gives your agent structured multi-step workflows, memory that survives across sessions, deep integration with the tools your work already lives in, and a dashboard that lends visibility and control.

**Runs on your existing Claude Code subscription** — no separate service fees. The agent you're already paying for does the work; your data stays on your machine.

<p align="center">
    <img src="docs/hero_dashboard.png" alt="work-buddy dashboard: browsing agent session conversations" width="700" />
    <br>
    <em>The dashboard's Chats tab: browsing and searching across agent sessions.</em>
</p>

<!-- Replace with demo GIF when ready -->

## Get Started

You'll need [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (CLI or Desktop). [Obsidian.md](https://obsidian.md/) is recommended but not required for the core.

1. **Download the installer** from the [latest release](https://github.com/KadenMc/work-buddy/releases/latest) and run it.
   - Bundles its own Python (about 1 GB of dependencies).
   - Windows today; macOS and Linux installers are *coming soon!*
2. **Open the install folder in Claude Code.** work-buddy is already running, and Claude Code connects to it automatically.
3. **Run `/wb-setup guided`.** An agent walks you through the rest of setup: which features you want, connected to your own tools and preferences.
4. **Start working.** In Claude Code:

```
/wb-morning        # the morning routine: briefing, priorities, day plan
/wb-task-triage    # batch-decide on your task inbox
/wb-dev            # extend work-buddy itself
```

And from any terminal:

```
wbuddy status                    # sidecar health
wbuddy start | stop | restart    # control the background services
wbuddy --help                    # everything else
```

A **system-tray icon** shows work-buddy's status at a glance and opens a small panel to start, stop, or restart it and jump to the dashboard.

### What Can I Do with work-buddy?

- Review today's work state across notes, tasks, git, browser, and calendar before planning
- Triage a browser full of Chrome tabs: group them, close them, or turn them into tasks
- Finally empty your scratchpad: quick captures get routed into tasks, references, or kept as open questions
- Run a morning routine that writes a briefing, picks your top priorities, and generates a day plan
- Have the agent surface what would help next (captured fragments, stale tasks, drifted projects) proactively, before you have to ask
- Search everything you've ever discussed with your agent, across every project, not just this one
- Keep agent sessions coordinated through dashboard threads, notifications, and approvals

## How It Works

work-buddy runs a local [MCP server](https://modelcontextprotocol.io/) that extends Claude Code with a **gateway**: a small set of tools through which your agent discovers and runs everything work-buddy can do. Behind those tools sit purpose-built systems (a workflow conductor, a searchable knowledge store, tasks, memory, notifications, and more), every one of them yours to customize and extend. Workflows run their routine steps as ordinary code and engage the agent only where judgment is needed, which makes them fast, cheap, and remarkably consistent from run to run. The conductor handles ordering, dependencies, and resuming if you get interrupted.

```
wb_search  → discover what's available (natural language)
wb_run     → execute a capability or start a workflow
wb_advance → step through a multi-step workflow
wb_status  → check progress or system health
```

Here is a real morning routine:

```
> /wb-morning

Step 1/9: [code]  Load config and resolve target date
Step 2/9: [code]  Read sign-in state from journal
Step 3/9: [agent] Collect and synthesize context
Step 4/9: [code]  Fetch contract health data
Step 5/9: [code]  Pull calendar events
Step 6/9: [agent] Task briefing: prioritize, flag issues
Step 7/9: [agent] Metacognition check: detect drift
Step 8/9: [agent] Generate day plan
Step 9/9: [code]  Write briefing to journal
```

## Stay Sovereign

Automation you can't oversee is automation you can't trust. Sensitive actions (deleting tasks, pruning memory, changing your project files) require your explicit approval before they run, and the request reaches you everywhere at once: your phone (Telegram), your notes (Obsidian), and the web dashboard. Answer on whichever is closest; first response wins. The dashboard is part of the control loop, not just a viewer: live status, persistent conversation threads with your agents, decision prompts, and full session history, remotely accessible if you want it.

Your agents work autonomously when they can, and check in when they should. You set the boundaries.

## Under the Hood

| | |
|---|---|
| **Memory that survives sessions** | Preferences, project context, and working patterns persist across conversations, with semantic search over the lot. |
| **Every session, searchable** | work-buddy keeps a durable, searchable record of your Claude Code sessions across every project, so past decisions stay findable. |
| **Native Obsidian integration** | Plugin-level access to your vault: Tasks, Day Planner, Datacore, calendars (not just file I/O). |
| **A phone-sized command center** | Approve requests, answer questions, trigger workflows, and capture notes from Telegram. |
| **Real work commitments** | Contracts with claims, evidence plans, and stop rules keep projects honest; metacognition checks catch your documented failure patterns. |
| **Agents that coordinate** | Sessions message each other, hand off tasks, and hold persistent dashboard threads, so multi-session work doesn't need you as the relay. |

The full catalog of capabilities, workflows, and 50+ slash commands lives in the [handbook](https://docs.work-buddy.ai).

## Extend It

work-buddy builds work-buddy. The documentation your agent reads covers not just how to *operate* the framework but how to *develop* it, so you can tell your agent what you want (a new workflow, a new capability, a new integration) and it creates the pieces in the knowledge store, wires up a slash command, and ships the change through the built-in dev workflow (`/wb-dev` to orient, `/wb-dev-pr` to test, document, and open the PR). **What gets built is yours to read, edit, share, or remove.** The agent drafts; you curate.

## Documentation

| | |
|---|---|
| [Docs home](https://docs.work-buddy.ai) | Overview, install, and the entry point to everything below |
| [Handbook](https://docs.work-buddy.ai) | Every capability, workflow, and slash command, generated from the same knowledge store your agent reads |
| [Architecture](https://docs.work-buddy.ai) | How the gateway, conductor, sidecar services, and integrations fit together |
| [Contributing](CONTRIBUTING.md) | Dev setup (uv), the fork-and-pull workflow, and how to extend work-buddy |
| [License](LICENSE) | GPL-3.0-only, and why |

## Status

work-buddy runs real daily work, and it is beta software with edges:

- **Developed on Windows 11.** Linux and macOS support is new. Cross-platform compatibility has been audited and the core paths are guarded, but edge cases may remain. Issues and PRs for other platforms are especially welcome.
- The API surface is not yet stable; during 0.x, minor versions may contain breaking changes.

This is a framework designed to be extended. If you use Claude Code and want structured workflows, persistent memory, and deep tool integration, this is built for you.

## Community

- **Questions and ideas**: [GitHub Discussions](https://github.com/KadenMc/work-buddy/discussions)
- **Bugs and feature requests**: [Issues](https://github.com/KadenMc/work-buddy/issues)
- **Support the project**: [GitHub Sponsors](https://github.com/sponsors/KadenMc)

## Contributing

We welcome contributions: bug fixes, new capabilities, workflows, integrations, and documentation. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the full guide.

The fastest way to get started: clone the repo, install, and run `/wb-dev`. Your agent will orient itself.

## License

work-buddy is free software, licensed under the **[GNU General Public License v3.0](LICENSE)** (`GPL-3.0-only`).

Use it, study it, modify it, share it. The GPL adds one reciprocal condition: anyone you pass a modified version to receives the same freedoms you had. work-buddy is built for individuals, and copyleft is how it stays that way: improvements to the runtime keep flowing back to the people who use it rather than being absorbed into a closed product.

Copyright © 2025–2026 Kaden McKeen.
