---
name: Co-work Verify durable dispatch and rechecks
kind: system
description: Closed durable worker dispatch, fail-closed restart recovery, and proposal-linked re-evaluation obligations.
summary: Verify worker launches use the disk-backed operation queue through a non-agent-callable internal allowlist. Recovery recreates only provably unlaunched work, fails closed on uncertain calls, and resumes durable typed submissions without another model call. Applied corrections project exact recheck intents that Review hands to the Verify dock; only dock-owned Run Verify launches them.
entry_points:
- work_buddy.cowork.verify_dispatch
- work_buddy.cowork.verify_runtime
- work_buddy.cowork.verify_rechecks
- work_buddy.sidecar.internal_operations
- work_buddy.sidecar.retry_sweep
tags:
- cowork
- verify
- durable-dispatch
- queue
- recovery
- recheck
- lineage
aliases:
- Verify queue
- Verify recovery
- applied-correction recheck
- result lineage
parents:
- cowork/verify-and-cothink
dev_notes: |-
  The Truth ledger is the domain authority. `cowork_verify_jobs.db` stores only
  operational worker bindings and state. Internal launch records live in the
  existing operations directory and are resolved through a closed allowlist,
  not the MCP capability registry.

  The safety rule is asymmetric: a missing handoff for a `prepared` job is safe
  to recreate, while an expired `launching` lease has an unknown launch outcome
  and must become unavailable without replay. A `submitted` job is different:
  its typed payload and hash are already durable, so a projection lease may
  resume only the deterministic domain consequences.

  Portable coordination records treat a legacy affirmation's
  `affirmed_action_snapshot_id` as a required prior ActionSnapshot reference.
  Import cannot silently retain a confirmation whose attested affirmation is
  absent.
---

## Durable launch path

Starting Verify or Co-think creates:

- the immutable Truth action/plan/run records;
- a job-scoped model-call authorization receipt;
- an operational runtime job bound to the exact run, role, provider/model,
  context hash, and worker session; and
- a deterministic internal operation in the existing disk-backed queue.

The internal operation type is closed and non-discoverable. Agents cannot find
or invoke the worker-launch primitive through `wb_search` or `wb_run`.

The queue operation ID is deterministic from the handler and job ID.
Concurrent identical enqueues converge; conflicting reuse fails. `RetrySweep`
provides the existing atomic lease. The runtime job then adds an atomic launch
claim, preventing two sweepers from spawning the same worker.

## What recovery may do

On sidecar start and queue sweeps:

- a prepared job whose handoff file is missing is re-enqueued while its exact
  authorization remains valid;
- an expired authorization becomes terminally unavailable without a model call;
- a running job with a live process remains running;
- a running job whose process exited before typed submission becomes
  unavailable; and
- a job left in launching after its launch lease expires becomes unavailable
  with `launch_outcome_unknown`; while
- a job left in `submitted` has its stored typed payload integrity-checked,
  atomically projection-leased, and completed from that payload without
  starting another model call.

The last case is intentionally not replayed: the host cannot prove whether the
external model call began before the crash. The model-call receipt has
`retry_limit: 0`; queue retry handles pre-launch dispatch failure, not silent
model-call replay.

The durable `submitted` boundary is not a call retry. It means the worker call
already returned and the normalized typed payload plus SHA-256 digest were
committed before consequence projection. Reconciliation can therefore append
the same dispositions, next-job binding, Co-think outcome, or proposal exactly
once under a projection lease. The portable Truth lifecycle also records the
submitted fact, so a crash between runtime and Truth projection can be
backfilled without exposing raw worker output.

Run history and inspection expose prepared/queued/running/completed/failure
state and diagnostics. Queue state is operational only; it cannot invent a
Truth result or proposal.

## Recheck lineage

When a Verify result leads to a proposal, a `ResultRelation` records
`addresses → proposal`. When that exact proposal is applied through a committed
human sitting, `verification_recheck_intents` derives the pending obligation
from durable sitting receipts, Verify runs, and result relations. It does not
create another mutable recheck queue.

The intent binds:

- sitting and source Verify run;
- exact applied proposal IDs;
- original action snapshot and target source/kind;
- the original target reference when the action was scoped;
- original provider/model; and
- the original user goal and protected intent; and
- committed timestamp.

A conforming recheck remains useful: it records that the exact deterministic
criterion found no configured non-preferred term in the new frozen target.

## Starting a recheck

A pending intent appears as a persistent **Correction ready to recheck** card
in Review. It does not run a model on refresh, SSE invalidation, sitting
commit, or background recovery. The Review action is only a contextual handoff:
it opens the Verify dock and binds the source run, pending proposals, original
intent, and required execution selection. Review cannot launch the worker.

Only **Run Verify** in that bound dock captures the fresh action snapshot and
starts execution. When the durable original target still resolves, it is
rebound automatically. When a legacy target cannot be resolved, the dock
requires a newly chosen and affirmed exact **Working on** passage before the
run can start. The affirmation captures the character-range reference identity
and target-text hash in a separate non-executing server request. The server
persists that human ActionSnapshot and returns its receipt. Run captures again,
reloads the attested receipt, and must match both values; an edit inside the
same apparent range invalidates the affirmation.

Every strict recheck start requires:

- a fresh action snapshot captured after the sitting committed;
- the source run’s provider/model;
- the source run’s user goal and protected intent;
- the exact still-pending proposal set; and
- the exact derived `recheck_intent_id`;
- a fresh model-call authorization.

For an ordinary durable intent, the fresh capture must also resolve the same
target source, kind, and durable target reference. For a legacy
`user_action_required` intent, the server instead requires the separately
persisted human affirmation receipt, a fresh non-document **Working on**
action, a character-granular durable reference, and exact reference/text
hashes matching that affirmation. The receipt is bound to the intent, source
run, proposal set, original goal, protected intent, actor, and document before
Run. The confirmation, intent, source run, proposals, and
original goal/intent travel together in the coordination request and model-call
authorization context.

Neither path may widen an unresolved range to the whole document. Review only
hands the context to the Verify dock; the person chooses and affirms the
intended passage, and **Run Verify** remains the execution boundary.

An exact retry of the same Run capture and bindings returns the already-started
run, even after the derived intent has become fulfilled. Reusing that capture
with a changed goal, protected intent, model, configuration, lineage, or
affirmation receipt fails closed.

New results record `rechecks` relations to the applied proposal and prior
evaluation result. The derived intent becomes fulfilled only when later result
relations prove that the exact pending proposals were re-evaluated by a run
bound to that intent, target, provider/model, and source run.
