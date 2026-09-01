import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";
import { expectNoAccessibilityViolations } from "../../test/setup";
import { WidgetDraftTestScope } from "../../test/DashboardTestRuntime";
import { fallbackCanvasTheme } from "../../theme/resolveTheme";
import { NOTES_APP_CONTRIBUTION } from "./contribution";
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

const renderNotes = (
  widgetInput: RunningNotesInput,
  emit: ReturnType<typeof vi.fn>,
  hostPresentation: WidgetPresentationContext = presentation,
) => (
  <WidgetDraftTestScope
    definition={NOTES_APP_CONTRIBUTION.widgetDefinitions[0]}
    presentation={hostPresentation}
    input={widgetInput}
  >
    <RunningNotesWidget
      input={widgetInput}
      emit={emit as ComponentProps<typeof RunningNotesWidget>["emit"]}
      presentation={hostPresentation}
    />
  </WidgetDraftTestScope>
);

describe("RunningNotesWidget", () => {
  it("emits an exact versioned Markdown edit through the generic Notes intent", async () => {
    const emit = vi.fn();
    const { container } = render(
      renderNotes(input, emit),
    );

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const editor = screen.getByRole("textbox", { name: "Edit note" });
    await userEvent.clear(editor);
    await userEvent.type(editor, "  Revised **exactly**  ");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: "wb.notes.edit-requested",
        client_mutation_id: expect.stringMatching(/^notes-edit:/),
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

  it("confirms deletion and emits a versioned, idempotent mutation intent", async () => {
    const emit = vi.fn();
    const { container } = render(renderNotes(input, emit));

    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const firstDialog = screen.getByRole("alertdialog", {
      name: "Delete this running note?",
    });
    expect(firstDialog).toHaveTextContent(/keeps a tombstone/i);
    await userEvent.click(screen.getByRole("button", { name: "Keep note" }));
    expect(emit).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete note" }));

    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: "wb.notes.delete-requested",
        client_mutation_id: expect.stringMatching(/^notes-delete:/),
        view_id: presentation.viewId,
        instance_id: presentation.instanceId,
        payload: { item_id: "note-1", expected_version: 3 },
      }),
    );
    expect(screen.getByRole("button", { name: "Deleting…" })).toBeDisabled();
    await expectNoAccessibilityViolations(container);
  });

  it("detects a snapshot version conflict while preserving the local draft", async () => {
    const { rerender } = render(
      renderNotes(input, vi.fn()),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Edit note" }), " local");

    rerender(
      renderNotes(
        { ...input, revision: "r2", items: [{ ...item, version: 4 }] },
        vi.fn(),
      ),
    );

    expect(screen.getByText(/changed while you were editing/i)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Edit note" })).toHaveValue(
      "Meeting ran long local",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("renders a heavy grouped collection without truncating records", async () => {
    const items = Array.from({ length: 200 }, (_, index): MarkdownNoteItem => ({
      ...item,
      itemId: `note-${index}`,
      markdown: `Stress note ${index}`,
      groupId: index % 2 === 0 ? "Decisions" : "Questions",
    }));
    render(
      renderNotes(
        { ...input, displayMode: "grouped", items },
        vi.fn(),
        { ...presentation, sizeMode: "compact" },
      ),
    );

    expect(await screen.findByRole("heading", { name: "Decisions" })).toBeInTheDocument();
    expect(screen.getByText("Stress note 199")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(200);
  });

  it("keeps read-only notes legible while disabling edits", async () => {
    render(
      renderNotes(
        {
          ...input,
          access: { mode: "read_only", reason: "Archive is read-only." },
        },
        vi.fn(),
      ),
    );
    expect(await screen.findByText("Meeting ran long")).toBeInTheDocument();
    expect(screen.getByText("Archive is read-only.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });

  it("does not warn about expected edit limits when there are no notes", () => {
    render(
      renderNotes(
        {
          ...input,
          access: { mode: "read_only", reason: "Open a running note in Co-work to edit it." },
          items: [],
        },
        vi.fn(),
      ),
    );

    expect(screen.getByText("No running notes for this collection.")).toBeInTheDocument();
    expect(screen.queryByText("Open a running note in Co-work to edit it.")).not.toBeInTheDocument();
  });

  it("renders module-owned generated artifacts beside the collection", () => {
    render(
      renderNotes(
        {
          ...input,
          items: [],
          supplementalItems: [{
            itemId: "briefing-1",
            itemKind: "generated_artifact",
            text: "A generated daily briefing.",
            authorityKind: "native_plain",
          }],
        },
        vi.fn(),
      ),
    );

    expect(screen.getByRole("region", { name: "Other entries" })).toHaveTextContent(
      "A generated daily briefing.",
    );
    expect(screen.queryByText("No running notes for this collection.")).toBeNull();
  });

  it("restores a versioned tombstone through an explicit mutation", async () => {
    const emit = vi.fn().mockResolvedValue({ intent_id: "restore-note", status: "accepted" });
    render(renderNotes({ ...input, items: [], tombstones: [{ ...item, markdown: "Archived context" }] }, emit));

    await userEvent.click(screen.getByRole("button", { name: "Restore" }));

    expect(emit).toHaveBeenCalledWith(expect.objectContaining({
      intent_type: "wb.notes.restore-requested",
      client_mutation_id: expect.stringMatching(/^notes-restore:/u),
      payload: { item_id: "note-1", expected_version: 3 },
    }));
  });

  it("offers the source-bound Co-work document action even while note editing is read-only", async () => {
    const emit = vi.fn().mockResolvedValue({
      intent_id: "open-note",
      status: "accepted",
    });
    render(
      renderNotes(
        {
          ...input,
          access: { mode: "read_only", reason: "Managed by the daily note." },
          items: [
            {
              ...item,
              document: {
                state: "available",
                gestureContextSha256: "a".repeat(64),
              },
            },
          ],
        },
        emit,
      ),
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Open in Co-work" }),
    );

    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: "wb.notes.open-document-requested",
        payload: {
          item_id: "note-1",
          expected_version: 3,
          gesture_context_sha256: "a".repeat(64),
        },
      }),
    );
  });
});
