import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";
import type {
  CanvasThemeSnapshot,
  ResolvedThemeSummary,
} from "../../dashboard/contributions/themeContract";
import { JOURNAL_VIEW_DEFINITION_ID } from "./bindings";
import type { JournalGenericModuleInput } from "./contracts";
import JournalGenericModule from "./JournalGenericModule";

const theme = {
  contractVersion: 1,
  preference: { scheme: "dark", skinId: "wb.default" },
  resolvedScheme: "dark",
  skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
  accessibility: { forcedColors: false, reducedMotion: false, reducedTransparency: false },
} satisfies ResolvedThemeSummary;

const canvas = {
  surfaceCanvas: "#000", surfaceRaised: "#111", textPrimary: "#fff",
  textSecondary: "#ccc", borderDefault: "#333", focusRing: "#58a6ff",
  dataSeries: ["#58a6ff"],
} satisfies CanvasThemeSnapshot;

const presentation = {
  instanceId: asWidgetInstanceId("custom.check-in"),
  viewId: JOURNAL_VIEW_DEFINITION_ID,
  width: 500, height: 400, sizeMode: "standard", interactionMode: "operate",
  editing: false, theme, getCanvasTheme: () => canvas,
} satisfies WidgetPresentationContext;

const input = {
  instanceId: "custom.check-in",
  revision: "journal:one",
  dayId: "journal-day:2026-08-27:America/New_York:05:00",
  localDate: "2026-08-27",
  access: { mode: "read_write" },
  moduleTypeId: "field_group",
  moduleInstanceVersion: 3,
  moduleDefinitionVersion: 1,
  behaviorId: "human_value",
  behaviorVersion: 1,
  aiContribution: "forbidden",
  label: "Check-in",
  fields: [{
    compositionSlotId: "check-in:focus",
    fieldId: "field.focus", definitionVersion: 2, label: "Focus",
    description: "Current usable focus", valueKind: "scale",
    value: null, required: false, minimum: 1, maximum: 5, readOnly: false,
    functionId: "function.focus", functionVersion: 1,
  }],
} satisfies JournalGenericModuleInput;

