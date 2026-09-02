---
name: Sensitive Data Checkpoint
kind: capability
description: Seal a local-only SQLite Journal hot snapshot beside an already authorized, content-aware Sources export and verify both by digest. The checkpoint is never eligible for the unencrypted remote backup.
capability_name: data_sensitive_checkpoint
category: backups
op: op.wb.data_sensitive_checkpoint
schema_version: wb-capability/v1
parameters:
  source_export_path:
    type: str
    description: Exact path returned by source_maintenance_operator action=export. It must already be directly inside the checkpoint directory.
    required: true
  source_export_sha256:
    type: str
    description: SHA-256 digest returned by the authorized Sources export.
    required: true
  source_export_id:
    type: str
    description: Export identity returned by the authorized Sources export.
    required: true
  source_item_count:
    type: int
    description: Item count returned by the authorized Sources export.
    required: true
  issued_copy_count:
    type: int
    description: Issued-copy count returned by the authorized Sources export.
    required: true
  destination:
    type: str
    description: Checkpoint directory under the configured backup root. Defaults to the Sources export's parent directory.
    required: false
  idempotency_key:
    type: str
    description: Stable replay key. Defaults to the Sources export ID.
    required: false
mutates_state: true
retry_policy: manual
tags:
- backups
- journal
- sources
- sensitive
- local-only
aliases:
- back up journal and sources
- sensitive checkpoint
- private content backup
parents:
- backups
---

This capability is the second step of a sensitive backup. First create a
content-carrying Sources archive through `source_maintenance_operator`; that
operator records every issued offline copy behind its own high-consent gate.
Then pass its exact receipt here. This operation does not copy or rename the
Sources archive. It adds a SQLite-consistent Journal member and a digest
manifest in the same directory.

The resulting directory is local-only (`remoteEligible: false`). It is not
added to the ordinary tarball or uploaded to the unencrypted GitHub Releases
backup path.
