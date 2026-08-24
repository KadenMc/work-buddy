import {
  TASK_LENSES,
  type TaskAccess,
  type TaskActionItem,
  type TaskBatchPreview,
  type TaskDetail,
  type TaskDocumentSummary,
  type TaskFacets,
  type TaskHistoryEntry,
  type TaskLens,
  type TaskLocalFileLink,
  type TaskOptions,
  type TaskQueryState,
  type TaskSummary,
  type TasksViewModel,
  type TaskUrgency,
} from "../contracts";

export interface TaskViewApiResponse {
  readonly ok: true;
  readonly collection_revision: number;
  readonly observed_at: string;
  readonly access: { readonly mode: "read_write" | "read_only"; readonly reason?: string };
  readonly query: {
    readonly lens: TaskLens;
    readonly q?: string;
    readonly project?: string;
    readonly namespace?: string;
    readonly urgency?: string;
    readonly due?: string;
    readonly state?: string;
    readonly note?: string;
    readonly task?: string | null;
  };
  readonly facets: unknown;
  readonly tasks: readonly unknown[];
  readonly selected_task: unknown | null;
  readonly options: unknown;
}

export interface TaskMutationApiEnvelope {
  readonly ok: true;
  readonly task?: unknown;
  readonly tasks?: readonly unknown[];
  readonly collection_revision: number;
  readonly receipt?: unknown;
}

export interface TaskApiErrorEnvelope {
  readonly code: string;
  readonly message: string;
  readonly field_errors?: Readonly<Record<string, string>>;
  readonly retryable?: boolean;
  readonly current_revision?: number;
  readonly current_task?: unknown;
}

export class TasksApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly fieldErrors: Readonly<Record<string, string>>;
  readonly currentRevision?: number;
  readonly currentTask?: TaskDetail;

  constructor(status: number, envelope: TaskApiErrorEnvelope) {
    super(envelope.message);
    this.name = "TasksApiError";
    this.code = envelope.code;
    this.status = status;
    this.fieldErrors = envelope.field_errors ?? {};
    this.currentRevision = envelope.current_revision;
    this.currentTask = envelope.current_task === undefined
      ? undefined
      : parseTaskDetail(envelope.current_task);
  }
}

const record = (value: unknown, label: string): Record<string, unknown> => {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
};

const string = (value: unknown, label: string): string => {
  if (typeof value !== "string") throw new Error(`${label} must be a string.`);
  return value;
};

const optionalString = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 ? value : null;

