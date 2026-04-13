# Contributing to work-buddy

Thank you for your interest in contributing! We welcome all types of contributions — bug fixes, new capabilities, workflow definitions, integration improvements, and documentation.

## Before You Start

- **Search existing issues:** Check if your idea or bug has already been reported.
- **Open an issue first:** For significant changes (new subsystems, architectural changes, new integrations), please open an issue to discuss your proposal before writing code. This saves everyone time.
- **Small fixes are welcome without discussion** — typos, bug fixes, documentation improvements, and minor enhancements can go straight to a PR.

## Development Setup

### Prerequisites

- Python 3.11 (via [Miniforge](https://github.com/conda-forge/miniforge) or similar)
- [Poetry](https://python-poetry.org/) for dependency management
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (recommended for development — work-buddy develops itself)
- Git

### Install

```bash
git clone https://github.com/YOUR-USERNAME/work-buddy.git
cd work-buddy

conda create -n work-buddy python=3.11 -y
conda activate work-buddy

poetry install
poetry install --extras all  # Optional: enables all integrations
```

### Running Tests

```bash
poetry run pytest              # All tests
poetry run pytest -m unit      # Fast unit tests only
poetry run pytest -m component # Tests with temp dirs/DBs
```

**Important:** Never use `pip install`. Always use Poetry for dependency management.

### Orienting on the Codebase

If you're using Claude Code, start with:

```
> /wb-dev
```

This orients you on the architecture, patterns, and development workflow. The agent documentation system (`agent_docs`) is the fastest way to understand any subsystem — faster than reading scattered READMEs.

## The Fork and Pull Workflow

We use the **Fork and Pull Model** for all contributions.

1. **Fork** the repository to your own GitHub account.
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/YOUR-USERNAME/work-buddy.git
   ```
3. **Add the upstream remote** to stay synced:
   ```bash
   git remote add upstream https://github.com/KadenMc/work-buddy.git
   ```
4. **Create a branch** for your work:
   ```bash
   git checkout -b feature/your-feature-name
   ```
5. **Make your changes**, commit with clear messages.
6. **Run tests** before pushing — all tests must pass.
7. **Push** and open a Pull Request against `main`.

## How to Extend work-buddy

work-buddy is designed to be extended through three main patterns. You don't need to understand the entire codebase to contribute — pick the pattern that fits your contribution.

### Adding a Capability

Capabilities are single functions exposed through the MCP gateway. To add one:

1. Write your function in the appropriate `work_buddy/` submodule
2. Register it in `work_buddy/mcp_server/registry.py` using the `@capability` pattern
3. Add a knowledge store unit (or let `build.py` generate one)
4. Add tests

Your capability becomes discoverable via `wb_search` and executable via `wb_run` — no other wiring needed.

### Adding a Workflow

Workflows are multi-step DAGs stored as `WorkflowUnit` entries in the knowledge store. The conductor handles dependency ordering, state persistence, and step execution — you describe the steps and their instructions, and the framework runs them.

A `WorkflowUnit` contains:
- **Steps** — a DAG of `{id, name, depends_on}` entries that define execution order
- **Step instructions** — per-step text that the agent receives when executing each step
- **Execution policy** — whether steps run in the main session or delegate to subagents
- **Auto-run specs** — steps that execute deterministic code automatically (no agent reasoning needed)

**The easiest way to add a workflow:** describe what you want to your agent and let it use the knowledge store editor to create the `WorkflowUnit`. The store handles validation, DAG integrity checks, and discoverability. Add a thin slash command in `.claude/commands/wb-your-workflow.md` and it's accessible via `/wb-` autocomplete.

Browse existing workflows via `wb_search("workflow")` or `agent_docs` to see real-world patterns including auto-run steps, subagent delegation, sub-workflow chaining, and conditional branching.

### Adding an Integration

New integrations (external services, tools, APIs) should follow the feature toggle pattern:

1. Add dependencies as optional extras in `pyproject.toml`
2. Gate imports behind availability checks (see `work_buddy/health/` for the toggle system)
3. Put integration code in its own submodule under `work_buddy/`
4. Include a `README.md` in your submodule with architecture and setup docs

This keeps the core installable without your integration's dependencies.

## Developing with Agents

work-buddy is designed to be developed by the same agents that use it. Most commits in this repo were authored by Claude Code agents — and the tooling is set up to make that workflow smooth.

**The typical agentic development loop:**

```
/wb-dev                    # Orient on architecture and patterns
# ... make your changes ...
/wb-dev-test               # Run tests for what you changed, check coverage
/wb-dev-push               # Pre-push checklist: tests, knowledge store, DAG integrity
/wb-task-handoff            # If the work spans sessions, package context for the next one
```

**Why this matters for contributors:**

- You don't need to memorize the codebase. Point your agent at `/wb-dev` and it will discover what it needs via `agent_docs`.
- You don't need to figure out which tests to run. `/wb-dev-test` detects what changed and runs the right subset.
- You don't need to manually check if your workflow DAG is valid. `/wb-dev-push` validates everything before you ship.
- If your work takes multiple sessions, `/wb-task-handoff` creates a structured handoff note so the next agent picks up exactly where you left off — no context loss.

**A few conventions that support this model:**

- **Commit messages are descriptive** — they're often written by agents and read by future agents. Clear summaries help everyone.
- **The knowledge system is load-bearing** — agents query `agent_docs` at runtime to understand subsystems. If you change how something works, update the corresponding knowledge unit.
- **Capabilities over hidden code** — if something can be a registered capability (discoverable via `wb_search`, executable via `wb_run`), it should be. Hidden functions are invisible to agents.

You're welcome to develop however you prefer — by hand, with Claude Code, or with any other tool. But the framework is optimized for agent-assisted development, and the tooling is there to support it.

## Pull Request Checklist

Run `/wb-dev-push` to check most of these automatically. When submitting your PR, ensure:

- [ ] All tests pass (`poetry run pytest` or `/wb-dev-test`)
- [ ] New features include tests
- [ ] Knowledge store validates (no broken DAG refs, no orphaned commands)
- [ ] The PR title is descriptive and concise
- [ ] You've linked to a relevant issue if one exists (e.g., "Closes #12")
- [ ] Documentation is updated if you changed behavior (knowledge units, subsystem READMEs)
- [ ] New dependencies use Poetry (`poetry add`), not pip
- [ ] New integrations are behind feature toggles (optional extras in `pyproject.toml`)

## Project Philosophy

A few principles that guide decisions:

- **Core stays lean.** The gateway, conductor, and config system should remain small and stable. Complexity lives in capabilities and workflows, not the framework.
- **Capabilities over code.** If something can be a registered capability (discoverable, executable via `wb_run`), it should be — rather than a hidden function buried in a module.
- **Documentation is infrastructure.** The knowledge store isn't an afterthought. Agents use it at runtime. Undocumented capabilities are invisible capabilities.
- **Feature toggles over hard dependencies.** Not everyone needs Telegram, Chrome triaging, or persistent memory. New integrations should be optional.
- **Honest about maturity.** This is a pre-release project. APIs may change. Document what's stable and what isn't.

## License

By contributing, you agree that your work will be licensed under the [MIT License](LICENSE).
