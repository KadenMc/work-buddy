import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardAnnouncer } from "../../dashboard/accessibility/DashboardAnnouncer";
import { DashboardHelpProvider } from "../../dashboard/help";
import type { IntentResult, JsonValue, WidgetIntent, WidgetPresentationContext } from "../../dashboard/contributions/contracts";
import { InMemoryWidgetDraftRepository, WidgetDraftRuntimeProvider, WidgetDraftScopeProvider, type WidgetDraftIdentity } from "../../dashboard/drafts";
import { InteractionSurfaceProvider } from "../../dashboard/interactions";
import { WidgetDraftTestScope } from "../../test/DashboardTestRuntime";
import { JOB_AUTHORING_WIDGET, JOBS_INSTANCE_ID, JOBS_VIEW_ID } from "./contribution";
import { JOB_INTENTS, type JobAuthoringInput } from "./contracts";
import JobComposer from "./JobComposer";

const presentation: WidgetPresentationContext = {
  instanceId: JOBS_INSTANCE_ID, viewId: JOBS_VIEW_ID, width: 800, height: 760,
  sizeMode: "standard", interactionMode: "operate", editing: false,
  theme: { contractVersion: 1, preference: { scheme: "light", skinId: "wb.default" }, resolvedScheme: "light",
    skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
    accessibility: { forcedColors: false, reducedMotion: false, reducedTransparency: false } },
  getCanvasTheme: () => ({ surfaceCanvas: "", surfaceRaised: "", textPrimary: "", textSecondary: "", borderDefault: "", focusRing: "", dataSeries: [] }),
};
const input: JobAuthoringInput = {
  access: { mode: "read_write" }, timeZone: "America/New_York",
  skills: [{ name: "journal_state", description: "Read Journal state", parameters: {} }], workflows: [],
};
const legacyDraftIdentity: WidgetDraftIdentity = {
  profileId: "legacy-profile", workspaceId: "legacy-workspace",
  appId: JOB_AUTHORING_WIDGET.publisherAppId, viewId: JOBS_VIEW_ID,
  instanceId: JOBS_INSTANCE_ID, widgetTypeId: JOB_AUTHORING_WIDGET.typeId,
  draftName: "job-create", scopeKey: "view",
};
const renderForm = (emit: (intent: WidgetIntent) => Promise<IntentResult>, overrides: Partial<JobAuthoringInput> = {}, mode: WidgetPresentationContext["interactionMode"] = "operate", help = false) => {
  const widgetInput = { ...input, ...overrides };
  const context = { ...presentation, interactionMode: mode };
  return render(<DashboardHelpProvider enabled={help}><DashboardAnnouncer><WidgetDraftTestScope definition={JOB_AUTHORING_WIDGET} presentation={context} input={widgetInput}>
    <JobComposer input={widgetInput} emit={emit} presentation={context} />
  </WidgetDraftTestScope></DashboardAnnouncer></DashboardHelpProvider>);
};
const accepted = (intent: WidgetIntent): IntentResult => ({ intent_id: intent.intent_id, status: "accepted", value: intent.intent_type === JOB_INTENTS.describeSchedule ? { valid: true, description: "Every Monday at 9:00 AM", max_jitter_seconds: 300 } : undefined });

