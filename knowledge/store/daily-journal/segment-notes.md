---
name: Segment Notes
kind: workflow
description: Read the Running Notes section from a journal file, identify coherent threads of related content, and annotate the text with inline thread IDs. The raw text is never modified â€” only HTML comment tags are inserted.
summary: Read the Running Notes section from a journal file, identify coherent threads of related content, and annotate the text with inline thread IDs. The raw text is never modified â€” only HTML comment tags are inserted.
workflow_name: segment-notes
execution: subagent
steps:
- id: extract-section
  name: Extract Running Notes section from journal
  step_type: code
  depends_on: []
  execution: main
  invokes: []
- id: generate-ids
  name: Generate pool of thread IDs
  step_type: code
  depends_on:
  - extract-section
  execution: main
  visibility:
    mode: full
  invokes: []
- id: segment-and-tag
  name: Segment content and insert thread tags
  step_type: reasoning
  depends_on:
  - generate-ids
  execution: main
  invokes: []
- id: validate
  name: Validate segmentation with Python
  step_type: code
  depends_on:
  - segment-and-tag
  execution: main
  invokes: []
- id: extract-threads
  name: Extract thread objects from tagged text
  step_type: code
  depends_on:
  - validate
  execution: main
  visibility:
    mode: none
  invokes: []
- id: return-results
  name: Return tagged text, thread list, and validation
  step_type: code
  depends_on:
  - extract-threads
  execution: main
  visibility:
    mode: full
  invokes: []
tags:
- daily-journal
- segment
- notes
parents:
- daily-journal
- daily-journal
---

## What NOT to do

- Don't modify, rephrase, summarize, or reorder any raw text
- Don't interpret what items mean â€” that's the router's job (with user input)
- Don't delete content, even if it looks stale or irrelevant
- Don't merge threads aggressively â€” when in doubt, keep separate
- Don't try to route or triage during segmentation â€” separation of concerns
- Don't skip the Python validation step


## Output quality checks

Before returning, verify:
- [ ] Every content line is inside exactly one thread
- [ ] All thread tags are properly opened and closed
- [ ] No content was modified, deleted, or reordered
- [ ] Code blocks and links are intact
- [ ] Carried-over banners are stripped (noted as metadata, not content)
- [ ] Multi-concern lines are flagged with `<!-- [multi] -->`

## extract-section

If given a `journal_path`, read the file and extract everything between:
- Start: `# **Running Notes / Considerations**`
- End: `% RUNNING END` marker (if present) or the next top-level heading

Strip the `carried over from YYYY-MM-DD` banners â€” these are structural artifacts of the carry-forward mechanism, not content. Preserve the dates as metadata (they indicate when items were originally written).

## generate-ids

Use Python to pre-generate a pool of random IDs:

```python
import uuid

def generate_thread_id() -> str:
    return f"t_{uuid.uuid4().hex[:6]}"
```

Generate more IDs than you expect to need (e.g., 50). Pass these to the LLM so it uses consistent, collision-free IDs.

## segment-and-tag

This is the reasoning-heavy step. Read the full extracted text and identify coherent threads â€” groups of content that belong to the same concern, topic, or action item.

**Insert open/close HTML comment tags around each thread:**

```markdown
<!-- [t_a3f8c1] -->
- Graph tokenization approach for entity linking
- Looked at paper X, relates to graph tok idea
<!-- [/t_a3f8c1] -->
```

**Segmentation rules:**

1. **Preserve all raw text exactly** â€” do not edit, rephrase, reorder, or delete any content. Only insert `<!-- [t_xxxxxx] -->` and `<!-- [/t_xxxxxx] -->` tags
2. **Every line of content must be inside exactly one thread** â€” no orphaned lines
3. **A thread is content that belongs to the same concern.** This requires interpretation â€” "Taxes - Varsha" and "Varsha - T4 / T4A" are the same thread even though they appear on different dates. A 40-line technical exploration about graph tokenization is one thread
4. **Multi-concern lines must be flagged.** If a single bullet contains two unrelated concerns (e.g., "VPN/taxes/new laptop stuff"), wrap it in one thread but add a `<!-- [multi] -->` annotation so the router knows it may need splitting
5. **Sub-bullets belong to their parent's thread** unless they clearly introduce a new topic
6. **Code blocks, links, and formatted text are preserved exactly** â€” tags go outside these, not inside
7. **Empty lines and horizontal rules between threads are fine** â€” place tags around content, not whitespace
8. **Carried-over banners are stripped** â€” they are not content. But note the source dates in a separate metadata output
9. **When uncertain whether two items are the same thread, keep them separate.** Over-splitting is better than under-splitting â€” the router can propose merges, but splitting a wrongly-merged thread is harder

**Edge cases:**

- **Long multi-paragraph explorations**: one thread, even if they span 50+ lines. The thread is the coherent unit
- **Isolated one-liners with no context**: each gets its own thread ID. The router will ask the user what they mean
- **Links/references followed by commentary**: group together as one thread
- **Empty carried-over sections** (just banners, no content between them): skip entirely

## validate

After the LLM produces the tagged text, run Python validation:

```python
import re

def validate_segmentation(tagged_text: str, original_text: str) -> dict:
    """Validate that segmentation is complete and consistent."""

    # Extract all thread IDs used
    open_tags = re.findall(r'<!-- \[(t_[a-f0-9]{6})\] -->', tagged_text)
    close_tags = re.findall(r'<!-- \[/(t_[a-f0-9]{6})\] -->', tagged_text)

    thread_ids = set(open_tags)

    errors = []

    # Every open tag has a matching close tag
    if sorted(open_tags) != sorted(close_tags):
        errors.append("Mismatched open/close tags")

    # No nested threads (open inside open without close)
    # ... validation logic ...

    # All content lines from original are present in tagged version
    original_lines = [l.strip() for l in original_text.splitlines() if l.strip() and not l.strip().startswith('---') and 'carried over from' not in l]
    tagged_content = re.sub(r'<!-- \[/?t_[a-f0-9]{6}\??]\s*-->', '', tagged_text)
    tagged_lines = [l.strip() for l in tagged_content.splitlines() if l.strip() and not l.strip().startswith('---')]

    if len(original_lines) != len(tagged_lines):
        errors.append(f"Line count mismatch: {len(original_lines)} original vs {len(tagged_lines)} tagged")

    return {
        "valid": len(errors) == 0,
        "thread_count": len(thread_ids),
        "thread_ids": sorted(thread_ids),
        "errors": errors,
    }
```

If validation fails, the LLM should fix the issues and re-validate. Do not proceed to routing with invalid segmentation.

## extract-threads

Once validated, extract each thread into a structured object:

```python
def extract_threads(tagged_text: str) -> list[dict]:
    """Extract thread objects from tagged text."""
    threads = []
    # Parse tagged text, group content by thread ID
    # For each thread:
    threads.append({
        "id": "t_a3f8c1",
        "raw_text": "The exact text between open/close tags",
        "line_count": 3,
        "source_dates": ["2026-03-03", "2026-03-14", "2026-03-28"],  # from banner positions
        "has_multi_flag": False,
    })
    return threads
```

## return-results

Return:
1. The **tagged text** (original content with inserted thread tags)
2. The **thread list** (extracted structured objects)
3. The **validation result**

The caller (`process-backlog.md`) uses the thread list to feed into the routing workflow.
