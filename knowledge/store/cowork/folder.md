---
name: Co-work Folder
kind: concept
description: 'The folder a person selects as their Co-work working boundary: what it owns on disk, what it contains, and the lifecycle entry point for setting one up.'
summary: A Co-work Folder is an ordinary host folder that Co-work has been set up in. It owns .wbuddy/manifest.yaml and the canonical .wbuddy/cowork store, and it is the containment boundary for every document, conversation, and claim beneath it.
tags:
- cowork
- folders
- boundary
- lifecycle
- overview
aliases:
- co-work folder
- folder boundary
- working folder
- folder lifecycle
parents:
- cowork
dev_notes: |
  Keep this unit short and conceptual. It is the browse node for the folder
  lifecycle, so per-stage mechanics, refusal vocabularies, budgets, and lock
  ordering belong in the child units rather than here.

  Path identity is the canonical host path, not the folder's basename: the
  basename labels the chip and never determines identity, and symlink and
  junction aliases are canonicalized before any comparison so an alias cannot
  register the same store twice.
---

# Co-work Folder

A Co-work Folder is the working boundary a person selects. It is an ordinary folder on the host that Co-work has been set up in, and it is the containment boundary for everything Co-work knows about the work inside it.

A folder owns two things on disk. `.wbuddy/manifest.yaml` declares which Work Buddy components the folder carries. `.wbuddy/cowork/` holds the canonical store: the profile that names the folder's identity, the SQLite database behind its documents and claims, and the deterministic export that travels with the folder. Everything else beside the store is machine-local and stays uncommitted.

One folder may own many documents. Every document is addressed by a path relative to the folder root, which is why a folder can enclose no other Co-work folder: two folders that both enclose a file would each claim it under a different relative path and neither could see the other's record of it.

The folder is also the scope a person moves between. Opening a folder selects the documents, conversations, and claims beneath it; closing it puts them away as a set.

Children of this unit cover the folder's lifecycle. Start with `cowork/folder/setup` for how a folder is inspected, proven safe, and set up.
