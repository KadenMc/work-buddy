import type { TaskProposal } from "../contracts";

export const isProposalId = (value: string): boolean => /^th-[a-zA-Z0-9_-]{1,80}$/.test(value);
export const isTaskId = (value: string): boolean => /^t-[a-zA-Z0-9_-]{1,80}$/.test(value);
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

/** Never use model- or server-supplied navigation URLs without validating identity. */
export function parseTaskProposalEnvelope(value: unknown): TaskProposal {
  if (!record(value) || value.ok !== true || !record(value.proposal)) {
    throw new Error("The task proposal response is invalid.");
  }
  const proposal = value.proposal;
  if (proposal.status === "unavailable" && record(proposal.error)) {
    throw Object.assign(new Error(typeof proposal.error.message === "string" ? proposal.error.message : "This task proposal is unavailable."), {
      code: typeof proposal.error.code === "string" ? proposal.error.code : "proposal_unavailable",
    });
  }
  if (
    typeof proposal.thread_id !== "string" || !isProposalId(proposal.thread_id) ||
    typeof proposal.proposal_event_id !== "number" || !Number.isSafeInteger(proposal.proposal_event_id) || proposal.proposal_event_id < 1 ||
    !record(proposal.parameters) || !record(proposal.origin) ||
    !["ready", "executing", "realized", "rejected", "needs_attention", "unavailable"].includes(String(proposal.status))
  ) throw new Error("The task proposal has an invalid identity or state.");
  let realization: TaskProposal["realization"] = null;
  if (proposal.realization !== null && proposal.realization !== undefined) {
    const result = proposal.realization;
    if (
      !record(result) || typeof result.task_id !== "string" || !isTaskId(result.task_id) ||
      typeof result.receipt_id !== "string" || !result.receipt_id ||
      typeof result.task_revision !== "number" || !Number.isSafeInteger(result.task_revision) || result.task_revision < 0
    ) throw new Error("The task proposal realization is invalid.");
    realization = {
      task_id: result.task_id,
      receipt_id: result.receipt_id,
      task_revision: result.task_revision,
      href: `/app/tasks?task=${encodeURIComponent(result.task_id)}`,
    };
  }
  if (proposal.status === "realized" && realization === null) {
    throw new Error("The task proposal is missing its task receipt.");
  }
  return {
    thread_id: proposal.thread_id,
    proposal_event_id: proposal.proposal_event_id,
    status: proposal.status as TaskProposal["status"],
    parameters: proposal.parameters,
    origin: proposal.origin,
    realization,
    href: `/app/tasks?proposal=${encodeURIComponent(proposal.thread_id)}`,
  };
}
