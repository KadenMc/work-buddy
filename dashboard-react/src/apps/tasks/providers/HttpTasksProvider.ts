import type {
  AppInvalidation,
  DashboardIntent,
  IntentResult,
  JsonValue,
  ReconcileResult,
  ViewId,
  ViewLoadRequest,
  ViewSnapshot,
  WidgetLoadRequest,
  WidgetSnapshot,
  WidgetTypeId,
} from "../../../dashboard/contributions/contracts";
import type { ViewLocationAdapter } from "../../../dashboard/contributions/viewModules";
import type { ViewProvider } from "../../../dashboard/providers/ViewProvider";
import { exactHumanAuthorityHeaders } from "../../../security/humanAuthority";
import {
  TASKS_APP_ID,
  TASKS_INSTANCE_IDS,
  TASKS_VIEW_ID,
  TASKS_WIDGET_TYPE_IDS,
} from "../bindings";
import {
  TASK_INTENTS,
  TASK_LENSES,
  type TaskQuickAddInput,
  type TasksViewModel,
  type TaskWorkspaceInput,
} from "../contracts";
import {
  parseBatchPreview,
  parseMutationEnvelope,
  parseTaskApiError,
  parseTaskViewPayload,
  type TasksApiError,
} from "./taskApiContract";

export interface HttpTasksProviderOptions {
  readonly fetchImpl?: typeof fetch;
  readonly location: ViewLocationAdapter;
  readonly navigate?: (href: string) => void;
  readonly clock?: () => string;
}

type TasksSnapshot = ViewSnapshot<TasksViewModel, unknown, TaskQuickAddInput | TaskWorkspaceInput>;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

const taskIdFrom = (payload: Record<string, unknown>): string | null =>
  typeof payload.task_id === "string" && payload.task_id.length > 0 ? payload.task_id : null;

const mutationId = (intent: DashboardIntent): string =>
  intent.client_mutation_id ?? intent.intent_id;

const safeBody = (intent: DashboardIntent): Record<string, unknown> => {
  if (!isRecord(intent.payload)) return { client_mutation_id: mutationId(intent) };
  return { ...intent.payload, client_mutation_id: mutationId(intent) };
};

interface MutationSpec {
  readonly method: "POST" | "PATCH" | "PUT" | "DELETE";
  readonly path: string;
  readonly operation: string;
  readonly subject: string;
  readonly preview?: boolean;
}

const itemIdFrom = (payload: Record<string, unknown>): string | null =>
  typeof payload.action_item_id === "string" && payload.action_item_id.length > 0
    ? payload.action_item_id
    : null;

