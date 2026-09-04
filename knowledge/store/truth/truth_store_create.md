---
name: Truth Store Create
kind: capability
description: Safely initialize and register one Folder's canonical .wbuddy/cowork Truth store from a complete profile. A store id is minted when neither the request nor profile supplies one.
capability_name: truth_store_create
category: truth
op: op.wb.truth_store_create
schema_version: wb-capability/v1
parameters:
  root:
    type: str
    description: Folder that will own .wbuddy/manifest.yaml and the canonical .wbuddy/cowork sidecar. Setup proves under the folder operation locks that the folder holds no Co-work store, sits inside none, and encloses none, then writes all of it or none of it. A folder that fails that proof is refused with the typed code naming its state.
    required: true
  profile:
    type: dict
    description: Complete Truth profile mapping. The store_id field may be omitted.
    required: true
  store_id:
    type: str
    description: Optional UUID-compatible store identity.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
consent_operations:
- truth.store_create
tags:
- truth
- store
- create
- registry
aliases:
- create truth store
- initialize truth ledger
- register scoped truth
- new evidence store
- make claim store
parents:
- truth
---

Store creation delegates the folder work to Co-work's setup path, so the capability inherits one boundary proof rather than repeating it. The folder is walked exactly once, inside the folder operation locks, and that single walk is what authorizes the write: it classifies the folder from the filesystem and proceeds only if the folder holds no Co-work store, sits inside no Co-work folder, and encloses none. There is no partial outcome. A failure part way through removes the store directory, restores the exact manifest bytes, unregisters the store, and leaves the folder as it was.

This caller shows the folder to nobody between deciding and writing, so it holds no prior observation to pin and passes no inspection fingerprint. That choice is what makes its refusals useful. Instead of a generic report that the folder changed, a refusal carries the reason code for the state the walk actually found: `folder_already_initialized`, `inside_existing_folder`, `contains_nested_folder`, `folder_too_large_for_safe_setup`, `folder_layout_incomplete`, or `identity_conflict`. Each message names both the folder's state and the action that answers it, which is the whole contract for an agent caller that reads the exception text and nothing else.

A folder whose Work Buddy data could not be read comes back as `folder_unreachable`, a retryable failure saying the data is temporarily unavailable and to try again in a moment. It is deliberately not the repair wording a genuine collision uses, because nothing was established about what that data holds and a healthy store held open by a backup or a scanner classifies this way. A descendant that could not be read, or that kept changing while it was listed, comes back as `descendant_scan_incomplete` and is retryable for the same reason. Every other refusal is settled: repeating the call cannot change it.

Store identity is resolved before the folder is touched. A `store_id` argument and a `profile.store_id` that disagree are rejected, either one alone is accepted, and an identity supplied by neither is minted. An identity already present in the registry is refused before any folder work begins. On success the validated store is registered, the registered row is returned, and a `truth.store_created` event is emitted carrying the sidecar path and profile name. A created store that did not reach the registry is an invariant violation, not a partial success.

See `cowork/folder/setup` for the boundary proof, the full refusal vocabulary, and which refusals are worth retrying.