describe("JournalGenericModule", () => {
  it("emits a typed CAS edit while preserving the exact control input", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({
      intent_id: intent.intent_id,
      client_mutation_id: intent.client_mutation_id,
      status: "accepted" as const,
    }));
    render(<JournalGenericModule input={input} emit={emit} presentation={presentation} />);

    expect(screen.getByText("Function: function.focus")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add value" }));
    await user.type(screen.getByRole("spinbutton", { name: "Focus" }), "4");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: "wb.journal.field-value.put",
      client_mutation_id: expect.stringMatching(/^journal-field:/u),
      instance_id: "custom.check-in",
      payload: {
        local_date: "2026-08-27",
        module_instance_id: "custom.check-in",
        module_instance_version: 3,
        composition_slot_id: "check-in:focus",
        field_id: "field.focus",
        field_definition_version: 2,
        expected_revision: 0,
        value: 4,
        exact_input: "4",
      },
    });
  });

  it("does not offer an edit action when the provider fences the module", () => {
    render(
      <JournalGenericModule
        input={{ ...input, access: { mode: "read_only", reason: "Recovery in progress." },
          fields: input.fields.map((field) => ({ ...field, readOnly: true })) }}
        emit={vi.fn()}
        presentation={presentation}
      />,
    );

    expect(screen.queryByRole("button", { name: "Add value" })).toBeNull();
    expect(screen.getByText("Recovery in progress.")).toBeInTheDocument();
  });

  it("keeps the seed separate and makes a failed generation retryable", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({
      intent_id: intent.intent_id,
      client_mutation_id: intent.client_mutation_id,
      status: "unavailable" as const,
      message: "The background agent could not start. Choose Generate again to retry.",
    }));
    const promptInput = {
      ...input,
      moduleTypeId: "prompt_result",
      behaviorId: "provenance_only",
      aiContribution: "allowed",
      fields: [{
        compositionSlotId: "context:topic",
        fieldId: "field.topic",
        definitionVersion: 1,
        promptId: "prompt.topic",
        promptVersion: 1,
        label: "Topic context",
        valueKind: "long_text" as const,
        value: null,
        required: false,
        readOnly: false,
      }],
      promptInteractions: [{
        interactionId: "jpi_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        moduleInstanceId: "custom.check-in",
        moduleInstanceVersion: 3,
        promptId: "prompt.topic",
        promptVersion: 1,
        promptWording: "Add useful topic context",
        inputText: "Keep the scope concise.",
        lifecycle: "current",
        currentRevision: 2,
        variants: [{
          variantId: "jpv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          resultText: "A generated context option.",
          authorship: "generated",
          reviewState: "unreviewed",
          lifecycle: "current",
          producerId: "journal-prompt:fixture",
          modelId: "fixture-model",
          createdAt: "2026-08-27T12:00:00Z",
        }],
        generationRequests: [{
          requestId: "jpgr_cccccccccccccccccccccccccccccccc",
          status: "failed" as const,
          retryable: true,
          attempts: 1,
          errorCode: "generation_worker_start_failed",
          createdAt: "2026-08-27T12:00:00Z",
          updatedAt: "2026-08-27T12:00:01Z",
          completedAt: "2026-08-27T12:00:01Z",
        }],
      }],
    } satisfies JournalGenericModuleInput;

    render(<JournalGenericModule input={promptInput} emit={emit} presentation={presentation} />);

    expect(screen.getByText("Original seed")).toBeInTheDocument();
    expect(screen.getByText("Keep the scope concise.")).toBeInTheDocument();
    expect(screen.getByText("Generated result")).toBeInTheDocument();
    expect(screen.getByText("A generated context option.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry generation" }));

    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));
    expect(emit.mock.calls[0]?.[0]).toMatchObject({
      intent_type: "wb.journal.prompt-generate",
      client_mutation_id: expect.stringMatching(/^journal-prompt-generate:/u),
      payload: {
        interaction_id: "jpi_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        expected_revision: 2,
      },
    });
    expect(await screen.findByText(/background agent could not start/u)).toBeInTheDocument();
  });

  it("does not expose generation when the module behavior forbids AI contribution", () => {
    const promptInput = {
      ...input,
      moduleTypeId: "prompt_result",
      fields: [{
        compositionSlotId: "context:topic",
        fieldId: "field.topic",
        definitionVersion: 1,
        promptId: "prompt.topic",
        promptVersion: 1,
        label: "Topic context",
        valueKind: "long_text" as const,
        value: null,
        required: false,
        readOnly: false,
      }],
      promptInteractions: [{
        interactionId: "jpi_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        moduleInstanceId: "custom.check-in",
        moduleInstanceVersion: 3,
        promptId: "prompt.topic",
        promptVersion: 1,
        promptWording: "Add useful topic context",
        inputText: "Keep the scope concise.",
        lifecycle: "current",
        currentRevision: 1,
        variants: [],
        generationRequests: [],
      }],
    } satisfies JournalGenericModuleInput;

    render(<JournalGenericModule input={promptInput} emit={vi.fn()} presentation={presentation} />);

    expect(screen.queryByRole("button", { name: "Generate result" })).toBeNull();
    expect(screen.getByText(/AI generation is disabled/u)).toBeInTheDocument();
  });

  it("refreshes an item editor from the latest server revision", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const }));
    const itemInput = {
      ...input,
      fields: [],
      items: [{
        itemId: "jni_topic", itemKind: "record", text: "First revision",
        createdAt: "2026-08-27T12:00:00Z", updatedAt: "2026-08-27T12:00:00Z",
        revision: 1, lifecycle: "current", authorityKind: "native_plain",
        actions: ["edit", "route"] as const, relations: [],
      }],
    } satisfies JournalGenericModuleInput;
    const view = render(<JournalGenericModule input={itemInput} emit={emit} presentation={presentation} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.clear(screen.getByRole("textbox", { name: "edit Journal item" }));
    await user.type(screen.getByRole("textbox", { name: "edit Journal item" }), "Local revision");
    await user.click(screen.getByRole("button", { name: "Save edit" }));
    await waitFor(() => expect(emit).toHaveBeenCalledTimes(1));

    view.rerender(<JournalGenericModule input={{
      ...itemInput,
      items: [{ ...itemInput.items[0], text: "Server revision", revision: 2 }],
    }} emit={emit} presentation={presentation} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("textbox", { name: "edit Journal item" })).toHaveValue("Server revision");
  });

  it("routes only to a backend-supported target domain", async () => {
    const user = userEvent.setup();
    const emit = vi.fn(async (intent) => ({ intent_id: intent.intent_id, status: "accepted" as const }));
    render(<JournalGenericModule input={{
      ...input,
      fields: [],
      items: [{
        itemId: "jni_route", itemKind: "record", text: "Route this context",
        createdAt: "2026-08-27T12:00:00Z", updatedAt: "2026-08-27T12:00:00Z",
        revision: 1, lifecycle: "current", authorityKind: "native_plain",
        actions: ["route"], relations: [],
      }],
    }} emit={emit} presentation={presentation} />);
    await user.click(screen.getByRole("button", { name: "Route" }));
    expect(screen.getByRole("combobox", { name: "Route domain" })).toHaveValue("task");
    expect(screen.queryByRole("option", { name: "Tasks" })).toBeNull();
    await user.type(screen.getByRole("textbox", { name: "Route target ID" }), "task_fixture");
    await user.click(screen.getByRole("button", { name: "Save route" }));
    expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: "wb.journal.item-action",
      payload: expect.objectContaining({ target_domain: "task", target_id: "task_fixture" }),
    }));
  });
});
