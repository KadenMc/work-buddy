---
name: Cowork Doc List
kind: capability
description: List registered cowork docs in a scope with source-writeback mode, hashes, drift, and open-proposal counts.
capability_name: cowork_doc_list
category: cowork
op: op.wb.cowork_doc_list
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  profile:
    type: str
    description: Optional profile filter. One store carries one profile, so a value that does not match the scope yields an empty list.
    required: false
mutates_state: false
retry_policy: manual
auto_retry: false
tags:
- cowork
- doc
- list
aliases:
- list cowork docs
- list documents
- cowork document list
- open documents in scope
parents:
- cowork
---

`cowork_doc_list` returns the registered document catalog for one store. Open
edit proposals and open flags have separate counts; the optional profile filter
matches the store's single profile and otherwise returns an empty list.

Each row keeps the source artifact and managed projection distinguishable:

- `source_writeback` is `same_file` or `never`.
- `import_source_sha256` is the source digest recorded when a detached file
  import was captured. It is the comparison baseline for selecting that source
  path again and is `null` when no retained import identity is available.
- `observed_source_file_sha256` is the digest of the file currently found at the
  recorded source path, or `null` when safe, bounded observation does not
  succeed.
- `source_file_sha256` is a compatibility alias for
  `observed_source_file_sha256`; new consumers should not use it as the recorded
  import identity.
- `last_materialized_sha256` identifies the current managed projection.
- For `source_writeback=same_file`, `current_file_sha256` and `drift_state`
  describe the writeback target. For `source_writeback=never`,
  `current_file_sha256` is the internal managed-projection baseline and
  `drift_state` remains `clean`.

For detached imports, routine observation is governed by the persisted importer
descriptor and its size limit. It accepts only a regular file and never follows
links or reparse points. Missing, unsafe, changing, oversized, or
importer-unbound sources leave `observed_source_file_sha256=null` instead of
failing the catalog or harming the managed document. The explicit
`version=current` source read is the diagnostic boundary and returns a typed
failure rather than collapsing the reason to `null`.

For a document created through **From file**, `source_writeback=never`. Its
original file is not a Save target, so later source-file changes do not become
unsaved Co-work edits or a reimport instruction. Comparing
`observed_source_file_sha256` with `import_source_sha256` can instead support the
explicit changed-source warning when the path is selected again. Matching
digests permit opening the existing managed copy; a mismatch or missing import
identity must not silently refresh it.

The catalog returns digests, not source bytes. Current imports retain their
exact source bytes in the content-addressed store and portable Truth export;
historical imports may retain only a digest. Markdown is the only supported file
importer today, but this catalog contract is not format-specific.
