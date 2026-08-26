import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DashboardAnnouncer } from "../../dashboard/accessibility/DashboardAnnouncer";
import type { IntentResult, WidgetIntent, WidgetPresentationContext } from "../../dashboard/contributions/contracts";
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
  capabilities: [{ name: "journal_state", description: "Read Journal state", parameters: {} }], workflows: [],
};
const renderForm = (emit: (intent: WidgetIntent) => Promise<IntentResult>, overrides: Partial<JobAuthoringInput> = {}, mode: WidgetPresentationContext["interactionMode"] = "operate") => {
  const widgetInput = { ...input, ...overrides };
  const context = { ...presentation, interactionMode: mode };
  return render(<DashboardAnnouncer><WidgetDraftTestScope definition={JOB_AUTHORING_WIDGET} presentation={context} input={widgetInput}>
    <JobComposer input={widgetInput} emit={emit} presentation={context} />
  </WidgetDraftTestScope></DashboardAnnouncer>);
};
const accepted = (intent: WidgetIntent): IntentResult => ({ intent_id: intent.intent_id, status: "accepted", value: intent.intent_type === JOB_INTENTS.describeSchedule ? { valid: true, description: "Every Monday at 9:00 AM", max_jitter_seconds: 300 } : undefined });

describe("JobComposer", () => {
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
    await user.selectOptions(screen.getByRole("combobox", { name: "Job type" }), "capability");
    await user.type(screen.getByRole("combobox", { name: "Capability" }), "journal_state");
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

  it("cannot edit, submit, or start assistance in Arrange mode", async () => {
    const emit = vi.fn();
    renderForm(emit, {}, "arrange");
    expect(await screen.findByRole("textbox", { name: "Job name" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create job" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Help me shape this" })).toBeDisabled();
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
