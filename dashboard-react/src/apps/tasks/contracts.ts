import type { WidgetIntent } from "../../dashboard/contributions/contracts";

export const TASK_LENSES = [
  "focused",
  "inbox",
  "active",
  "snoozed",
  "completed",
  "trash",
  "triage",
] as const;

export type TaskLens = (typeof TASK_LENSES)[number];
export type TaskUrgency = "low" | "medium" | "high";
export type TaskAttentionState = "inbox" | "mit" | "focused" | "active" | "waiting" | "snoozed";

export interface TaskAccess {
  readonly mode: "read_write" | "read_only";
  readonly reason?: string;
}

export interface TaskProjectOption {
  readonly value: string;
  readonly label: string;
}

export interface TaskOptions {
  readonly projects: readonly TaskProjectOption[];
  readonly namespaces: readonly TaskProjectOption[];
  readonly contracts: readonly TaskProjectOption[];
  readonly contexts: readonly TaskProjectOption[];
}

export interface TaskQueryState {
  readonly lens: TaskLens;
  readonly q: string;
  readonly project: string;
  readonly namespace: string;
  readonly urgency: string;
  readonly due: string;
  readonly state: string;
  readonly note: string;
  readonly task: string | null;
  readonly proposal?: string | null;
}

/** Threads owns these pending actions. They are not TaskStore task records. */
export interface TaskProposal {
  readonly thread_id: string;
  readonly proposal_event_id: number;
  readonly status: "ready" | "executing" | "realized" | "rejected" | "needs_attention" | "unavailable";
  readonly parameters: Readonly<Record<string, unknown>>;
  readonly origin: Readonly<Record<string, unknown>>;
  readonly realization: {
    readonly task_id: string;
    readonly receipt_id: string;
    readonly task_revision: number;
    readonly href: string;
  } | null;
  readonly href: string;
}

export type TaskProposalSelection =
  | { readonly kind: "loaded"; readonly proposal: TaskProposal }
  | { readonly kind: "unavailable"; readonly threadId: string; readonly code: string; readonly message: string };

export interface TaskFacets {
  readonly counts: Readonly<Record<TaskLens, number>>;
  readonly projects: Readonly<Record<string, number>>;
  readonly namespaces: Readonly<Record<string, number>>;
  readonly urgencies: Readonly<Record<string, number>>;
}

export interface TaskSummary {
  readonly task_id: string;
  readonly title: string;
  readonly revision: number;
  readonly attention_state: string;
  readonly urgency: TaskUrgency;
  readonly due_date: string | null;
  readonly deadline_date: string | null;
  readonly snooze_until: string | null;
  readonly project: string | null;
  readonly namespaces: readonly string[];
  readonly tags: readonly string[];
  readonly current_action: string | null;
  readonly has_document: boolean;
  readonly completed_at: string | null;
  readonly archived_at: string | null;
  readonly deleted_at: string | null;
  readonly updated_at: string;
}

export interface TaskActionItem {
  readonly action_item_id: string;
  readonly text: string;
  readonly position: number;
  readonly completed: boolean;
  readonly current: boolean;
  readonly approval_state: "not_required" | "pending" | "approved" | "rejected";
  readonly deleted_at: string | null;
}

export interface TaskHistoryEntry {
  readonly history_id: string;
  readonly occurred_at: string;
  readonly actor: string;
  readonly action: string;
  readonly summary: string;
}

export interface TaskDocumentSummary {
  readonly state: "missing" | "available" | "creating" | "unavailable";
  readonly store_id: string | null;
  readonly document_id: string | null;
  readonly excerpt: string | null;
  readonly updated_at: string | null;
  readonly updated_by: string | null;
  readonly href: string | null;
}

export interface TaskLocalFileLink {
  readonly link_id: string;
  readonly display_name: string;
  readonly media_type: string;
  readonly byte_length: number;
  readonly sensitivity: string;
  readonly allowed_action: "open" | "reveal";
  readonly availability: "available" | "missing" | "changed" | "unavailable";
  readonly host_action_available: boolean;
  readonly unavailable_reason: string | null;
}

