import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createHash } from "node:crypto";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type IntentResult,
  type WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";
import { InMemoryWidgetDraftRepository, WidgetDraftRuntimeProvider, WidgetDraftScopeProvider } from "../../dashboard/drafts";
import { DashboardHelpProvider } from "../../dashboard/help";
import { InteractionSurfaceProvider } from "../../dashboard/interactions";
import { expectNoAccessibilityViolations } from "../../test/setup";
import { WidgetDraftTestScope } from "../../test/DashboardTestRuntime";
import { fallbackCanvasTheme } from "../../theme/resolveTheme";
import { CAPTURE_APP_CONTRIBUTION } from "./contribution";
import type { QuickTextCaptureInput } from "./contracts";
import QuickTextCaptureWidget from "./QuickTextCaptureWidget";

const presentation: WidgetPresentationContext = {
  instanceId: asWidgetInstanceId("instance-capture-test"),
  viewId: asViewId("example.host.main"),
  width: 480,
  height: 320,
  sizeMode: "standard",
  interactionMode: "operate",
  editing: false,
  theme: {
    contractVersion: 1,
    preference: { scheme: "light", skinId: "wb.default" },
    resolvedScheme: "light",
    skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
    accessibility: {
      forcedColors: false,
      reducedMotion: false,
      reducedTransparency: false,
    },
  },
  getCanvasTheme: () => fallbackCanvasTheme("light"),
};

const baseInput: QuickTextCaptureInput = {
  instanceId: "instance-capture-test",
  revision: "r1",
  dayId: "day-1",
  access: { mode: "read_write" },
  targets: [
    {
      targetId: "log",
      label: "Log",
      description: "Append exact text to the daily log.",
      supportedModes: ["dumb", "smart"],
      defaultMode: "dumb",
      enabled: true,
    },
  ],
  capturesToday: 2,
  recentSubmissions: [],
};

const autoInput: QuickTextCaptureInput = {
  ...baseInput,
  targets: [
    {
      targetId: "auto",
      label: "Auto",
      description: "Let Smart infer whether this belongs in Log or Running notes.",
      supportedModes: ["smart"],
      defaultMode: "smart",
      enabled: true,
    },
    ...baseInput.targets,
    {
      targetId: "running_notes",
      label: "Running notes",
      description: "Capture an open thought as a stable Markdown item.",
      supportedModes: ["dumb", "smart"],
      defaultMode: "smart",
      enabled: true,
    },
  ],
};

const renderCapture = (
  input: QuickTextCaptureInput,
  emit: ReturnType<typeof vi.fn>,
  hostPresentation: WidgetPresentationContext = presentation,
) => (
  <WidgetDraftTestScope
    definition={CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0]}
    presentation={hostPresentation}
    input={input}
  >
    <QuickTextCaptureWidget
      input={input}
      emit={emit as ComponentProps<typeof QuickTextCaptureWidget>["emit"]}
      presentation={hostPresentation}
    />
  </WidgetDraftTestScope>
);

const proposalInput: QuickTextCaptureInput = { ...autoInput,
  secondaryActions: [{ actionId: "task_proposal", label: "Save and propose task",
    description: "No model runs and no task is created.", targetId: "running_notes", mode: "dumb" }],
};

const smartInput = (model = "reviewed-model", provider = "reviewed-provider"): QuickTextCaptureInput => ({
  ...baseInput,
  targets: baseInput.targets.map((target) => ({ ...target, defaultMode: "smart" })),
  smartAvailability: { state: "ready", code: "ready", reason: "Smart is ready.",
    disclosure: { provider, model, maxInputBytes: 32768, tools: false, web: false } },
});

const displayedHash = (input: QuickTextCaptureInput) => {
  const disclosure = input.smartAvailability!.disclosure;
  return createHash("sha256").update(JSON.stringify({ maxInputBytes: disclosure.maxInputBytes,
    model: disclosure.model, provider: disclosure.provider, tools: disclosure.tools, web: disclosure.web })).digest("hex");
};

