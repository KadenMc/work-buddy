# Prompt Templates

work-buddy uses [Jinja2](https://jinja.palletsprojects.com/) templates for LLM system prompts. Templates live in two directories:

- **`defaults/`** — shipped with the package, version-controlled
- **`overrides/`** — user customizations, gitignored

The override directory is checked first, so user files win. To customize a prompt, copy the default to `overrides/` and edit it.

## Usage

```python
from work_buddy.prompts import get_prompt

# Simple template (no variables)
text = get_prompt("classify_system")

# Template with variables
text = get_prompt("triage_recommend_system", lens="intent", data_type="chrome",
                  actions=["close", "group", "capture"])
```

## Available Templates

### `classify_system.j2`

**Source:** `work_buddy/llm/classify.py`
**Variables:** none
**Purpose:** System prompt for multi-label intent classification. Instructs the model to classify data items against intent hypotheses with cited evidence.

### `summarize_system.j2`

**Source:** `work_buddy/llm/summarize.py`
**Variables:** none
**Purpose:** System prompt for structured content analysis. Extracts summaries, entities, key claims, user intent speculation, and posture.

### `triage_recommend_system.j2`

**Source:** `work_buddy/triage/recommend.py`
**Variables:**
- `lens` (`str`): `"intent"` or `"topic"` — determines grouping strategy
- `data_type` (`str`): `"chrome"`, `"document"`, `"journal"`, or `"conversation"` — activates data-specific guidance
- `actions` (`list[str]`): available triage actions (e.g., `["close", "group", "create_task", ...]`)

**Purpose:** System prompt for the triage recommendation engine. Groups items by intent or topic with data-type-specific heuristics.

### `reasoning_step.j2`

**Source:** `work_buddy/sidecar/dispatch/executor.py`
**Variables:**
- `workflow_name` (`str`): name of the running workflow
- `step_name` (`str`): current step identifier
- `instruction` (`str`): step instruction text (from workflow `.md` file)
- `prior_results` (`list[dict]`): previous step results, each with `step`, `type`, `result` keys (last 3 shown)

**Purpose:** Prompt wrapper for workflow reasoning steps executed by the sidecar dispatch system.

## Customization

1. Copy the default template: `cp prompts/defaults/classify_system.j2 prompts/overrides/classify_system.j2`
2. Edit the override file
3. Restart the process — templates are loaded once at import time

Templates use Jinja2's `StrictUndefined` mode: missing variables raise errors rather than silently producing empty strings.

## Diagnostics

```python
from work_buddy.prompts import list_templates

print(list_templates())
# {'classify_system': 'default', 'summarize_system': 'override', ...}
```