export interface TaskBatchPreviewRow {
  readonly index: number;
  readonly title: string;
  readonly valid: boolean;
  readonly field_errors: Readonly<Record<string, string>>;
  readonly duplicate: boolean;
  readonly duplicate_reason: "batch" | "existing_title" | null;
  readonly will_create: boolean;
}

export interface TaskBatchPreview {
  readonly rows: readonly TaskBatchPreviewRow[];
  readonly accepted_indices: readonly number[];
  readonly accepted_count: number;
  readonly can_commit: boolean;
  readonly collection_revision: number;
  readonly preview_token: string;
}

export interface TaskDetail extends TaskSummary {
  readonly summary: string;
  readonly desired_outcome: string;
  readonly next_action: string;
  readonly definition_of_done: string;
  readonly dependencies: readonly string[];
  readonly contract: string | null;
  readonly required_contexts: readonly string[];
  readonly automation_tier: string | null;
  readonly provenance: {
    readonly created_by: string;
    readonly created_at: string;
    readonly source: string;
  };
  readonly action_items: readonly TaskActionItem[];
  readonly history: readonly TaskHistoryEntry[];
  readonly document: TaskDocumentSummary;
  readonly local_files: readonly TaskLocalFileLink[];
  readonly local_files_error: string | null;
}

export interface TaskQuickAddInput {
  readonly instanceId: string;
  readonly revision: number;
  readonly access: TaskAccess;
  readonly options: TaskOptions;
  /** Current selected Thread projection, for explicit recovery of a linked draft. */
  readonly selectedProposal?: TaskProposal | null;
  /** Last validated Thread projection observed by this provider, independent of URL selection. */
  readonly observedProposal?: TaskProposal | null;
}

export interface TaskWorkspaceInput {
  readonly instanceId: string;
  readonly revision: number;
  readonly access: TaskAccess;
  readonly query: TaskQueryState;
  readonly facets: TaskFacets;
  readonly tasks: readonly TaskSummary[];
  readonly selectedTask: TaskDetail | null;
  readonly selectedProposal?: TaskProposalSelection | null;
  readonly options: TaskOptions;
}

export interface TasksViewModel {
  readonly schemaVersion: 1;
  readonly revision: number;
  readonly observedAt: string;
  readonly access: TaskAccess;
  readonly query: TaskQueryState;
  readonly facets: TaskFacets;
  readonly tasks: readonly TaskSummary[];
  readonly selectedTask: TaskDetail | null;
  readonly selectedProposal?: TaskProposalSelection | null;
  readonly options: TaskOptions;
}

export const TASK_INTENTS = {
  create: "wb.tasks.task.create",
  proposalCreate: "wb.tasks.proposal.create",
  proposalRevise: "wb.tasks.proposal.revise",
  proposalAccept: "wb.tasks.proposal.accept",
  proposalReject: "wb.tasks.proposal.reject",
  batchPreview: "wb.tasks.task.batch-preview",
  batchCreate: "wb.tasks.task.batch-create",
  update: "wb.tasks.task.update",
  complete: "wb.tasks.task.complete",
  reopen: "wb.tasks.task.reopen",
  focus: "wb.tasks.task.focus",
  snooze: "wb.tasks.task.snooze",
  archive: "wb.tasks.task.archive",
  unarchive: "wb.tasks.task.unarchive",
  delete: "wb.tasks.task.delete",
  restore: "wb.tasks.task.restore",
  replaceTags: "wb.tasks.task.tags.replace",
  createDocument: "wb.tasks.task.document.create",
  openDocument: "wb.tasks.document.open",
  localFileAction: "wb.tasks.local-file.action",
  actionItemCreate: "wb.tasks.action-item.create",
  actionItemUpdate: "wb.tasks.action-item.update",
  actionItemReorder: "wb.tasks.action-item.reorder",
  actionItemCurrent: "wb.tasks.action-item.current",
  actionItemApprove: "wb.tasks.action-item.approve",
  actionItemDelete: "wb.tasks.action-item.delete",
  actionItemRestore: "wb.tasks.action-item.restore",
  locationChange: "wb.tasks.location.change",
} as const;

export type TaskIntentType = (typeof TASK_INTENTS)[keyof typeof TASK_INTENTS];
export type TaskWidgetIntent = WidgetIntent<Record<string, unknown>>;