function deferred<Value>() {
  let resolve!: (value: Value) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<Value>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const renderStoredCapture = (input: QuickTextCaptureInput, emit: ReturnType<typeof vi.fn>, repository: InMemoryWidgetDraftRepository) => (
  <InteractionSurfaceProvider>
    <WidgetDraftRuntimeProvider repository={repository} profileId="test-profile" workspaceId="test-workspace">
      <WidgetDraftScopeProvider definition={CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0]}
        viewId={presentation.viewId} instanceId={presentation.instanceId} input={input}>
        <QuickTextCaptureWidget input={input} emit={emit as ComponentProps<typeof QuickTextCaptureWidget>["emit"]} presentation={presentation} />
      </WidgetDraftScopeProvider>
    </WidgetDraftRuntimeProvider>
  </InteractionSurfaceProvider>
);

describe("QuickTextCaptureWidget", () => {
  it.each(["Capture", "Save and propose task"])("locks both save paths immediately while %s flushes and submits", async (action) => {
    const repository = new InMemoryWidgetDraftRepository();
    const flush = deferred<void>();
    const response = deferred<IntentResult>();
    const originalSave = repository.save.bind(repository);
    let delayWrites = false;
    vi.spyOn(repository, "save").mockImplementation(async (request) => {
      if (delayWrites) await flush.promise;
      return originalSave(request);
    });
    const emit = vi.fn(() => response.promise);
    render(renderStoredCapture(proposalInput, emit, repository));
    const textarea = await screen.findByRole("textbox", { name: "Capture text" });
    await userEvent.type(textarea, "  one exact source  ");
    delayWrites = true;
    const form = textarea.closest("form")!;
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: action }));
      fireEvent.click(screen.getByRole("button", { name: action === "Capture" ? "Save and propose task" : "Capture" }));
      fireEvent.submit(form);
    });
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save and propose task" })).toBeDisabled();
    expect(screen.getByText("Saving your exact capture…")).toBeVisible();
    expect(form).toHaveAttribute("aria-busy", "true");
    expect(emit).not.toHaveBeenCalled();
    await act(async () => { flush.resolve(); });
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    fireEvent.submit(form);
    await userEvent.click(screen.getByRole("button", { name: "Save and propose task" }));
    expect(emit).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    await act(async () => { response.resolve({ intent_id: "saved", status: "accepted" }); });
    await waitFor(() => expect(textarea).toHaveValue(""));
    expect(form).toHaveAttribute("aria-busy", "false");
    expect(screen.queryByText("Saving your exact capture…")).not.toBeInTheDocument();
  });

  it.each(["promise rejection", "unavailable result"])("retains exact text and the durable retry ID after %s", async (failure) => {
    const repository = new InMemoryWidgetDraftRepository();
    const response = deferred<IntentResult>();
    const emit = vi.fn().mockImplementationOnce(() => response.promise)
      .mockImplementation(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" }));
    const first = render(renderStoredCapture(proposalInput, emit, repository));
    const exactText = "  Keep my source\nexactly as entered.  ";
    await userEvent.type(await screen.findByRole("textbox", { name: "Capture text" }), exactText);
    await userEvent.click(screen.getByRole("button", { name: "Save and propose task" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    const initial = emit.mock.calls[0]![0];
    await act(async () => {
      if (failure === "promise rejection") response.reject(new Error("connection closed after commit"));
      else response.resolve({ intent_id: initial.intent_id, status: "unavailable", message: "Could not confirm the response." });
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save and propose task" })).toBeEnabled());
    expect(screen.getByRole("textbox", { name: "Capture text" })).toHaveValue(exactText);
    expect(screen.getByText(/retrying it unchanged checks the same/)).toBeVisible();
    first.unmount();

    render(renderStoredCapture(proposalInput, emit, repository));
    expect(await screen.findByRole("textbox", { name: "Capture text" })).toHaveValue(exactText);
    await userEvent.click(screen.getByRole("button", { name: "Save and propose task" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]![0]).toMatchObject({ client_mutation_id: initial.client_mutation_id, payload: initial.payload });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Capture text" })).toHaveValue(""));
  });

  it("keeps text edited during a pending save and uses a new ID for the changed request", async () => {
    const response = deferred<IntentResult>();
    const emit = vi.fn().mockImplementationOnce(() => response.promise)
      .mockImplementation(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" }));
    render(renderCapture(baseInput, emit));
    const textarea = await screen.findByRole("textbox", { name: "Capture text" });
    await userEvent.type(textarea, "first capture");
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    const firstId = emit.mock.calls[0]![0].client_mutation_id;
    await userEvent.type(textarea, " plus another thought");
    await act(async () => { response.resolve({ intent_id: "first", status: "accepted" }); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    expect(textarea).toHaveValue("first capture plus another thought");
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]![0].client_mutation_id).not.toBe(firstId);
    expect(emit.mock.calls[1]![0].payload.exact_text).toBe("first capture plus another thought");
  });

  it("pauses an unconfirmed request on action drift until the user explicitly edits the draft", async () => {
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "unavailable" }));
    render(renderCapture(proposalInput, emit));
    await userEvent.type(await screen.findByRole("textbox", { name: "Capture text" }), "same text, different action");
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save and propose task" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Save and propose task" }));
    expect(await screen.findByText(/unconfirmed capture's destination, action, or Smart setup changed/)).toBeVisible();
    expect(emit).toHaveBeenCalledTimes(1);
    await userEvent.type(screen.getByRole("textbox", { name: "Capture text" }), " (new capture)");
    await userEvent.click(screen.getByRole("button", { name: "Save and propose task" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]![0].client_mutation_id).not.toBe(emit.mock.calls[0]![0].client_mutation_id);
    expect(emit.mock.calls[1]![0].payload.follow_up_action).toBe("task_proposal");
  });

  it("freezes click-time disclosure through a delayed flush and pauses a restored uncertain retry on model drift", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const flush = deferred<void>();
    const originalSave = repository.save.bind(repository);
    let delayWrites = false;
    let waitingForFlush = false;
    vi.spyOn(repository, "save").mockImplementation(async (request) => {
      if (delayWrites) { waitingForFlush = true; await flush.promise; }
      return originalSave(request);
    });
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "unavailable" }));
    const reviewed = smartInput();
    const changed = smartInput("new-model", "new-provider");
    const first = render(renderStoredCapture(reviewed, emit, repository));
    await userEvent.type(await screen.findByRole("textbox", { name: "Capture text" }), "  preserve this pending source  ");
    delayWrites = true;
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(waitingForFlush).toBe(true));
    first.rerender(renderStoredCapture(changed, emit, repository));
    await act(async () => { flush.resolve(); });
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]![0].payload.smart_disclosure_sha256).toBe(displayedHash(reviewed));
    expect(emit.mock.calls[0]![0].payload.smart_disclosure_sha256).not.toBe(displayedHash(changed));
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    const initial = emit.mock.calls[0]![0];
    first.unmount();

    const restored = render(renderStoredCapture(changed, emit, repository));
    expect(await screen.findByRole("textbox", { name: "Capture text" })).toHaveValue("  preserve this pending source  ");
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    expect(await screen.findByText(/unconfirmed capture's destination, action, or Smart setup changed/)).toBeVisible();
    expect(emit).toHaveBeenCalledTimes(1);
    restored.rerender(renderStoredCapture(reviewed, emit, repository));
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]![0]).toMatchObject({ client_mutation_id: initial.client_mutation_id, payload: initial.payload });
  });

  it("does not automatically replace a pending Auto request when Smart becomes unavailable", async () => {
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "unavailable" }));
    const ready: QuickTextCaptureInput = { ...autoInput, smartAvailability: smartInput().smartAvailability };
    const rendered = render(renderCapture(ready, emit));
    await userEvent.type(await screen.findByRole("textbox", { name: "Capture text" }), "keep the Auto request");
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    const firstId = emit.mock.calls[0]![0].client_mutation_id;
    const unavailable: QuickTextCaptureInput = { ...ready, targets: ready.targets.map((target) => ({ ...target,
      enabled: target.targetId !== "auto", supportedModes: target.targetId === "auto" ? ["smart"] : ["dumb"],
    })) };
    rendered.rerender(renderCapture(unavailable, emit));
    expect(screen.getByRole("button", { name: /Destination/i })).toHaveTextContent("Auto");
    expect(screen.getByRole("switch", { name: "Smart" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
    rendered.rerender(renderCapture(ready, emit));
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]![0].client_mutation_id).toBe(firstId);
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    rendered.rerender(renderCapture(unavailable, emit));
    await userEvent.click(screen.getByRole("switch", { name: "Smart" }));
    // The explicit mode choice releases the pending guard immediately, without
    // waiting for a provider poll or another destination-menu interaction.
    expect(screen.getByRole("button", { name: /Destination/i })).toHaveTextContent("Log");
    expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(3));
    expect(emit.mock.calls[2]![0].payload).toMatchObject({ mode: "dumb", target_id: "log" });
    expect(emit.mock.calls[2]![0].client_mutation_id).not.toBe(firstId);
  });

  it("freezes a follow-up retry's displayed disclosure before the widget refreshes", async () => {
    const emit = vi.fn();
    const reviewed: QuickTextCaptureInput = { ...smartInput(), recentSubmissions: [{
      captureId: "a".repeat(32), clientMutationId: "saved", revision: 4, targetId: "log", mode: "smart",
      submittedAt: "2026-08-25T12:00:00Z", persistenceStatus: "persisted", processingStatus: "failed", retryable: true,
    }] };
    const changed: QuickTextCaptureInput = { ...reviewed, smartAvailability: smartInput("new-model", "new-provider").smartAvailability };
    const rendered = render(renderCapture(reviewed, emit));
    await screen.findByRole("button", { name: "Retry follow-up" });
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Retry follow-up" }));
      rendered.rerender(renderCapture(changed, emit));
    });
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]![0].payload).toEqual({ capture_id: "a".repeat(32), expected_revision: 4,
      smart_disclosure_sha256: displayedHash(reviewed) });
  });

  it("keeps the selected Smart switch visible after availability loss without replacing the uncertain save", async () => {
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "unavailable" }));
    const unavailable: QuickTextCaptureInput = { ...baseInput,
      targets: baseInput.targets.map((target) => ({ ...target, supportedModes: ["dumb"] })),
    };
    const rendered = render(renderCapture(baseInput, emit));
    await userEvent.type(await screen.findByRole("textbox", { name: "Capture text" }), "keep the uncertain Smart save");
    await userEvent.click(screen.getByRole("switch", { name: "Smart" }));
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    const firstId = emit.mock.calls[0]![0].client_mutation_id;

    rendered.rerender(renderCapture(unavailable, emit));
    expect(screen.getByRole("switch", { name: "Smart" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Smart" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Capture text" })).toHaveValue("keep the uncertain Smart save");
    expect(emit).toHaveBeenCalledTimes(1);

    rendered.rerender(renderCapture(baseInput, emit));
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]![0].client_mutation_id).toBe(firstId);
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    rendered.rerender(renderCapture(unavailable, emit));
    await userEvent.click(screen.getByRole("switch", { name: "Smart" }));
    expect(screen.queryByRole("switch", { name: "Smart" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(3));
    expect(emit.mock.calls[2]![0].payload.mode).toBe("dumb");
    expect(emit.mock.calls[2]![0].client_mutation_id).not.toBe(firstId);
  });

  it("lets a restored Smart draft explicitly switch to direct capture when Smart is unavailable", async () => {
    const repository = new InMemoryWidgetDraftRepository();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "unavailable" }));
    const first = render(renderStoredCapture(baseInput, emit, repository));
    await userEvent.type(await screen.findByRole("textbox", { name: "Capture text" }), "  restore these exact words  ");
    await userEvent.click(screen.getByRole("switch", { name: "Smart" }));
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled());
    const firstId = emit.mock.calls[0]![0].client_mutation_id;
    first.unmount();

    const unavailable: QuickTextCaptureInput = { ...baseInput,
      targets: baseInput.targets.map((target) => ({ ...target, supportedModes: ["dumb"] })),
    };
    render(renderStoredCapture(unavailable, emit, repository));
    expect(await screen.findByRole("textbox", { name: "Capture text" })).toHaveValue("  restore these exact words  ");
    expect(screen.getByRole("switch", { name: "Smart" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
    expect(emit).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("switch", { name: "Smart" }));
    expect(screen.getByRole("button", { name: "Capture" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(2));
    expect(emit.mock.calls[1]![0].payload).toMatchObject({ mode: "dumb", exact_text: "  restore these exact words  " });
    expect(emit.mock.calls[1]![0].client_mutation_id).not.toBe(firstId);
  });

  it("shows opt-in setup even when Auto cannot be selected", async () => {
    const input: QuickTextCaptureInput = { ...baseInput,
      targets: baseInput.targets.map((target) => ({ ...target, supportedModes: ["dumb"] })),
      smartAvailability: { state: "disabled_by_policy", code: "smart_not_enabled", reason: "Smart is off. Enable it in Journal settings.",
        disclosure: { provider: null, model: null, maxInputBytes: 32768, tools: false, web: false },
        action: { kind: "app_link", label: "Set up Smart", href: "/app/settings/apps/journal?setting=wb.journal.smart-processing" } },
    };
    render(renderCapture(input, vi.fn()));
    expect(await screen.findByRole("link", { name: "Set up Smart" })).toHaveAttribute("href", "/app/settings/apps/journal?setting=wb.journal.smart-processing");
    expect(screen.getByText(/Smart is off/)).toBeVisible();
    expect(screen.getByText(/32 KiB of exact saved text/)).toBeVisible();
    expect(screen.queryByRole("switch", { name: "Smart" })).not.toBeInTheDocument();
  });

  it("offers a visible provider retry without sending capture text", async () => {
    const emit = vi.fn();
    render(renderCapture({ ...baseInput,
      smartAvailability: { state: "provider_unavailable", code: "provider_not_preflightable", reason: "Provider unavailable; direct capture still works.",
        disclosure: { provider: "test", model: "test-model", maxInputBytes: 32768, tools: false, web: false },
        action: { kind: "retry", label: "Retry Smart setup" } },
    }, emit));
    await userEvent.click(await screen.findByRole("button", { name: "Retry Smart setup" }));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: "wb.capture.availability-refresh", payload: {} }));
    expect(screen.getByText(/test · test-model/)).toBeVisible();
  });

  it("saves an explicit proposal without Smart and preserves the exact source", async () => {
    const emit = vi.fn();
    render(renderCapture({ ...autoInput,
      secondaryActions: [{ actionId: "task_proposal", label: "Save and propose task", description: "No model runs and no task is created.", targetId: "running_notes", mode: "dumb" }],
    }, emit));
    const text = "  keep exact\nsource  ";
    await userEvent.type(await screen.findByRole("textbox", { name: "Capture text" }), text);
    await userEvent.click(screen.getByRole("button", { name: "Save and propose task" }));
    await waitFor(() => expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: "wb.capture.submit", payload: {
      day_id: "day-1", target_id: "running_notes", mode: "dumb", exact_text: text, follow_up_action: "task_proposal",
    } })));
  });

  it("renders review links and retries failed follow-ups without calling Tasks code", async () => {
    const emit = vi.fn();
    render(renderCapture({ ...baseInput, recentSubmissions: [{
      captureId: "a".repeat(32), clientMutationId: "saved", revision: 4, targetId: "log", mode: "dumb", submittedAt: "2026-08-25T12:00:00Z",
      persistenceStatus: "persisted", processingStatus: "not_requested", retryable: true,
      followUps: [{ kind: "app_link", referenceId: "th-0123abcd", label: "Review in Tasks", description: "Task proposal ready — no task has been created.", href: "/app/tasks?proposal=th-0123abcd" }],
    }] }, emit));
    expect(await screen.findByRole("link", { name: "Review in Tasks" })).toHaveAttribute("href", "/app/tasks?proposal=th-0123abcd");
    await userEvent.click(screen.getByRole("button", { name: "Retry follow-up" }));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({ intent_type: "wb.capture.retry-requested", payload: { capture_id: "a".repeat(32), expected_revision: 4 } }));
  });
  it("defaults to Auto, keeps destination copy compact, and requires Smart for Auto", async () => {
    const emit = vi.fn();
    render(renderCapture(autoInput, emit));

    const destination = await screen.findByRole("button", { name: /Destination/i });
    const destinationField = destination.closest(".wb-select-field");
    expect(destination).toHaveTextContent("Auto");
    expect(destination).not.toHaveTextContent("Let Smart infer");
    expect(destinationField).toHaveClass("wb-select-field--label-hidden");
    expect(destinationField).not.toHaveClass("wb-select-field--compact");
    expect(screen.queryByText(/Let Smart infer whether/i)).not.toBeInTheDocument();

    await userEvent.click(destination);
    expect(await screen.findByText(/Let Smart infer whether/i)).toBeVisible();
    await userEvent.keyboard("{Escape}");

    const smart = screen.getByRole("switch", { name: "Smart" });
    const capture = screen.getByRole("button", { name: "Capture" });
    expect(
      smart.compareDocumentPosition(destination) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      destination.compareDocumentPosition(capture) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(smart).toBeChecked();
    await userEvent.click(smart);
    expect(smart).not.toBeChecked();
    await userEvent.type(
      screen.getByRole("textbox", { name: "Capture text" }),
      "Route this for me",
    );
    expect(capture).toBeDisabled();
    expect(screen.getByText("Turn on Smart to use Auto.")).toBeVisible();

    await userEvent.click(smart);
    expect(capture).toBeEnabled();
    await userEvent.click(capture);
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      payload: { target_id: "auto", mode: "smart", exact_text: "Route this for me" },
    });
  });

  it("emits exact text and host identity through the generic Capture intent", async () => {
    const emit = vi.fn();
    const { container } = render(
      renderCapture(baseInput, emit),
    );
    const textarea = await screen.findByRole("textbox", { name: "Capture text" });
    await userEvent.type(textarea, "  Meeting ran long  ");
    const smart = screen.getByRole("switch", { name: "Smart" });
    expect(smart).not.toBeChecked();
    expect(
      screen.queryByText("Run a smart follow-up after saving."),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Press Ctrl + Enter to capture")).not.toBeInTheDocument();
    await userEvent.click(smart);
    expect(smart).toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: "wb.capture.submit",
      schema_version: 1,
      view_id: presentation.viewId,
      instance_id: presentation.instanceId,
      payload: {
        day_id: "day-1",
        target_id: "log",
        mode: "smart",
        exact_text: "  Meeting ran long  ",
      },
    });
    expect(emit.mock.calls[0]?.[0].client_mutation_id).toBe(
      emit.mock.calls[0]?.[0].intent_id,
    );
    await expectNoAccessibilityViolations(container);
  });

  it("retains the exact draft after a provider-reported persistence failure", async () => {
    const emit = vi.fn();
    const { rerender } = render(
      renderCapture(baseInput, emit),
    );
    const textarea = await screen.findByRole("textbox", { name: "Capture text" });
    await userEvent.type(textarea, "keep me exactly");
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    const mutationId = emit.mock.calls[0]?.[0].client_mutation_id as string;

    rerender(
      renderCapture(
        {
          ...baseInput,
          revision: "r2",
          recentSubmissions: [
            {
              clientMutationId: mutationId,
              targetId: "log",
              mode: "dumb",
              exactText: "keep me exactly",
              submittedAt: "2026-07-11T12:18:00-04:00",
              persistenceStatus: "failed",
              processingStatus: "not_requested",
              errorMessage: "Destination unavailable",
            },
          ],
        },
        emit,
      ),
    );

    expect(screen.getByRole("textbox", { name: "Capture text" })).toHaveValue(
      "keep me exactly",
    );
    expect(screen.getByText("Destination unavailable")).toBeInTheDocument();
  });

  it("reveals Smart's full explanation only through Hover help", async () => {
    render(
      <DashboardHelpProvider enabled>
        {renderCapture(
          {
            ...baseInput,
            smartHelp: {
              summary: "Smart uses the configured Journal model.",
              details:
                "The exact saved capture is sent to anthropic · claude-haiku-test for one classification. This processor has no tools or web access.",
            },
          },
          vi.fn(),
        )}
      </DashboardHelpProvider>,
    );

    const smart = await screen.findByRole("switch", { name: "Smart" });
    expect(screen.queryByText("Smart uses the configured Journal model.")).not.toBeInTheDocument();
    await userEvent.hover(smart);
    expect(
      await screen.findByText("Smart uses the configured Journal model."),
    ).toBeVisible();
    expect(screen.getByText(/no tools or web access/i)).toBeVisible();
    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByText("Smart uses the configured Journal model.")).not.toBeInTheDocument(),
    );
  });

  it("keeps a read-only capture useful but non-mutating", async () => {
    render(
      renderCapture(
        {
          ...baseInput,
          access: { mode: "read_only", reason: "This day is archived." },
        },
        vi.fn(),
        { ...presentation, sizeMode: "compact" },
      ),
    );
    expect(await screen.findByText("This day is archived.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Capture text" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
  });

  it("defers an access notice to a containing view without enabling capture", async () => {
    render(
      renderCapture(
        {
          ...baseInput,
          access: { mode: "read_only", reason: "Editing is paused." },
          accessNotice: "view",
        },
        vi.fn(),
      ),
    );

    expect(screen.queryByText("Editing is paused.")).not.toBeInTheDocument();
    expect(await screen.findByRole("textbox", { name: "Capture text" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
  });
});
