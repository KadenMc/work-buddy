import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";
import { expectNoAccessibilityViolations } from "../../test/setup";
import { fallbackCanvasTheme } from "../../theme/resolveTheme";
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

describe("QuickTextCaptureWidget", () => {
  it("emits exact text and host identity through the generic Capture intent", async () => {
    const emit = vi.fn();
    const { container } = render(
      <QuickTextCaptureWidget
        input={baseInput}
        emit={emit}
        presentation={presentation}
      />,
    );
    const textarea = screen.getByRole("textbox", { name: "Capture text" });
    await userEvent.type(textarea, "  Meeting ran long  ");
    await userEvent.click(
      screen.getByRole("radio", { name: "Save + smart follow-up" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));

    expect(emit).toHaveBeenCalledTimes(1);
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
      <QuickTextCaptureWidget
        input={baseInput}
        emit={emit}
        presentation={presentation}
      />,
    );
    const textarea = screen.getByRole("textbox", { name: "Capture text" });
    await userEvent.type(textarea, "keep me exactly");
    await userEvent.click(screen.getByRole("button", { name: "Capture" }));
    const mutationId = emit.mock.calls[0]?.[0].client_mutation_id as string;

    rerender(
      <QuickTextCaptureWidget
        input={{
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
        }}
        emit={emit}
        presentation={presentation}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Capture text" })).toHaveValue(
      "keep me exactly",
    );
    expect(screen.getByText("Destination unavailable")).toBeInTheDocument();
  });

  it("keeps a read-only capture useful but non-mutating", () => {
    render(
      <QuickTextCaptureWidget
        input={{
          ...baseInput,
          access: { mode: "read_only", reason: "This day is archived." },
        }}
        emit={vi.fn()}
        presentation={{ ...presentation, sizeMode: "compact" }}
      />,
    );
    expect(screen.getByText("This day is archived.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Capture text" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Capture" })).toBeDisabled();
  });
});