function mutationSpec(intent: DashboardIntent, body: Record<string, unknown>): MutationSpec | null {
  const taskId = taskIdFrom(body);
  const taskPath = taskId === null ? null : `/api/tasks/${encodeURIComponent(taskId)}`;
  const existing = (operation: string, suffix = "", method: MutationSpec["method"] = "POST") =>
    taskId === null || taskPath === null
      ? null
      : {
          method,
          path: `${taskPath}${suffix}`,
          operation,
          subject: `task:${taskId}`,
        } satisfies MutationSpec;
  switch (intent.intent_type) {
    case TASK_INTENTS.create:
      return {
        method: "POST",
        path: "/api/tasks",
        operation: "create",
        subject: `task:new:${mutationId(intent)}`,
      };
    case TASK_INTENTS.batchPreview:
      return {
        method: "POST",
        path: "/api/tasks/batch/preview",
        operation: "batch_preview",
        subject: `task-batch-preview:${mutationId(intent)}`,
        preview: true,
      };
    case TASK_INTENTS.batchCreate:
      return {
        method: "POST",
        path: "/api/tasks/batch",
        operation: "batch_create",
        subject: `task-batch:${mutationId(intent)}`,
      };
    case TASK_INTENTS.update:
      return existing("update", "", "PATCH");
    case TASK_INTENTS.complete:
      return existing("complete", "/complete");
    case TASK_INTENTS.reopen:
      return existing("reopen", "/reopen");
    case TASK_INTENTS.focus:
      return existing("focus", "/focus");
    case TASK_INTENTS.snooze:
      return existing("snooze", "/snooze");
    case TASK_INTENTS.archive:
      return existing("archive", "/archive");
    case TASK_INTENTS.unarchive:
      return existing("unarchive", "/unarchive");
    case TASK_INTENTS.delete:
      return existing("delete", "", "DELETE");
    case TASK_INTENTS.restore:
      return existing("restore", "/restore");
    case TASK_INTENTS.replaceTags:
      return existing("replace_tags", "/tags", "PUT");
    case TASK_INTENTS.createDocument:
      return existing("create_document", "/document");
    case TASK_INTENTS.localFileAction: {
      const linkId = typeof body.link_id === "string" ? body.link_id : null;
      const action = body.action;
      return linkId === null || (action !== "open" && action !== "reveal") ? null : existing(
        "local_file_action",
        `/local-files/${encodeURIComponent(linkId)}/action`,
      );
    }
    case TASK_INTENTS.actionItemCreate:
      return existing("action_item_create", "/action-items");
    case TASK_INTENTS.actionItemReorder:
      return existing("action_item_reorder", "/action-items/reorder");
    case TASK_INTENTS.actionItemUpdate:
    case TASK_INTENTS.actionItemCurrent:
    case TASK_INTENTS.actionItemApprove:
    case TASK_INTENTS.actionItemDelete:
    case TASK_INTENTS.actionItemRestore: {
      const itemId = itemIdFrom(body);
      if (itemId === null) return null;
      const suffix = intent.intent_type === TASK_INTENTS.actionItemCurrent
        ? "/current"
        : intent.intent_type === TASK_INTENTS.actionItemApprove
          ? "/approve"
          : intent.intent_type === TASK_INTENTS.actionItemRestore
            ? "/restore"
            : "";
      const method = intent.intent_type === TASK_INTENTS.actionItemUpdate
        ? "PATCH"
        : intent.intent_type === TASK_INTENTS.actionItemDelete
          ? "DELETE"
          : "POST";
      const operation = intent.intent_type.split(".").slice(-2).join("_").replace(/-/g, "_");
      return existing(operation, `/action-items/${encodeURIComponent(itemId)}${suffix}`, method);
    }
    default:
      return null;
  }
}

const canonicalSearch = (current: string, payload: Record<string, unknown>): string => {
  const params = new URLSearchParams(current.startsWith("?") ? current.slice(1) : current);
  const next = isRecord(payload.patch) ? payload.patch : payload;
  const keys = ["lens", "q", "project", "namespace", "urgency", "due", "state", "note", "task"];
  for (const key of keys) {
    const value = next[key];
    if (value === null || value === undefined || value === "") params.delete(key);
    else if (typeof value === "string") params.set(key, value);
  }
  const lens = params.get("lens");
  if (lens !== null && !TASK_LENSES.includes(lens as (typeof TASK_LENSES)[number])) {
    params.set("lens", "inbox");
  }
  params.delete("provider");
  const rendered = params.toString();
  return rendered.length === 0 ? "" : `?${rendered}`;
};

const validateCoworkHref = (href: unknown): string => {
  if (typeof href !== "string" || !href.startsWith("/app/cowork?")) {
    throw new Error("Tasks returned an unsafe Co-work route.");
  }
  const parsed = new URL(href, "http://work-buddy.local");
  if (parsed.origin !== "http://work-buddy.local" || parsed.pathname !== "/app/cowork") {
    throw new Error("Tasks returned an unsafe Co-work route.");
  }
  return `${parsed.pathname}${parsed.search}`;
};

const readJsonResponse = async (response: Response): Promise<unknown> => {
  const mediaType = response.headers.get("Content-Type")
    ?.split(";", 1)[0]
    ?.trim()
    .toLowerCase();
  if (mediaType !== "application/json" && mediaType?.endsWith("+json") !== true) {
    const message = response.status === 404
      ? "The Tasks API is unavailable. Restart work-buddy so the dashboard and API use the same build."
      : `The Tasks API returned ${mediaType ?? "a non-JSON response"}.`;
    throw Object.assign(new Error(message), { status: response.status });
  }
  try {
    return await response.json() as unknown;
  } catch {
    throw Object.assign(new Error("The Tasks API returned invalid JSON."), {
      status: response.status,
    });
  }
};

export class HttpTasksProvider implements ViewProvider {
  readonly appId = TASKS_APP_ID;
  readonly #fetch: typeof fetch;
  readonly #location: ViewLocationAdapter;
  readonly #navigate: (href: string) => void;
  readonly #clock: () => string;
  #last: TasksSnapshot | undefined;

