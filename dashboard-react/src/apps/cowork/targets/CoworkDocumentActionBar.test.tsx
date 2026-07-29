import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import type {
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
  CoworkCapturedActionSnapshot,
} from "./contracts";
import { CoworkDocumentActionBar } from "./CoworkDocumentActionBar";

const verifyCapability = {
  enabled: true,
  contractVersion: 1,
  canRun: true,
  canConfigure: true,
  canCothink: true,
  disabledReason: null,
} as const;

const state: CoworkActionSnapshotControllerState = {
  phase: "ready",
  selection: {
    kind: "text_range",
    label: "Risks",
    wordCount: 12,
    range: { from: 4, to: 30 },
  },
  currentSection: {
    kind: "text_range",
    label: "Risks",
    wordCount: 85,
    range: { from: 2, to: 140 },
  },
  workingTarget: {
    kind: "text_range",
    label: "Methods",
    wordCount: 42,
    range: { from: 150, to: 240 },
    resolution: "relative",
  },
  customRangeStart: null,
  customRange: null,
};

const capture: CoworkCapturedActionSnapshot = {
  schema: "wb.cowork.action-snapshot/v1",
  captureId: "capture-a",
  storeId: "store-a",
  documentId: "doc-a",
  capturedAt: "2026-07-28T12:00:00.000Z",
  editGeneration: 1,
  ydocGenerationSha256: "generation-a",
  snapshotBase64: "AA==",
  snapshotSha256: "snapshot",
  stateVectorBase64: "AQ==",
  stateVectorSha256: "state-vector",
  structuredHeadSha256: "head",
  projectionMarkdown: "# Methods",
  projectionSha256: "projection",
  target: {
    source: "working_target",
    label: "Methods",
    wordCount: 42,
    proseMirrorRange: { from: 150, to: 240 },
    selector: {
      kind: "text_quote",
      exact: "# Methods",
      prefix: "",
      suffix: "",
      start: 0,
      end: 9,
    },
    targetTextSha256: "target",
  },
};

const fakeController = (): {
  readonly controller: CoworkActionSnapshotController;
  readonly setWorkingTargetFromSelection: ReturnType<typeof vi.fn>;
  readonly clearWorkingTarget: ReturnType<typeof vi.fn>;
  readonly capture: ReturnType<typeof vi.fn>;
  readonly setCustomRangeStartHere: ReturnType<typeof vi.fn>;
  readonly setCustomRangeEndHere: ReturnType<typeof vi.fn>;
} => {
  const setWorkingTargetFromSelection = vi.fn();
  const clearWorkingTarget = vi.fn();
  const captureAction = vi.fn(async () => capture);
  const setCustomRangeStartHere = vi.fn();
  const setCustomRangeEndHere = vi.fn();
  return {
    controller: {
      getSnapshot: () => state,
      subscribe: () => () => undefined,
      setWorkingTargetFromSelection,
      clearWorkingTarget,
      setCustomRangeStartHere,
      setCustomRangeEndHere,
      clearCustomRange: vi.fn(),
      capture: captureAction,
    },
    setWorkingTargetFromSelection,
    clearWorkingTarget,
    capture: captureAction,
    setCustomRangeStartHere,
    setCustomRangeEndHere,
  };
};

