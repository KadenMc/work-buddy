import { describe, expect, it } from "vitest";

import type { TaskProposal } from "../contracts";
import {
  canClearRealizedProposal,
  draftFromTaskProposal,
  retainedProposalResolution,
  taskDraftFields,
  taskDraftFingerprint,
  taskProposalResolution,
  type TaskCreateDraft,
} from "./taskDraft";

const realized: TaskProposal = {
  thread_id: "th-linked", proposal_event_id: 7, status: "realized",
  parameters: { task_text: "Reviewed title", summary: "Reviewed context" },
  origin: { kind: "task_quick_add", actor: { subject: "not-host-metadata" } },
  realization: { task_id: "t-created", task_revision: 1, receipt_id: "receipt-created", href: "/app/tasks?task=t-created" },
  href: "/app/tasks?proposal=th-linked",
};
const fields = draftFromTaskProposal(realized);
const linked: TaskCreateDraft = {
  ...fields,
  proposal_ref: { threadId: realized.thread_id, proposalEventId: realized.proposal_event_id, draftFingerprint: taskDraftFingerprint(fields) },
};

describe("Task proposal draft terminal evidence", () => {
  it("carries the requested note role and explicit Truth resolution in canonical fields", () => {
    expect(taskDraftFields({ ...fields, create_note: false, enable_truth_tools: true })).toMatchObject({
      requested_note_role: null,
      initial_note: null,
      requested_truth_policy_resolution: null,
    });
    expect(taskDraftFields({ ...fields, create_note: true, initial_note: "  Exact text\n", enable_truth_tools: false })).toMatchObject({
      requested_note_role: "working_document/v1",
      initial_note: "  Exact text\n",
      requested_truth_policy_resolution: "disabled",
    });
    expect(taskDraftFields({ ...fields, create_note: true, enable_truth_tools: true })).toMatchObject({
      requested_note_role: "working_document/v1",
      requested_truth_policy_resolution: "enabled",
    });
  });

  it("permits clearing only a realized exact-bound, unchanged canonical draft", () => {
    expect(canClearRealizedProposal(linked, realized)).toBe(true);
    expect(canClearRealizedProposal({ ...linked, title: "Later human edit" }, realized)).toBe(false);
    expect(canClearRealizedProposal(linked, { ...realized, parameters: { ...realized.parameters, summary: "Different canonical context" } })).toBe(false);
    expect(canClearRealizedProposal(linked, { ...realized, thread_id: "th-another" })).toBe(false);
    expect(canClearRealizedProposal(linked, { ...realized, proposal_event_id: 8 })).toBe(false);
    expect(canClearRealizedProposal(linked, { ...realized, proposal_event_id: 6 })).toBe(false);
  });

  it("never clears a dismissed, unresolved, pending, batch, or unbound source", () => {
    for (const status of ["rejected", "ready", "executing", "needs_attention", "unavailable"] as const) {
      expect(canClearRealizedProposal(linked, { ...realized, status, realization: null })).toBe(false);
    }
    expect(canClearRealizedProposal({ ...linked, proposal_pending: { clientMutationId: "same-request", parameters: {}, origin: {}, draftFingerprint: "same-fields" } }, realized)).toBe(false);
    expect(canClearRealizedProposal({ ...linked, batch_lines: ["Preserve this row"] }, realized)).toBe(false);
    expect(canClearRealizedProposal(fields, realized)).toBe(false);
  });

  it("retains only terminal status, event, and validated task identity", () => {
    const resolution = taskProposalResolution(realized);
    expect(resolution).toEqual({ status: "realized", proposalEventId: 7, taskId: "t-created" });
    expect(taskProposalResolution({ ...realized, status: "rejected", realization: null })).toEqual({ status: "rejected", proposalEventId: 7 });
    const unsafeExtras = { ...resolution, href: "https://untrusted.invalid", receipt: realized.realization, actor: realized.origin.actor };
    expect(retainedProposalResolution({ ...linked.proposal_ref!, resolution: unsafeExtras as NonNullable<TaskCreateDraft["proposal_ref"]>["resolution"] })).toEqual(resolution);
    expect(taskDraftFingerprint({ ...linked, proposal_ref: { ...linked.proposal_ref!, resolution } })).toBe(taskDraftFingerprint(fields));
  });

  it.each([
    { threadId: "javascript:alert(1)" },
    { proposalEventId: 0 },
    { proposalEventId: 1.5 },
    { resolution: { status: "realized", proposalEventId: 6, taskId: "t-created" } },
    { resolution: { status: "realized", proposalEventId: 7, taskId: "https://untrusted.invalid" } },
    { resolution: { status: "ready", proposalEventId: 7, taskId: "t-created" } },
  ])("ignores malformed or stale persisted terminal hints %#", (changes) => {
    const reference = {
      ...linked.proposal_ref!,
      resolution: { status: "realized", proposalEventId: 7, taskId: "t-created" },
      ...changes,
    } as NonNullable<TaskCreateDraft["proposal_ref"]>;
    expect(retainedProposalResolution(reference)).toBeUndefined();
  });

  it("allows newer terminal evidence only as a hint, never as an exact clear fence", () => {
    const next = { ...realized, proposal_event_id: 9 };
    const resolution = taskProposalResolution(next);
    expect(retainedProposalResolution({ ...linked.proposal_ref!, resolution })).toEqual({ status: "realized", proposalEventId: 9, taskId: "t-created" });
    expect(canClearRealizedProposal(linked, next)).toBe(false);
  });
});
