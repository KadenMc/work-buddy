---
name: Sources Maintenance Operator
kind: capability
description: Preview, export, recover, safely abort, import, redact, or resume an exact paused effect through a high-consent operator boundary.
capability_name: source_maintenance_operator
category: context
op: op.wb.source_maintenance_operator
schema_version: wb-capability/v1
parameters:
  action:
    type: string
    description: preview, status, effects, export, recover_export, abort_export, import, redact, or recover_effect.
    required: true
  source_refs:
    type: list[str]
    description: Exact SourceRef URIs; omit only to preview or export all retained Sources.
    required: false
  destination:
    type: string
    description: Exact destination path for export.
    required: false
  source_path:
    type: string
    description: Exact archive path for import.
    required: false
  export_id:
    type: string
    description: Durable export ID for status or recovery.
    required: false
  include_content:
    type: bool
    description: Include retained bytes in an export; defaults true.
    required: false
  collision_policy:
    type: string
    description: quarantine, remap, or reject foreign identity collisions.
    required: false
  reason_code:
    type: string
    description: Stable redaction reason code.
    required: false
  effect_id:
    type: string
    description: Exact paused/retryable Sources effect ID for recover_effect.
    required: false
mutates_state: true
consent_operations:
- sources.maintenance
retry_policy: manual
auto_retry: false
tags:
- sources
- provenance
- export
- redaction
- recovery
parents:
- architecture/source-foundation
---

`preview`, `status`, and `effects` return content-free scope and state. Every export,
recovery, safe pre-write abort, import, and redaction requires a fresh exact high-risk approval. The
operation derives the enrolled local actor and authorization fingerprint
inside Work Buddy; callers cannot submit either. Content exports register
issued offline copies, so redaction can report that those copies may remain.

Export is a durable prepared → written → completed state machine. The archive
digest and issued-copy usages are recorded before acknowledgement. Recovery
verifies the exact destination bytes and never interprets an interrupted write
as “not sent.” Imports retain foreign authority and quarantine collisions by
default. No action silently joins the unencrypted rolling backup.

An imported archive keeps command and effect records inert. `recover_effect`
is the reachable operator path for one exact paused effect: its target domain,
type, payload digest, status, and error are shown in a fresh high-risk prompt,
and only that effect is reauthorized. It does not deliver the effect inside the
approval call; the ordinary domain dispatcher performs restart-safe delivery.