describe("CoworkDocumentActionBar", () => {
  it("shows the reusable target and hands one frozen capture to Verify", async () => {
    const user = userEvent.setup();
    const fake = fakeController();
    const onRunVerify = vi.fn(async () => undefined);
    const { container } = render(
      <CoworkDocumentActionBar
        controller={fake.controller}
        onRunVerify={onRunVerify}
        verifyCapability={verifyCapability}
      />,
    );

    expect(screen.getByText("Methods · 42 words")).toBeInTheDocument();
    expect(
      container.querySelector(".wb-cowork-action-bar__working-context"),
    ).toContainElement(
      screen.getByText("Custom range", { selector: "summary" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Work on this" }),
    );
    expect(fake.setWorkingTargetFromSelection).toHaveBeenCalledOnce();

    await user.click(screen.getByRole("button", { name: "Run Verify" }));
    await waitFor(() =>
      expect(onRunVerify).toHaveBeenCalledWith(capture, {
        userGoal: "Check this target against the active verification criteria.",
        protectedIntent:
          "Preserve the author's intended meaning, voice, and constraints.",
      }),
    );
    expect(fake.capture).toHaveBeenCalledWith("working_target");
    expect(
      screen.getByRole("status"),
    ).toHaveTextContent("Co-work Verify started");
    await expectNoAccessibilityViolations(container);
  });

  it("does not start durable Verify work in a read-only session", () => {
    const fake = fakeController();
    render(
      <CoworkDocumentActionBar
        controller={fake.controller}
        readOnly
        onRunVerify={() => undefined}
        verifyCapability={verifyCapability}
      />,
    );
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeDisabled();
  });

  it("captures the current selection for one run without replacing Working on", async () => {
    const user = userEvent.setup();
    const fake = fakeController();
    const onRunVerify = vi.fn(async () => undefined);
    render(
      <CoworkDocumentActionBar
        controller={fake.controller}
        onRunVerify={onRunVerify}
        verifyCapability={verifyCapability}
      />,
    );

    await user.click(
      screen.getByRole("button", {
        name: "Working on Action target",
      }),
    );
    await user.click(
      screen.getByRole("option", {
        name: /Current selection · one run/u,
      }),
    );
    await user.click(screen.getByRole("button", { name: "Run Verify" }));
    await waitFor(() => expect(onRunVerify).toHaveBeenCalledOnce());
    expect(fake.capture).toHaveBeenCalledWith("current_selection");
    expect(fake.setWorkingTargetFromSelection).not.toHaveBeenCalled();
  });

  it("keeps explicit Co-think available when Verify itself is unavailable", async () => {
    const user = userEvent.setup();
    const fake = fakeController();
    const onInvitePerspective = vi.fn(async () => undefined);
    render(
      <CoworkDocumentActionBar
        controller={fake.controller}
        onInvitePerspective={onInvitePerspective}
        verifyCapability={verifyCapability}
      />,
    );

    expect(screen.getByRole("button", { name: "Run Verify" })).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Invite perspective" }),
    );
    await waitFor(() =>
      expect(onInvitePerspective).toHaveBeenCalledWith(capture),
    );
  });

  it("summarizes the next-run setup and blocks an empty Verify plan", () => {
    const fake = fakeController();
    render(
      <CoworkDocumentActionBar
        controller={fake.controller}
        onRunVerify={() => undefined}
        verifySetup={{ activeCount: 0, unavailableCount: 1 }}
        verifyCapability={verifyCapability}
        executionLabel="Codex · GPT-5.6"
      />,
    );

    expect(
      screen.getByText(
        "Co-work Verify · 0 active · 1 unavailable · Codex · GPT-5.6",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeDisabled();
  });

  it("lets the human bind the run to an explicit goal and protected intent", async () => {
    const user = userEvent.setup();
    const fake = fakeController();
    const onRunVerify = vi.fn(async () => undefined);
    render(
      <CoworkDocumentActionBar
        controller={fake.controller}
        onRunVerify={onRunVerify}
        verifyCapability={verifyCapability}
      />,
    );

    await user.click(
      screen.getByText("Goal and protected intent", { selector: "summary" }),
    );
    const goal = screen.getByRole("textbox", {
      name: "What should Verify accomplish?",
    });
    const intent = screen.getByRole("textbox", {
      name: "What must it preserve?",
    });
    await user.clear(goal);
    await user.type(goal, "Keep the PRD faithful to the requested workflow.");
    await user.clear(intent);
    await user.type(intent, "Do not change the product boundary.");
    await user.click(screen.getByRole("button", { name: "Run Verify" }));

    await waitFor(() =>
      expect(onRunVerify).toHaveBeenCalledWith(capture, {
        userGoal: "Keep the PRD faithful to the requested workflow.",
        protectedIntent: "Do not change the product boundary.",
      }),
    );
  });

  it("fails closed for unknown contracts and gates Verify separately from Co-think", () => {
    const fake = fakeController();
    const { rerender } = render(
      <CoworkDocumentActionBar
        controller={fake.controller}
        onRunVerify={() => undefined}
        onInvitePerspective={() => undefined}
        verifyCapability={{ ...verifyCapability, contractVersion: 2 }}
      />,
    );
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Invite perspective" }),
    ).toBeDisabled();

    rerender(
      <CoworkDocumentActionBar
        controller={fake.controller}
        onRunVerify={() => undefined}
        onInvitePerspective={() => undefined}
        verifyCapability={{
          ...verifyCapability,
          canRun: false,
          canCothink: true,
        }}
      />,
    );
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Invite perspective" }),
    ).toBeEnabled();
  });

  it("offers exact keyboard-accessible custom range boundaries", async () => {
    const user = userEvent.setup();
    const fake = fakeController();
    render(
      <CoworkDocumentActionBar
        controller={fake.controller}
        verifyCapability={verifyCapability}
      />,
    );
    await user.click(screen.getByText("Custom range", { selector: "summary" }));
    await user.click(
      screen.getByRole("button", { name: "↦ Set start here" }),
    );
    expect(fake.setCustomRangeStartHere).toHaveBeenCalledOnce();
    expect(
      screen.getByRole("button", { name: "↤ Set end here" }),
    ).toBeDisabled();
  });
});
