import type { JsonValue } from "../../../dashboard/contributions/contracts";
import type { TaskOptions, TaskProposal, TaskUrgency } from "../contracts";
import { isProposalId, isTaskId } from "../providers/proposalApiContract";

/** Presentation-only terminal evidence. Never authorizes a proposal mutation. */
export type TaskProposalResolution = {
  readonly status: "realized";
  readonly proposalEventId: number;
  readonly taskId: string;
} | {
  readonly status: "rejected";
  readonly proposalEventId: number;
};

export interface TaskCreateDraft {
  readonly title: string;
  readonly attention_state: string;
  readonly urgency: TaskUrgency;
  readonly due_date: string;
  readonly deadline_date: string;
  readonly project: string;
  readonly namespaces: string;
  readonly summary: string;
  readonly desired_outcome: string;
  readonly next_action: string;
  readonly definition_of_done: string;
  readonly dependencies: string;
  readonly batch_lines: readonly string[];
  /** Host-only linkage; never an assistable field or a second proposal authority. */
  readonly proposal_ref?: {
    readonly threadId: string;
    readonly proposalEventId: number;
    readonly draftFingerprint: string;
    readonly requiresDetailedReview?: boolean;
    readonly resolution?: TaskProposalResolution;
  };
  /** Retain the exact ingress/revision until an uncertain response can be replayed. */
  readonly proposal_pending?: {
    readonly clientMutationId: string;
    readonly parameters: Readonly<Record<string, JsonValue>>;
    readonly origin: Readonly<Record<string, JsonValue>>;
    readonly draftFingerprint: string;
    readonly revisionOf?: { readonly threadId: string; readonly proposalEventId: number };
  };
}

export const EMPTY_TASK_CREATE_DRAFT: TaskCreateDraft = {
  title: "", attention_state: "inbox", urgency: "medium", due_date: "", deadline_date: "",
  project: "", namespaces: "", summary: "", desired_outcome: "", next_action: "",
  definition_of_done: "", dependencies: "", batch_lines: [],
};

export const isTaskCreateDraftPristine = (value: TaskCreateDraft): boolean =>
  JSON.stringify(value) === JSON.stringify(EMPTY_TASK_CREATE_DRAFT);

export const taskDraftCsv = (value: string): readonly string[] =>
  value.split(",").map((part) => part.trim()).filter(Boolean);
const optional = (value: string): string | null => value.trim() || null;

export const taskDraftFields = (value: TaskCreateDraft): Readonly<Record<string, JsonValue>> => ({
  title: value.title.trim(), attention_state: value.attention_state, urgency: value.urgency,
  due_date: optional(value.due_date), deadline_date: optional(value.deadline_date),
  project: optional(value.project), namespaces: taskDraftCsv(value.namespaces),
  summary: optional(value.summary), desired_outcome: optional(value.desired_outcome),
  next_action: optional(value.next_action), definition_of_done: optional(value.definition_of_done),
  dependencies: taskDraftCsv(value.dependencies),
});

export const taskProposalParameters = (value: TaskCreateDraft): Readonly<Record<string, JsonValue>> => {
  const { title, attention_state, namespaces, desired_outcome, next_action, ...fields } = taskDraftFields(value);
  return { task_text: title!, state: attention_state!, tags: namespaces!, outcome_text: desired_outcome!, next_action_text: next_action!, ...fields };
};

const editableProposalFields = new Set(Object.keys(taskProposalParameters(EMPTY_TASK_CREATE_DRAFT)));
/** Other standard task_create settings must be visible in full proposal review. */
export const additionalTaskProposalParameters = (proposal: TaskProposal): readonly (readonly [string, unknown])[] =>
  Object.entries(proposal.parameters).filter(([key]) => !editableProposalFields.has(key));

/** A local comparison only, not an authorization hash. Metadata is deliberately excluded. */
export const taskDraftFingerprint = (value: TaskCreateDraft): string => JSON.stringify(taskProposalParameters(value));

