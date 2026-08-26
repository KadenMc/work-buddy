import type { AppInvalidation, DashboardIntent, IntentResult, JsonValue, ViewId, ViewLoadRequest, ViewSnapshot, WidgetLoadRequest, WidgetSnapshot, WidgetTypeId } from "../../dashboard/contributions/contracts";
import type { ViewProvider } from "../../dashboard/providers/ViewProvider";
import type { ViewLocationAdapter } from "../../dashboard/contributions/viewModules";
import { exactHumanAuthorityHeaders } from "../../security/humanAuthority";
import { JOBS_APP_ID, JOBS_INSTANCE_ID, JOBS_VIEW_ID, JOBS_WIDGET_ID } from "./contribution";
import { JOB_INTENTS, type JobAuthoringInput, type JobRegistryEntry } from "./contracts";

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const parameters = (value: unknown): JobRegistryEntry["parameters"] => {
  if (record(value)) return value as JobRegistryEntry["parameters"];
  if (!Array.isArray(value)) return {};
  return Object.fromEntries(value.filter(record).flatMap((item) => typeof item.name === "string" ? [[item.name, item]] : [])) as JobRegistryEntry["parameters"];
};
const entries = (value: unknown): readonly JobRegistryEntry[] => Array.isArray(value) ? value.filter(record).flatMap((item) => typeof item.name === "string" ? [{ name: item.name, description: typeof item.description === "string" ? item.description : "", parameters: parameters(item.parameters) }] : []) : [];
const json = async (response: Response): Promise<Record<string, unknown>> => {
  if (!response.headers.get("Content-Type")?.includes("application/json")) throw new Error("The Jobs API is unavailable. Restart the dashboard to use the current build.");
  const body: unknown = await response.json();
  if (!record(body)) throw new Error("The Jobs response is invalid.");
  return body;
};

export class HttpJobsProvider implements ViewProvider {
  readonly appId = JOBS_APP_ID;
  readonly #fetch: typeof fetch;
  #last?: ViewSnapshot;
  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis), readonly location?: ViewLocationAdapter) { this.#fetch = fetchImpl; }
  async loadView(viewId: ViewId, _request: ViewLoadRequest): Promise<ViewSnapshot> {
    if (viewId !== JOBS_VIEW_ID) throw new Error("The Jobs view is invalid.");
    const [viewResponse, registryResponse] = await Promise.all([
      this.#fetch("/api/jobs/authoring", { credentials: "same-origin", headers: { Accept: "application/json" } }),
      this.#fetch("/api/registry/list", { credentials: "same-origin", headers: { Accept: "application/json" } }),
    ]);
    const view = await json(viewResponse);
    if (!viewResponse.ok) throw new Error("Job authoring is unavailable.");
    const registry = registryResponse.ok ? await json(registryResponse) : {};
    const access = record(view.access) && view.access.mode === "read_write" ? { mode: "read_write" as const } : { mode: "read_only" as const, reason: record(view.access) && typeof view.access.reason === "string" ? view.access.reason : "Job editing is temporarily unavailable." };
    const input: JobAuthoringInput = { access, timeZone: typeof view.time_zone === "string" ? view.time_zone : "local time", capabilities: entries(registry.capabilities), workflows: entries(registry.workflows), openAssistance: new URLSearchParams(this.location?.getSearch() ?? "").get("assist") === "1" };
    this.#last = { viewId, observedAt: new Date().toISOString(), status: access.mode === "read_only" ? "read-only" : "ready", quality: { kind: "complete" }, model: input, bindings: {}, widgetInputs: { [JOBS_INSTANCE_ID]: input } };
    return this.#last;
  }
  async loadWidget(widgetTypeId: WidgetTypeId, request: WidgetLoadRequest): Promise<WidgetSnapshot> {
    const view = this.#last ?? await this.loadView(request.viewId, { reason: "refresh" });
    return { widgetTypeId, instanceId: request.instanceId, observedAt: view.observedAt, status: widgetTypeId === JOBS_WIDGET_ID && request.instanceId === JOBS_INSTANCE_ID ? "ready" : "unavailable", quality: view.quality, input: view.widgetInputs[request.instanceId] ?? null };
  }
  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    const result = (status: IntentResult["status"], message: string, extra: Partial<IntentResult> = {}): IntentResult => ({ intent_id: intent.intent_id, client_mutation_id: intent.client_mutation_id, status, message, ...extra });
    if (intent.view_id !== JOBS_VIEW_ID || !record(intent.payload)) return result("rejected", "Invalid Jobs action.");
    try {
      if (intent.intent_type === JOB_INTENTS.describeSchedule) {
        const response = await this.#fetch(`/api/cron/describe?expr=${encodeURIComponent(String(intent.payload.schedule ?? ""))}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
        const body = await json(response);
        return result(response.ok ? "accepted" : "unavailable", "Schedule preview updated.", { value: body as JsonValue });
      }
      if (intent.intent_type !== JOB_INTENTS.create) return result("rejected", "Unsupported Jobs action.");
      const body: Record<string, JsonValue> & { client_mutation_id: string } = { ...intent.payload, client_mutation_id: intent.client_mutation_id ?? intent.intent_id };
      const path = "/api/jobs/authoring";
      const headers = await exactHumanAuthorityHeaders({ action: "dashboard.jobs.create", subject: `job:new:${body.client_mutation_id}`, context: { method: "POST", path, body } }, this.#fetch);
      const response = await this.#fetch(path, { method: "POST", credentials: "same-origin", headers: { Accept: "application/json", "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
      const payload = await json(response);
      if (!response.ok || payload.success !== true) {
        const message = typeof payload.error === "string" ? payload.error : "The job was not created. Your draft is preserved.";
        return result(response.status === 401 || response.status === 403 ? "unavailable" : "rejected", message, { fieldErrors: record(payload.errors_by_field) ? Object.fromEntries(Object.entries(payload.errors_by_field).filter((entry): entry is [string, string] => typeof entry[1] === "string")) : {} });
      }
      return result("accepted", "Job created. The scheduler will pick it up automatically.", { value: { name: String(payload.name ?? body.name) } });
    } catch (error) { return result("unavailable", error instanceof Error ? error.message : "The request could not be confirmed. Your draft is preserved; check existing jobs before retrying."); }
  }
  async reconcile(invalidation: AppInvalidation) {
    if (invalidation.appId !== JOBS_APP_ID) return { changed: false };
    return { changed: true, snapshot: await this.loadView(JOBS_VIEW_ID, { reason: "reconcile" }) };
  }
}
