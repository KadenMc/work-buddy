---
title: Wikilinks and Callouts
tags:
  - obsidian
  - synthetic
---

# Obsidian bracket constructs

A plain paragraph that precedes the interesting syntax.

See [[Some Note]] for background, or the aliased [[Some Note|friendly label]].

An embed of another note follows: ![[Embedded Note]] and it should survive.

A block reference anchor sits at the end of this sentence. ^block-anchor-1

Refer to [[Some Note#^block-anchor-1]] to jump straight to that block.

> [!note] A note callout
> The body of the callout spans
> two lines of quoted text.

> [!warning]- A collapsible warning
> Hidden until the reader expands it.

Inline metadata via dataview: status:: in-progress and priority:: high.

A paragraph with a #project/tag and #another inline tag.

Some text with %%an obsidian comment%% that should not render.

A sentence with ==highlighted text== in the middle.

## After the constructs

A final paragraph so the last construct is not the trailing block.
