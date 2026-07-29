import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import type {
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
} from "./contracts";
import { CoworkDocumentActionBar } from "./CoworkDocumentActionBar";

const readyState = (
  overrides: Partial<CoworkActionSnapshotControllerState> = {},
): CoworkActionSnapshotControllerState => ({
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
  workingTargetStart: null,
  ...overrides,
});

const fakeController = (
  state = readyState(),
): {
  readonly controller: CoworkActionSnapshotController;
  readonly setWorkingTargetFromSelection: ReturnType<typeof vi.fn>;
  readonly setWorkingTargetStartHere: ReturnType<typeof vi.fn>;
  readonly setWorkingTargetEndHere: ReturnType<typeof vi.fn>;
  readonly clearWorkingTargetDraft: ReturnType<typeof vi.fn>;
  readonly clearWorkingTarget: ReturnType<typeof vi.fn>;
} => {
  const setWorkingTargetFromSelection = vi.fn();
  const setWorkingTargetStartHere = vi.fn();
  const setWorkingTargetEndHere = vi.fn();
  const clearWorkingTargetDraft = vi.fn();
  const clearWorkingTarget = vi.fn();
  return {
    controller: {
      getSnapshot: () => state,
      subscribe: () => () => undefined,
      setWorkingTargetFromSelection,
      setWorkingTargetStartHere,
      setWorkingTargetEndHere,
      clearWorkingTargetDraft,
      clearWorkingTarget,
      capture: vi.fn(async () => {
        throw new Error("Capture is not used by the Working on bar.");
      }),
    },
    setWorkingTargetFromSelection,
    setWorkingTargetStartHere,
    setWorkingTargetEndHere,
    clearWorkingTargetDraft,
    clearWorkingTarget,
  };
};

describe("CoworkDocumentActionBar", () => {
  it("keeps the editor-top surface limited to the shared Working on target", async () => {
    const user = userEvent.setup();
    const fake = fakeController();
    const { container } = render(
      <CoworkDocumentActionBar controller={fake.controller} />,
    );

    expect(screen.getByText("Methods · 42 words")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Set by selection" }),
    );
    expect(fake.setWorkingTargetFromSelection).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(fake.clearWorkingTarget).toHaveBeenCalledOnce();
    expect(
      screen.queryByRole("button", { name: "Run Verify" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Co-think")).not.toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("supports the accessible two-step cursor flow for the shared target", async () => {
    const user = userEvent.setup();
    const fake = fakeController(
      readyState({
        workingTargetStart: { position: 14, label: "Paragraph 2 start" },
      }),
    );
    render(<CoworkDocumentActionBar controller={fake.controller} />);

    await user.click(
      screen.getByText(/Set by cursor · Paragraph 2 start/u, {
        selector: "summary",
      }),
    );
    await user.click(screen.getByRole("button", { name: "↦ Set start" }));
    await user.click(screen.getByRole("button", { name: "↤ Set end" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(fake.setWorkingTargetStartHere).toHaveBeenCalledOnce();
    expect(fake.setWorkingTargetEndHere).toHaveBeenCalledOnce();
    expect(fake.clearWorkingTargetDraft).toHaveBeenCalledOnce();
  });

  it("fails gracefully while selection or cursor controls are unavailable", () => {
    const state = readyState({
      phase: "loading",
      selection: null,
    });
    const base = fakeController(state).controller;
    const controller: CoworkActionSnapshotController = {
      getSnapshot: base.getSnapshot,
      subscribe: base.subscribe,
      setWorkingTargetFromSelection: base.setWorkingTargetFromSelection,
      clearWorkingTarget: base.clearWorkingTarget,
      capture: base.capture,
    };
    render(<CoworkDocumentActionBar controller={controller} />);

    expect(
      screen.getByRole("button", { name: "Set by selection" }),
    ).toBeDisabled();
    expect(screen.queryByText("Set by cursor")).not.toBeInTheDocument();
  });
});