  constructor(options: HttpTasksProviderOptions) {
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.#location = options.location;
    this.#navigate = options.navigate ?? ((href) => window.location.assign(href));
    this.#clock = options.clock ?? (() => new Date().toISOString());
  }

  async loadView(viewId: ViewId, _request: ViewLoadRequest): Promise<TasksSnapshot> {
    if (viewId !== TASKS_VIEW_ID) throw new Error(`HttpTasksProvider cannot load view ${viewId}`);
    const response = await this.#fetch(`/api/tasks/view${this.#location.getSearch()}`, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    const payload = await readJsonResponse(response);
    if (!response.ok) throw parseTaskApiError(response.status, payload);
    const model = parseTaskViewPayload(payload);
    const quickAdd: TaskQuickAddInput = {
      instanceId: TASKS_INSTANCE_IDS.quickAdd,
      revision: model.revision,
      access: model.access,
      options: model.options,
    };
    const workspace: TaskWorkspaceInput = {
      instanceId: TASKS_INSTANCE_IDS.workspace,
      revision: model.revision,
      access: model.access,
      query: model.query,
      facets: model.facets,
      tasks: model.tasks,
      selectedTask: model.selectedTask,
      options: model.options,
    };
    const snapshot: TasksSnapshot = {
      viewId: TASKS_VIEW_ID,
      revision: model.revision,
      observedAt: model.observedAt,
      status: model.access.mode === "read_only" ? "read-only" : "ready",
      quality: { kind: "complete" },
      model,
      bindings: {},
      widgetInputs: {
        [TASKS_INSTANCE_IDS.quickAdd]: quickAdd,
        [TASKS_INSTANCE_IDS.workspace]: workspace,
      },
    };
    this.#last = snapshot;
    return snapshot;
  }