describe("JobComposer", () => {
  it("keeps idle instruction text out of the form while retaining scheduling consent", async () => {
    const user = userEvent.setup();
    renderForm(vi.fn(async (intent: WidgetIntent) => accepted(intent)));
    const schedule = await screen.findByRole("textbox", { name: "Schedule" });
    const jitter = screen.getByRole("spinbutton", { name: "Jitter (seconds)" });
    expect(screen.queryByText(/The assistant fills these fields/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Ask Assist to turn a plain-English schedule/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Optional delay; maximum/)).not.toBeInTheDocument();
    expect(schedule).not.toHaveAttribute("aria-describedby");
    expect(jitter).not.toHaveAttribute("aria-describedby");
    await user.hover(schedule);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Job type" }), "skill");
    expect(screen.getByRole("combobox", { name: "Job type" })).toHaveValue("skill");
    expect(screen.getByRole("combobox", { name: "Skill" })).toBeVisible();
    expect(screen.queryByText(/Choose a registered name; creation validates it/)).not.toBeInTheDocument();
    expect(screen.getByText("Creates an enabled, recurring job in America/New_York.")).toBeVisible();
  });

  it("reveals schedule guidance on hover through the shared help mode", async () => {
    const user = userEvent.setup();
    renderForm(vi.fn(async (intent: WidgetIntent) => accepted(intent)), {}, "operate", true);
    const schedule = await screen.findByRole("textbox", { name: "Schedule" });
    expect(schedule).toHaveAttribute("data-help-target", "true");
    expect(screen.queryByText(/Use a five-field schedule in/)).not.toBeInTheDocument();
    await user.hover(schedule);
    expect(await screen.findByRole("tooltip", {}, { timeout: 3000 })).toHaveTextContent("Use a five-field schedule in America/New_York, or ask the assistant to turn a plain-English schedule into these fields.");
    await user.click(schedule);
    await user.type(schedule, "0 9 * * 1");
    expect(schedule).toHaveValue("0 9 * * 1");
    expect(await screen.findByText("Every Monday at 9:00 AM · America/New_York")).toBeVisible();
  });

  it("supports keyboard focus help without hiding an active jitter warning", async () => {
    const user = userEvent.setup();
    renderForm(vi.fn(async (intent: WidgetIntent) => accepted(intent)), {}, "operate", true);
    const jitter = await screen.findByRole("spinbutton", { name: "Jitter (seconds)" });
    act(() => jitter.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Use up to 300 seconds for this schedule.");
    await user.keyboard("{Escape}");
    await user.clear(jitter);
    await user.type(jitter, "10");
    expect(screen.getByText("Below 30 seconds may be too small to affect the scheduler tick.")).toBeVisible();
    for (const id of (jitter.getAttribute("aria-describedby") ?? "").split(" ").filter(Boolean)) {
      expect(document.getElementById(id)).not.toBeNull();
    }
    await user.clear(jitter);
    await user.type(jitter, "45");
    expect(screen.queryByText("Below 30 seconds may be too small to affect the scheduler tick.")).not.toBeInTheDocument();
  });

  it("clears stale field errors as the host draft changes while retaining unchanged errors", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent) => accepted(intent));
    renderForm(emit);
    await user.click(await screen.findByRole("button", { name: "Create job" }));
    const name = screen.getByRole("textbox", { name: "Job name" });
    const schedule = screen.getByRole("textbox", { name: "Schedule" });
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(schedule).toHaveAttribute("aria-invalid", "true");
    await user.type(name, "fixed-name");
    expect(name).not.toHaveAttribute("aria-invalid", "true");
    expect(schedule).toHaveAttribute("aria-invalid", "true");
    await user.type(schedule, "0 9 * * 1");
    expect(schedule).not.toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("textbox", { name: "What should the job do?" })).toHaveAttribute("aria-invalid", "true");
    expect(emit.mock.calls.every(([intent]) => intent.intent_type !== JOB_INTENTS.create)).toBe(true);
  });

  it("submits only from the real form, preserves exact prompt text, and clears only after success", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent) => accepted(intent));
    renderForm(emit);
    await user.type(await screen.findByRole("textbox", { name: "Job name" }), "weekly-review");
    await user.type(screen.getByRole("textbox", { name: "Schedule" }), "0 9 * * 1");
    await user.type(screen.getByRole("textbox", { name: "What should the job do?" }), "Review the paper draft.\nKeep the focus on one claim.");
    expect(emit.mock.calls.filter(([intent]) => intent.intent_type === JOB_INTENTS.create)).toHaveLength(0);
    expect(screen.getByText(/enabled, recurring job in America\/New_York/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Job name" })).toHaveValue(""));
    expect(emit.mock.calls.filter(([intent]) => intent.intent_type === JOB_INTENTS.create)).toHaveLength(1);
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: JOB_INTENTS.create, client_mutation_id: expect.stringMatching(/^job-create:/), payload: {
      name: "weekly-review", schedule: "0 9 * * 1", job_type: "prompt", jitter_seconds: 0,
      prompt: "Review the paper draft.\nKeep the focus on one claim.",
    } }));
  });

  it("shows the shared cron preview with the configured timezone", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent) => accepted(intent));
    renderForm(emit);
    await user.type(await screen.findByRole("textbox", { name: "Schedule" }), "0 9 * * 1");
    expect(await screen.findByText("Every Monday at 9:00 AM · America/New_York")).toBeInTheDocument();
    expect(emit.mock.calls.every(([intent]) => intent.intent_type === JOB_INTENTS.describeSchedule)).toBe(true);
  });

  it("rejects non-object parameters and retains every authored field", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent) => accepted(intent));
    renderForm(emit);
    await user.type(await screen.findByRole("textbox", { name: "Job name" }), "read-journal");
    await user.type(screen.getByRole("textbox", { name: "Schedule" }), "0 9 * * 1");
    await user.selectOptions(screen.getByRole("combobox", { name: "Job type" }), "skill");
    await user.type(screen.getByRole("combobox", { name: "Skill" }), "journal_state");
    const params = screen.getByRole("textbox", { name: "Parameters (JSON)" });
    await user.clear(params);
    await user.type(params, "true");
    await user.click(screen.getByRole("button", { name: "Create job" }));
    expect(await screen.findByText("Parameters must be a valid JSON object.")).toBeInTheDocument();
    expect(params).toHaveAttribute("aria-invalid", "true");
    expect(params).toHaveValue("true");
    expect(screen.getByRole("textbox", { name: "Job name" })).toHaveValue("read-journal");
    expect(emit.mock.calls.filter(([intent]) => intent.intent_type === JOB_INTENTS.create)).toHaveLength(0);
  });

  it("submits a canonical direct skill payload", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent) => accepted(intent));
    renderForm(emit);
    await user.type(await screen.findByRole("textbox", { name: "Job name" }), "read-journal");
    await user.type(screen.getByRole("textbox", { name: "Schedule" }), "0 9 * * 1");
    await user.selectOptions(screen.getByRole("combobox", { name: "Job type" }), "skill");
    await user.type(screen.getByRole("combobox", { name: "Skill" }), "journal_state");
    await user.click(screen.getByRole("button", { name: "Create job" }));
    await waitFor(() => expect(emit.mock.calls.some(([intent]) => intent.intent_type === JOB_INTENTS.create)).toBe(true));
    const create = emit.mock.calls.find(([intent]) => intent.intent_type === JOB_INTENTS.create)?.[0];
    expect(create?.payload).toMatchObject({ job_type: "skill", skill: "journal_state", params: {} });
    expect(create?.payload).not.toHaveProperty("capability");
  });

  it("keeps the draft and server field errors when creation is refused", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent: WidgetIntent): Promise<IntentResult> => intent.intent_type === JOB_INTENTS.create
      ? { intent_id: intent.intent_id, status: "rejected", message: "That job already exists.", fieldErrors: { name: "Choose a different job name." } }
      : accepted(intent));
    renderForm(emit);
    await user.type(await screen.findByRole("textbox", { name: "Job name" }), "existing-job");
    await user.type(screen.getByRole("textbox", { name: "Schedule" }), "0 9 * * 1");
    await user.type(screen.getByRole("textbox", { name: "What should the job do?" }), "Keep this draft");
    await user.click(screen.getByRole("button", { name: "Create job" }));
    await screen.findByText("Choose a different job name.");
    expect(screen.getByRole("textbox", { name: "Job name" })).toHaveValue("existing-job");
    expect(screen.getByRole("textbox", { name: "Job name" })).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("textbox", { name: "What should the job do?" })).toHaveValue("Keep this draft");
  });

  it("restores a persisted capability draft as a canonical skill draft", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    await repository.save({
      ...legacyDraftIdentity,
      draftSchema: JOB_AUTHORING_WIDGET.drafts![0]!.schema,
      value: {
        name: "read-journal", schedule: "0 9 * * 1", job_type: "capability",
        capability: "journal_state", workflow: "", prompt: "", params: "{}", jitter_seconds: 0,
      } as unknown as JsonValue,
      retentionDays: 30,
    });
    const emit = vi.fn(async (intent: WidgetIntent) => accepted(intent));
    render(<DashboardHelpProvider enabled={false}><DashboardAnnouncer><InteractionSurfaceProvider>
      <WidgetDraftRuntimeProvider repository={repository} profileId={legacyDraftIdentity.profileId} workspaceId={legacyDraftIdentity.workspaceId}>
        <WidgetDraftScopeProvider definition={JOB_AUTHORING_WIDGET} viewId={JOBS_VIEW_ID} instanceId={JOBS_INSTANCE_ID} input={input}>
          <JobComposer input={input} emit={emit} presentation={presentation} />
        </WidgetDraftScopeProvider>
      </WidgetDraftRuntimeProvider>
    </InteractionSurfaceProvider></DashboardAnnouncer></DashboardHelpProvider>);

    expect(await screen.findByRole("combobox", { name: "Job type" })).toHaveValue("skill");
    expect(screen.getByRole("combobox", { name: "Skill" })).toHaveValue("journal_state");
    await waitFor(async () => {
      const stored = (await repository.load(legacyDraftIdentity))?.value as unknown as Record<string, unknown>;
      expect(stored).toMatchObject({ job_type: "skill", skill: "journal_state" });
      expect(stored).not.toHaveProperty("capability");
    });
  });

  it("cannot edit, submit, or start assistance in Arrange mode", async () => {
    const emit = vi.fn();
    renderForm(emit, {}, "arrange");
    expect(await screen.findByRole("textbox", { name: "Job name" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create job" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "AI help" })).toBeDisabled();
    expect(emit).not.toHaveBeenCalled();
  });

  it("honors server read-only state without losing manual form visibility", async () => {
    const emit = vi.fn();
    renderForm(emit, { access: { mode: "read_only", reason: "Open from tray" } });
    expect(await screen.findByRole("textbox", { name: "Job name" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create job" })).toBeDisabled();
    expect(emit).not.toHaveBeenCalled();
  });
});