const integer = (value: unknown, label: string): number => {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer.`);
  }
  return value;
};

const identifier = (value: unknown, label: string): string => {
  if (typeof value === "string" && value.length > 0) return value;
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) {
    return String(value);
  }
  throw new Error(`${label} must be a string or non-negative integer.`);
};

const strings = (value: unknown): readonly string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

const urgency = (value: unknown): TaskUrgency =>
  value === "low" || value === "high" || value === "critical"
    ? value === "critical" ? "high" : value
    : "medium";

const optionList = (value: unknown): TaskOptions["projects"] => {
  if (!Array.isArray(value)) return [];
  return value.flatMap((candidate) => {
    if (typeof candidate === "string") return [{ value: candidate, label: candidate }];
    if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) return [];
    const item = candidate as Record<string, unknown>;
    if (typeof item.value !== "string") return [];
    return [{ value: item.value, label: typeof item.label === "string" ? item.label : item.value }];
  });
};

export function parseTaskOptions(value: unknown): TaskOptions {
  const item = record(value ?? {}, "Task options");
  return {
    projects: optionList(item.projects),
    namespaces: optionList(item.namespaces),
    contracts: optionList(item.contracts),
    contexts: optionList(item.contexts),
  };
}

export function parseTaskSummary(value: unknown): TaskSummary {
  const task = record(value, "Task");
  const taskId = task.task_id ?? task.taskId;
  const title = task.title ?? task.description;
  const attentionState = task.attention_state ?? task.state;
  return {
    task_id: string(taskId, "Task ID"),
    title: string(title, "Task title"),
    revision: integer(task.revision, "Task revision"),
    attention_state: typeof attentionState === "string" ? attentionState : "inbox",
    urgency: urgency(task.urgency),
    due_date: optionalString(task.due_date),
    deadline_date: optionalString(task.deadline_date),
    snooze_until: optionalString(task.snooze_until),
    project: optionalString(task.project),
    namespaces: strings(task.namespaces ?? task.namespace_tags),
    tags: strings(task.tags),
    current_action: optionalString(task.current_action),
    has_document: task.has_document === true || task.document_state === "available",
    completed_at: optionalString(task.completed_at),
    archived_at: optionalString(task.archived_at),
    deleted_at: optionalString(task.deleted_at),
    updated_at: typeof task.updated_at === "string" ? task.updated_at : "",
  };
}

const parseActionItem = (value: unknown): TaskActionItem => {
  const item = record(value, "Action item");
  const approval = item.approval_state;
  return {
    action_item_id: identifier(item.action_item_id ?? item.id, "Action item ID"),
    text: string(item.text ?? item.description, "Action item text"),
    position: typeof item.position === "number"
      ? item.position
      : typeof item.sequence === "number"
        ? item.sequence
        : 0,
    completed: item.completed === true || item.state === "done",
    current: item.current === true,
    approval_state:
      approval === "pending" || approval === "approved" || approval === "rejected"
        ? approval
        : item.authorship === "agent_unapproved"
          ? "pending"
          : item.authorship === "agent_approved"
            ? "approved"
        : "not_required",
    deleted_at: optionalString(item.deleted_at),
  };
};

const parseHistory = (value: unknown): TaskHistoryEntry => {
  const item = record(value, "Task history entry");
  return {
    history_id: identifier(item.history_id ?? item.id, "History ID"),
    occurred_at: string(item.occurred_at ?? item.created_at, "History time"),
    actor: typeof item.actor === "string" ? item.actor : "Work Buddy",
    action: string(item.action, "History action"),
    summary: typeof item.summary === "string" ? item.summary : string(item.action, "History action"),
  };
};

const parseDocument = (value: unknown): TaskDocumentSummary => {
  if (value === null || value === undefined) {
    return { state: "missing", store_id: null, document_id: null, excerpt: null, updated_at: null, updated_by: null, href: null };
  }
  const item = record(value, "Task document");
  const state = item.state;
  return {
    state:
      state === "available" || state === "creating" || state === "unavailable"
        ? state
        : "missing",
    store_id: optionalString(item.store_id),
    document_id: optionalString(item.document_id),
    excerpt: optionalString(item.excerpt),
    updated_at: optionalString(item.updated_at),
    updated_by: optionalString(item.updated_by),
    href: optionalString(item.href),
  };
};

const parseLocalFile = (value: unknown): TaskLocalFileLink => {
  const item = record(value, "Linked local file");
  const action = item.allowed_action;
  const availability = item.availability;
  return {
    link_id: string(item.link_id, "Local link ID"),
    display_name: string(item.display_name, "Local file name"),
    media_type: typeof item.media_type === "string" ? item.media_type : "application/octet-stream",
    byte_length: integer(item.byte_length ?? 0, "Local file size"),
    sensitivity: typeof item.sensitivity === "string" ? item.sensitivity : "ordinary",
    allowed_action: action === "reveal" ? "reveal" : "open",
    availability:
      availability === "available" || availability === "missing" || availability === "changed"
        ? availability
        : "unavailable",
    host_action_available: item.host_action_available === true,
    unavailable_reason: optionalString(item.unavailable_reason),
  };
};

export function parseTaskDetail(value: unknown): TaskDetail {
  const raw = record(value, "Task detail");
  const base = parseTaskSummary(raw);
  const provenance = raw.provenance === undefined ? {} : record(raw.provenance, "Task provenance");
  const currentActionItemId = raw.current_action_item_id;
  const actionItems = Array.isArray(raw.action_items)
    ? raw.action_items.map(parseActionItem).map((item) => ({
        ...item,
        current:
          item.current ||
          (currentActionItemId !== null &&
            currentActionItemId !== undefined &&
            item.action_item_id === String(currentActionItemId)),
      }))
    : [];
  return {
    ...base,
    summary: typeof raw.summary === "string" ? raw.summary : "",
    desired_outcome: typeof raw.desired_outcome === "string" ? raw.desired_outcome : "",
    next_action: typeof raw.next_action === "string" ? raw.next_action : "",
    definition_of_done: typeof raw.definition_of_done === "string" ? raw.definition_of_done : "",
    dependencies: strings(raw.dependencies),
    contract: optionalString(raw.contract),
    required_contexts: strings(raw.required_contexts),
    automation_tier: optionalString(raw.automation_tier),
    provenance: {
      created_by: typeof provenance.created_by === "string" ? provenance.created_by : "Unknown",
      created_at: typeof provenance.created_at === "string" ? provenance.created_at : "",
      source: typeof provenance.source === "string" ? provenance.source : "task service",
    },
    action_items: actionItems,
    history: Array.isArray(raw.history) ? raw.history.map(parseHistory) : [],
    document: parseDocument(raw.document),
    local_files: Array.isArray(raw.local_files) ? raw.local_files.map(parseLocalFile) : [],
    local_files_error: optionalString(raw.local_files_error),
  };
}

const counts = (value: unknown): Readonly<Record<TaskLens, number>> => {
  const source = value === null || typeof value !== "object" || Array.isArray(value)
    ? {}
    : value as Record<string, unknown>;
  return Object.fromEntries(
    TASK_LENSES.map((lens) => [lens, typeof source[lens] === "number" ? source[lens] : 0]),
  ) as unknown as Readonly<Record<TaskLens, number>>;
};

const countMap = (value: unknown): Readonly<Record<string, number>> => {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).filter(
      (entry): entry is [string, number] => typeof entry[1] === "number",
    ),
  );
};

const parseFacets = (value: unknown): TaskFacets => {
  const item = record(value ?? {}, "Task facets");
  return {
    counts: counts(item.counts ?? item.lenses),
    projects: countMap(item.projects),
    namespaces: countMap(item.namespaces),
    urgencies: countMap(item.urgencies),
  };
};

const parseQuery = (value: unknown): TaskQueryState => {
  const item = record(value ?? {}, "Task query");
  const lens = TASK_LENSES.includes(item.lens as TaskLens) ? item.lens as TaskLens : "inbox";
  return {
    lens,
    q: typeof item.q === "string" ? item.q : "",
    project: typeof item.project === "string" ? item.project : "",
    namespace: typeof item.namespace === "string" ? item.namespace : "",
    urgency: typeof item.urgency === "string" ? item.urgency : "",
    due: typeof item.due === "string" ? item.due : "",
    state: typeof item.state === "string" ? item.state : "",
    note: typeof item.note === "string" ? item.note : "",
    task: optionalString(item.task),
  };
};

export function parseTaskViewPayload(payload: unknown): TasksViewModel {
  const root = record(payload, "Tasks response");
  if (root.ok !== true) throw new Error("Tasks endpoint did not acknowledge the request.");
  const view = root.view === undefined ? root : record(root.view, "Tasks view");
  const tasks = Array.isArray(view.tasks) ? view.tasks.map(parseTaskSummary) : [];
  const selected = view.selected_task ?? view.selectedTask;
  return {
    schemaVersion: 1,
    revision: integer(view.collection_revision ?? view.revision, "Collection revision"),
    observedAt: typeof view.observed_at === "string" ? view.observed_at : new Date().toISOString(),
    access: parseTaskAccess(view.access),
    query: parseQuery(view.query),
    facets: parseFacets(view.facets),
    tasks,
    selectedTask: selected === null || selected === undefined ? null : parseTaskDetail(selected),
    options: parseTaskOptions(view.options),
  };
}

export function parseTaskAccess(value: unknown): TaskAccess {
  const item = value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
  if (item.mode === "read_write") return { mode: "read_write" };
  return {
    mode: "read_only",
    reason: typeof item.reason === "string" && item.reason.length > 0
      ? item.reason
      : "Task editing is temporarily unavailable.",
  };
}

export function parseBatchPreview(value: unknown): TaskBatchPreview {
  const root = record(value, "Task batch preview response");
  if (root.ok !== true) throw new Error("Task batch preview was not acknowledged.");
  const preview = record(root.preview, "Task batch preview");
  if (!Array.isArray(preview.rows) || !Array.isArray(preview.accepted_indices)) {
    throw new Error("Task batch preview rows are invalid.");
  }
  const rows: TaskBatchPreview["rows"] = preview.rows.map((value) => {
    const row = record(value, "Task batch preview row");
    const rawErrors = row.field_errors;
    const fieldErrors = rawErrors !== null && typeof rawErrors === "object" && !Array.isArray(rawErrors)
      ? Object.fromEntries(
          Object.entries(rawErrors).filter(
            (entry): entry is [string, string] => typeof entry[1] === "string",
          ),
        )
      : {};
    const duplicateReason = row.duplicate_reason;
    return {
      index: integer(row.index, "Task batch row index"),
      title: string(row.title, "Task batch row title"),
      valid: row.valid === true,
      field_errors: fieldErrors,
      duplicate: row.duplicate === true,
      duplicate_reason:
        duplicateReason === "batch" || duplicateReason === "existing_title"
          ? duplicateReason
          : null,
      will_create: row.will_create === true,
    };
  });
  const acceptedIndices = preview.accepted_indices.map((value) =>
    integer(value, "Task batch accepted index")
  );
  return {
    rows,
    accepted_indices: acceptedIndices,
    accepted_count: integer(preview.accepted_count, "Task batch accepted count"),
    can_commit: preview.can_commit === true,
    collection_revision: integer(preview.collection_revision, "Collection revision"),
    preview_token: string(preview.preview_token, "Task batch preview token"),
  };
}

export function parseMutationEnvelope(value: unknown): {
  readonly collectionRevision: number;
  readonly task?: TaskDetail;
  readonly tasks?: readonly TaskSummary[];
  readonly receipt?: unknown;
} {
  const root = record(value, "Task mutation response");
  if (root.ok !== true) throw new Error("Task mutation was not acknowledged.");
  const result = root.result === undefined ? root : record(root.result, "Task mutation result");
  return {
    collectionRevision: integer(
      result.collection_revision ?? root.collection_revision,
      "Collection revision",
    ),
    ...(result.task === undefined ? {} : { task: parseTaskDetail(result.task) }),
    ...(Array.isArray(result.tasks) ? { tasks: result.tasks.map(parseTaskSummary) } : {}),
    ...(result.receipt === undefined ? {} : { receipt: result.receipt }),
  };
}

export function parseTaskApiError(status: number, value: unknown): TasksApiError {
  const root = record(value, "Task error");
  const error = root.error === undefined ? root : record(root.error, "Task error");
  const fieldErrors = error.field_errors;
  const envelope: TaskApiErrorEnvelope = {
    code: typeof error.code === "string" ? error.code : `http_${status}`,
    message: typeof error.message === "string" ? error.message : `Tasks request failed (HTTP ${status}).`,
    ...(fieldErrors !== null && typeof fieldErrors === "object" && !Array.isArray(fieldErrors)
      ? { field_errors: Object.fromEntries(Object.entries(fieldErrors).filter((entry): entry is [string, string] => typeof entry[1] === "string")) }
      : {}),
    ...(typeof error.current_revision === "number" ? { current_revision: error.current_revision } : {}),
    ...(error.current_task === undefined ? {} : { current_task: error.current_task }),
  };
  return new TasksApiError(status, envelope);
}