export function taskProposalResolution(proposal: TaskProposal): TaskProposalResolution | undefined {
  if (!isProposalId(proposal.thread_id) || !Number.isSafeInteger(proposal.proposal_event_id) || proposal.proposal_event_id < 1) return undefined;
  if (proposal.status === "rejected") return { status: "rejected", proposalEventId: proposal.proposal_event_id };
  if (proposal.status === "realized" && proposal.realization !== null && isTaskId(proposal.realization.task_id)) {
    return { status: "realized", proposalEventId: proposal.proposal_event_id, taskId: proposal.realization.task_id };
  }
  return undefined;
}

/** Old/local draft metadata may only suppress decisions, never enable them. */
export function retainedProposalResolution(reference: TaskCreateDraft["proposal_ref"]): TaskProposalResolution | undefined {
  if (!reference || !isProposalId(reference.threadId) || !Number.isSafeInteger(reference.proposalEventId) || reference.proposalEventId < 1) return undefined;
  const resolution = reference.resolution;
  if (!resolution || !Number.isSafeInteger(resolution.proposalEventId) || resolution.proposalEventId < reference.proposalEventId) return undefined;
  if (resolution.status === "rejected") return { status: "rejected", proposalEventId: resolution.proposalEventId };
  if (resolution.status === "realized" && typeof resolution.taskId === "string" && isTaskId(resolution.taskId)) {
    return { status: "realized", proposalEventId: resolution.proposalEventId, taskId: resolution.taskId };
  }
  return undefined;
}

export function canClearRealizedProposal(value: TaskCreateDraft, proposal: TaskProposal): boolean {
  const reference = value.proposal_ref;
  return taskProposalResolution(proposal)?.status === "realized"
    && reference !== undefined && reference.threadId === proposal.thread_id
    && reference.proposalEventId === proposal.proposal_event_id
    && value.proposal_pending === undefined && value.batch_lines.length === 0
    && reference.draftFingerprint === taskDraftFingerprint(value)
    && reference.draftFingerprint === taskDraftFingerprint(draftFromTaskProposal(proposal));
}

export async function taskDraftSha256(value: TaskCreateDraft): Promise<string> {
  const bytes = new TextEncoder().encode(taskDraftFingerprint(value));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function draftFromTaskProposal(proposal: TaskProposal): TaskCreateDraft {
  const fields = proposal.parameters;
  const text = (key: string, fallback = "") => typeof fields[key] === "string" ? fields[key] as string : fallback;
  const csv = (key: string) => Array.isArray(fields[key]) ? (fields[key] as unknown[]).filter((value) => typeof value === "string").join(", ") : text(key);
  return {
    ...EMPTY_TASK_CREATE_DRAFT,
    title: text("task_text", text("title")), attention_state: text("state", text("attention_state", "inbox")),
    urgency: fields.urgency === "low" || fields.urgency === "high" ? fields.urgency : "medium",
    due_date: text("due_date"), deadline_date: text("deadline_date"), project: text("project"),
    namespaces: csv("tags") || csv("namespaces"), summary: text("summary"), desired_outcome: text("outcome_text", text("desired_outcome")),
    next_action: text("next_action_text", text("next_action")), definition_of_done: text("definition_of_done"), dependencies: csv("dependencies"),
  };
}

export function newTaskStructures(value: TaskCreateDraft, options: TaskOptions): readonly string[] {
  const projects = new Set(options.projects.map((option) => option.value.toLocaleLowerCase()));
  const namespaces = new Set(options.namespaces.map((option) => option.value.toLocaleLowerCase()));
  return [
    ...(value.project.trim() && !projects.has(value.project.trim().toLocaleLowerCase()) ? [`project “${value.project.trim()}”`] : []),
    ...taskDraftCsv(value.namespaces).filter((namespace) => !namespaces.has(namespace.toLocaleLowerCase())).map((namespace) => `namespace “${namespace}”`),
  ];
}