  async loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<WidgetSnapshot> {
    if (request.viewId !== TASKS_VIEW_ID) {
      throw new Error(`HttpTasksProvider cannot load widgets for ${request.viewId}`);
    }
    const expected = request.instanceId === TASKS_INSTANCE_IDS.quickAdd
      ? TASKS_WIDGET_TYPE_IDS.quickAdd
      : request.instanceId === TASKS_INSTANCE_IDS.workspace
        ? TASKS_WIDGET_TYPE_IDS.workspace
        : null;
    if (expected !== widgetTypeId) {
      return {
        widgetTypeId,
        instanceId: request.instanceId,
        observedAt: this.#clock(),
        status: "unavailable",
        quality: { kind: "partial", message: "Widget is not bound to this Tasks slot." },
        input: null,
      };
    }
    const snapshot = this.#last ?? await this.loadView(TASKS_VIEW_ID, { reason: "refresh" });
    return {
      widgetTypeId,
      instanceId: request.instanceId,
      revision: snapshot.revision,
      observedAt: snapshot.observedAt,
      status: snapshot.status,
      quality: snapshot.quality,
      input: snapshot.widgetInputs[request.instanceId] ?? null,
    };
  }

  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    if (intent.view_id !== TASKS_VIEW_ID) return this.#result(intent, "rejected", "Intent targets another view.");
    if (!isRecord(intent.payload)) return this.#result(intent, "rejected", "Task action payload is invalid.");
    if (intent.intent_type === TASK_INTENTS.locationChange) {
      const search = canonicalSearch(this.#location.getSearch(), intent.payload);
      intent.payload.replace === true
        ? this.#location.replaceSearch(search)
        : this.#location.pushSearch(search);
      this.#last = undefined;
      // A location change changes the query even when the collection revision is
      // unchanged. Returning the old revision would let useViewSession short-circuit
      // reconciliation and leave the previous lens/filter rendered.
      return this.#result(intent, "accepted", "Task view updated.");
    }
    if (intent.intent_type === TASK_INTENTS.openDocument) {
      return this.#openDocument(intent, intent.payload);
    }
    const body = safeBody(intent);
    const spec = mutationSpec(intent, body);
    if (spec === null) return this.#result(intent, "rejected", "That task action is not valid.");
    try {
      const headers = spec.preview === true
        ? {}
        : await exactHumanAuthorityHeaders(
            {
              action: `dashboard.tasks.${spec.operation}`,
              subject: spec.subject,
              context: { method: spec.method, path: spec.path, body },
            },
            this.#fetch,
          );
      const response = await this.#fetch(spec.path, {
        method: spec.method,
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...headers,
          ...(spec.operation === "local_file_action"
            ? {
                "X-Work-Buddy-Intent":
                  body.action === "reveal"
                    ? "cowork-local-file-reveal"
                    : "cowork-local-file-open",
              }
            : {}),
        },
        body: JSON.stringify(body),
      });
      const payload = await readJsonResponse(response);
      if (!response.ok) throw parseTaskApiError(response.status, payload);
      if (spec.preview === true) {
        const preview = parseBatchPreview(payload);
        return {
          ...this.#result(intent, "accepted", "Task batch preview is ready.", preview.collection_revision),
          value: { preview },
        };
      }
      if (spec.operation === "local_file_action") {
        if (!isRecord(payload) || payload.ok !== true) {
          throw new Error("The linked-file action returned an invalid response.");
        }
        return {
          ...this.#result(intent, "accepted", "Linked local file action completed.", this.#last?.revision),
          value: {
            action: typeof payload.action === "string" ? payload.action : String(body.action),
            link_id: typeof payload.link_id === "string" ? payload.link_id : String(body.link_id),
          },
        };
      }
      const result = parseMutationEnvelope(payload);
      this.#last = undefined;
      return {
        ...this.#result(intent, "accepted", "Task saved.", result.collectionRevision),
        value: {
          ...(result.task === undefined ? {} : { task: result.task }),
          ...(result.tasks === undefined ? {} : { tasks: result.tasks }),
          ...(result.receipt === undefined ? {} : { receipt: result.receipt as JsonValue }),
        },
      };
    } catch (error) {
      return this.#errorResult(intent, error);
    }
  }

  async #openDocument(
    intent: DashboardIntent,
    payload: Record<string, unknown>,
  ): Promise<IntentResult> {
    const taskId = taskIdFrom(payload);
    if (taskId === null) return this.#result(intent, "rejected", "Choose a task first.");
    try {
      const response = await this.#fetch(`/api/tasks/${encodeURIComponent(taskId)}/document`, {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const raw = await readJsonResponse(response);
      if (!response.ok) throw parseTaskApiError(response.status, raw);
      if (!isRecord(raw)) throw new Error("Task document response is invalid.");
      const source = isRecord(raw.document) ? raw.document : raw;
      const href = validateCoworkHref(source.href ?? source.cowork_href ?? raw.cowork_href);
      this.#navigate(href);
      return this.#result(intent, "accepted", "Opening the Co-work document.", this.#last?.revision);
    } catch (error) {
      return this.#errorResult(intent, error);
    }
  }

  async reconcile(invalidation: AppInvalidation): Promise<ReconcileResult> {
    if (
      invalidation.appId !== TASKS_APP_ID ||
      (invalidation.viewIds !== undefined && !invalidation.viewIds.includes(TASKS_VIEW_ID))
    ) {
      return { changed: false, revision: this.#last?.revision };
    }
    const before = this.#last?.revision;
    const snapshot = await this.loadView(TASKS_VIEW_ID, { reason: "reconcile" });
    return before === snapshot.revision
      ? { changed: false, revision: snapshot.revision }
      : { changed: true, revision: snapshot.revision, snapshot };
  }

  #errorResult(intent: DashboardIntent, error: unknown): IntentResult {
    const api = error as Partial<TasksApiError>;
    const status = api.status === 409
      ? "conflict"
      : api.status === 401 || api.status === 403 || api.status === 503
        ? "unavailable"
        : "rejected";
    if (status === "conflict") this.#last = undefined;
    return {
      ...this.#result(intent, status, error instanceof Error ? error.message : "Tasks is unavailable."),
      ...(api.fieldErrors === undefined ? {} : { fieldErrors: api.fieldErrors }),
      ...(api.currentTask === undefined ? {} : { value: { task: api.currentTask } }),
    };
  }

  #result(
    intent: DashboardIntent,
    status: IntentResult["status"],
    message: string,
    revision?: string | number,
  ): IntentResult {
    return {
      intent_id: intent.intent_id,
      ...(intent.client_mutation_id === undefined ? {} : { client_mutation_id: intent.client_mutation_id }),
      status,
      ...(revision === undefined ? {} : { revision }),
      message,
    };
  }
}
