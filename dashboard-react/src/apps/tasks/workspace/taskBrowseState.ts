import type { JsonValue } from "../../../dashboard/contributions/contracts";
import type { TaskQueryState, TaskSort } from "../contracts";

export interface TaskBrowseState {
  readonly statuses: readonly string[];
  readonly projects: readonly string[];
  readonly namespaces: readonly string[];
  readonly exact_namespaces: readonly string[];
  readonly attention: readonly string[];
  readonly urgencies: readonly string[];
  readonly q: string;
  readonly due: string;
  readonly note: string;
  readonly sort: TaskSort;
  readonly direction: "asc" | "desc";
  readonly mode: "browse" | "triage";
  readonly offset: number;
  readonly limit: number;
}

export const DEFAULT_TASK_BROWSE_STATE: TaskBrowseState = {
  statuses: ["open"], projects: [], namespaces: [], exact_namespaces: [],
  attention: [], urgencies: [], q: "", due: "", note: "",
  sort: "created_at", direction: "desc", mode: "browse", offset: 0, limit: 50,
};

const list = (value: unknown, fallback: readonly string[] = []): readonly string[] =>
  Array.isArray(value) ? [...new Set(value.filter((item): item is string => typeof item === "string" && item.length > 0))] : fallback;

/** Only query state is retained; task/proposal identity and panel/grid layout are separate. */
export function taskBrowseState(value: unknown): TaskBrowseState {
  const item = value && typeof value === "object" ? value as Partial<TaskQueryState> : {};
  const sort: TaskSort = ["created_at", "updated_at", "title", "due_date", "urgency"].includes(item.sort ?? "") ? item.sort! : "created_at";
  return {
    statuses: list(item.statuses, ["open"]), projects: list(item.projects),
    namespaces: list(item.namespaces), exact_namespaces: list(item.exact_namespaces),
    attention: list(item.attention), urgencies: list(item.urgencies),
    q: typeof item.q === "string" ? item.q : "",
    due: typeof item.due === "string" ? item.due : "",
    note: typeof item.note === "string" ? item.note : "",
    sort,
    direction: item.direction === "asc" || item.direction === "desc" ? item.direction : sort === "title" || sort === "due_date" ? "asc" : "desc",
    mode: item.mode === "triage" ? "triage" : "browse",
    offset: typeof item.offset === "number" && Number.isSafeInteger(item.offset) && item.offset >= 0 ? item.offset : 0,
    limit: typeof item.limit === "number" && Number.isSafeInteger(item.limit) && item.limit > 0 ? item.limit : 50,
  };
}

export const taskBrowseKey = (value: unknown): string => JSON.stringify(taskBrowseState(value));

export const taskBrowsePatch = (value: unknown): Record<string, JsonValue> => ({
  ...taskBrowseState(value),
  // A canonical reset must also remove old bookmark aliases.
  lens: null, project: null, namespace: null, urgency: null, state: null,
});

/** An explicit query is complete, so shared URLs never inherit hidden personal filters. */
export const hasTaskBrowseQuery = (search: string): boolean => {
  const params = new URLSearchParams(search);
  return [
    "statuses", "projects", "namespaces", "exact_namespaces", "attention", "urgencies",
    "q", "due", "note", "sort", "direction", "offset", "limit",
    "lens", "project", "namespace", "urgency", "state",
  ].some((key) => params.has(key)) || params.get("mode") === "triage";
};
