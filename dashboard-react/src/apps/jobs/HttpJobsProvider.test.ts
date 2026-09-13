import { describe, expect, it, vi } from "vitest";
vi.mock("../../security/humanAuthority", () => ({ exactHumanAuthorityHeaders: vi.fn(async () => ({ "X-WB-Test": "yes" })) }));
import { HttpJobsProvider } from "./HttpJobsProvider";
import { JOBS_VIEW_ID } from "./contribution";
import { JOB_INTENTS } from "./contracts";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
describe("Jobs authoring provider", () => {
  it("projects the canonical skill registry into the Jobs view", async () => {
    const fetchImpl = vi.fn(async (url: RequestInfo | URL) => String(url) === "/api/registry/list"
      ? json({ skills: [{ name: "journal_state", description: "Read Journal state", parameters: { date: { type: "str" } } }], workflows: [] })
      : json({ access: { mode: "read_write" }, time_zone: "America/New_York" }));
    const provider = new HttpJobsProvider(fetchImpl as typeof fetch);
    const snapshot = await provider.loadView(JOBS_VIEW_ID, { reason: "mount" });
    expect(snapshot.model).toMatchObject({
      skills: [{ name: "journal_state", description: "Read Journal state", parameters: { date: { type: "str" } } }],
      workflows: [],
    });
    expect(snapshot.model).not.toHaveProperty("capabilities");
  });

  it("keeps domain submission on the human-authorized existing job service adapter", async () => {
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => json({ success: true, name: "weekly-review" }));
    const provider = new HttpJobsProvider(fetchImpl as typeof fetch);
    const result = await provider.dispatch({ intent_type: JOB_INTENTS.create, schema_version: 1, intent_id: "job-1", client_mutation_id: "job-1", view_id: JOBS_VIEW_ID, payload: { name: "weekly-review", schedule: "0 9 * * 1", job_type: "prompt", prompt: "Review work" } });
    expect(result.status).toBe("accepted");
    expect(fetchImpl).toHaveBeenCalledWith("/api/jobs/authoring", expect.objectContaining({ method: "POST", headers: expect.objectContaining({ "X-WB-Test": "yes" }) }));
    expect(JSON.parse(String(fetchImpl.mock.calls[0]?.[1]?.body))).not.toHaveProperty("overwrite");
  });
  it("preserves field-specific validation feedback", async () => {
    const provider = new HttpJobsProvider(vi.fn(async () => json({ success: false, error: "Unknown workflow", errors_by_field: { workflow: "Choose a registered workflow." } }, 400)) as typeof fetch);
    const result = await provider.dispatch({ intent_type: JOB_INTENTS.create, schema_version: 1, intent_id: "job-1", view_id: JOBS_VIEW_ID, payload: {} });
    expect(result).toMatchObject({ status: "rejected", fieldErrors: { workflow: "Choose a registered workflow." } });
  });

  it("sends direct skill creation with no retired capability field", async () => {
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => json({ success: true, name: "read-journal" }));
    const provider = new HttpJobsProvider(fetchImpl as typeof fetch);
    await provider.dispatch({
      intent_type: JOB_INTENTS.create, schema_version: 1, intent_id: "job-2", client_mutation_id: "job-2", view_id: JOBS_VIEW_ID,
      payload: { name: "read-journal", schedule: "0 9 * * 1", job_type: "skill", skill: "journal_state", params: {} },
    });
    const body = JSON.parse(String(fetchImpl.mock.calls[0]?.[1]?.body));
    expect(body).toMatchObject({ job_type: "skill", skill: "journal_state" });
    expect(body).not.toHaveProperty("capability");
  });
});
