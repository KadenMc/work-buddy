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
import type { MarkdownNoteItem, RunningNotesInput } from "./contracts";
import RunningNotesWidget from "./RunningNotesWidget";

const presentation: WidgetPresentationContext = {
  instanceId: asWidgetInstanceId("instance-notes-test"),
  viewId: asViewId("example.host.main"),
  width: 560,
  height: 600,
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

const item: MarkdownNoteItem = {
  itemId: "note-1",
  markdown: "Meeting ran long",
  createdAt: "2026-07-11T12:18:00-04:00",
  updatedAt: "2026-07-11T12:18:00-04:00",
  provenance: { source: "user", label: "you" },
  captureMode: "smart",
  processing: {
    state: "succeeded",
    annotation: { summary: "Schedule updated", effects: ["Protected past records"] },
  },
  resolutionState: "open",
  version: 3,
};

const input: RunningNotesInput = {
  instanceId: "instance-notes-test",
  revision: "r1",
  dayId: "day-1",
  access: { mode: "read_write" },
  displayMode: "chronological",
  items: [item],
};

describe("RunningNotesWidget", () => {
  it("emits an exact versioned Markdown edit through the generic Notes intent", async () => {
    const emit = vi.fn();
    const { container } = render(
      <RunningNotesWidget input={input} emit={emit} presentation={presentation} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const editor = screen.getByRole("textbox", { name: "Edit note" });
    await userEvent.clear(editor);
    await userEvent.type(editor, "  Revised **exactly**  ");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: "wb.notes.edit-requested",
        view_id: presentation.viewId,
        instance_id: presentation.instanceId,
        payload: {
          item_id: "note-1",
          expected_version: 3,
          markdown: "  Revised **exactly**  ",
        },
      }),
    );
    expect(screen.getByText("saving")).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("detects a snapshot version conflict while preserving the local draft", async () => {
    const { rerender } = render(
      <RunningNotesWidget input={input} emit={vi.fn()} presentation={presentation} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Edit note" }), " local");

    rerender(
      <RunningNotesWidget
        input={{ ...input, revision: "r2", items: [{ ...item, version: 4 }] }}
        emit={vi.fn()}
        presentation={presentation}
      />,
    );

    expect(screen.getByText(/changed while you were editing/i)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Edit note" })).toHaveValue(
      "Meeting ran long local",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("renders a heavy grouped collection without truncating records", () => {
    const items = Array.from({ length: 200 }, (_, index): MarkdownNoteItem => ({
      ...item,
      itemId: `note-${index}`,
      markdown: `Stress note ${index}`,
      groupId: index % 2 === 0 ? "Decisions" : "Questions",
    }));
    render(
      <RunningNotesWidget
        input={{ ...input, displayMode: "grouped", items }}
        emit={vi.fn()}
        presentation={{ ...presentation, sizeMode: "compact" }}
      />,
    );

    expect(screen.getByRole("heading", { name: "Decisions" })).toBeInTheDocument();
    expect(screen.getByText("Stress note 199")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(200);
  });

  it("keeps read-only notes legible while disabling edits", () => {
    render(
      <RunningNotesWidget
        input={{
          ...input,
          access: { mode: "read_only", reason: "Archive is read-only." },
        }}
        emit={vi.fn()}
        presentation={presentation}
      />,
    );
    expect(screen.getByText("Meeting ran long")).toBeInTheDocument();
    expect(screen.getByText("Archive is read-only.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
  });
});
