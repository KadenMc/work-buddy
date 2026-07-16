import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";
import { DashboardHelpProvider } from "../../dashboard/help";
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

describe("QuickTextCaptureWidget", () => {
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
        {renderCapture(baseInput, vi.fn())}
      </DashboardHelpProvider>,
    );

    const smart = await screen.findByRole("switch", { name: "Smart" });
    expect(screen.queryByText("Run a smart follow-up after capturing.")).not.toBeInTheDocument();
    await userEvent.hover(smart);
    expect(
      await screen.findByText("Run a smart follow-up after capturing."),
    ).toBeVisible();
    expect(screen.getByText(/governed operations still follow/i)).toBeVisible();
    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByText("Run a smart follow-up after capturing.")).not.toBeInTheDocument(),
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
});
