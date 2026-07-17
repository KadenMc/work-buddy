---
title: Frontmatter Variants
aliases:
  - fm-variants
  - front-matter
tags: [obsidian, synthetic, escaping]
dev_notes: |-
  This block scalar holds underscores_here and more_here and
  retention_predicate that a naive serializer re-escapes without bound.
nested:
  key: value
  flag: true
count: 42
enabled: false
---

# Body after rich frontmatter

The frontmatter above mixes a block scalar, a flow-sequence tag list, a
nested mapping, and scalar values. The import boundary must strip and
re-attach it byte-for-byte, never feeding it through the serializer.

A paragraph with underscores like dev_notes so the body also exercises the
escaping path independently of the frontmatter.

## A second section

Final paragraph after the frontmatter body.
