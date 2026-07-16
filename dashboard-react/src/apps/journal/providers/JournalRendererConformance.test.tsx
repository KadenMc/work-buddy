import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import {
  createContributionRegistry,
} from "../../../dashboard/contributions/registry";
import type {
  CanvasThemeSnapshot,
  ResolvedThemeSummary,
} from "../../../dashboard/contributions/themeContract";
import type {
  DashboardIntent,
  WidgetInstanceId,
  WidgetPresentationContext,
} from "../../../dashboard/contributions/contracts";
import { WidgetDraftTestScope } from "../../../test/DashboardTestRuntime";
import QuickTextCaptureWidget from "../../../widget-library/capture/QuickTextCaptureWidget";
import {
  CAPTURE_APP_CONTRIBUTION,
  QUICK_TEXT_CAPTURE_MODULE,
} from "../../../widget-library/capture/contribution";
import RunningNotesWidget from "../../../widget-library/notes/RunningNotesWidget";
import {
  NOTES_APP_CONTRIBUTION,
  RUNNING_NOTES_MODULE,
} from "../../../widget-library/notes/contribution";
import DayTimelineWidget from "../../../widget-library/timeline/DayTimelineWidget";
import {
  DAY_TIMELINE_MODULE,
  TIMELINE_APP_CONTRIBUTION,
} from "../../../widget-library/timeline/contribution";
import {
  JOURNAL_INSTANCE_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
} from "../bindings";
import { JOURNAL_WIDGET_INSTANCE_IDS, type JournalTimelineItem } from "../contracts";
import {
  JULY11_FIXED_ITEM_IDS,
  JULY11_PROTECTED_ITEM_IDS,
} from "../fixtures/july11";
import { JOURNAL_PRE_0500_BOUNDARY_FIXTURE } from "../fixtures/states";
import {
  toDayTimelineInput,
  toQuickTextCaptureInput,
  toRunningNotesInput,
} from "../rendererBindings";
import { JOURNAL_APP_CONTRIBUTION } from "../contribution";
import { JOURNAL_VIEW_DEFINITION } from "../viewDefinition";
import { InMemoryJournalProvider } from "./InMemoryJournalProvider";

const collectIntent = (emitted: DashboardIntent[]) => async (intent: DashboardIntent) => {
  emitted.push(intent);
  return { intent_id: intent.intent_id, status: "accepted" as const };
};

const theme: ResolvedThemeSummary = {
  contractVersion: 1,
  preference: { scheme: "dark", skinId: "wb.default" },
  resolvedScheme: "dark",
  skin: { id: "wb.default", version: 2, publisherAppId: "wb.core" },
  accessibility: {
    forcedColors: false,
    reducedMotion: false,
    reducedTransparency: false,
  },
};

const canvasTheme: CanvasThemeSnapshot = {
  surfaceCanvas: "#0d1117",
  surfaceRaised: "#161b22",
  textPrimary: "#e6edf3",
  textSecondary: "#9da7b3",
  borderDefault: "#30363d",
  focusRing: "#58a6ff",
  dataSeries: ["#d87857", "#58a6ff", "#3fb950"],
};

const presentation = (
  instanceId: WidgetInstanceId,
  sizeMode: "compact" | "standard" | "expanded" = "standard",
): WidgetPresentationContext => ({
  instanceId,
  viewId: JOURNAL_VIEW_DEFINITION_ID,
  width: sizeMode === "compact" ? 320 : sizeMode === "standard" ? 560 : 880,
  height: sizeMode === "compact" ? 280 : sizeMode === "standard" ? 520 : 760,
  sizeMode,
  interactionMode: "operate",
  editing: false,
  theme,
  getCanvasTheme: () => canvasTheme,
});

const itemById = (items: readonly JournalTimelineItem[], itemId: string) => {
  const item = items.find((candidate) => candidate.itemId === itemId);
  if (item === undefined) throw new Error(`Missing timeline item ${itemId}`);
  return item;
};

describe("Journal and the real widget library", () => {
  it("registers external roles/types before the Journal view contribution", () => {
    const registry = createContributionRegistry();
    registry.registerApp(CAPTURE_APP_CONTRIBUTION, [QUICK_TEXT_CAPTURE_MODULE]);
    registry.registerApp(TIMELINE_APP_CONTRIBUTION, [DAY_TIMELINE_MODULE]);
    registry.registerApp(NOTES_APP_CONTRIBUTION, [RUNNING_NOTES_MODULE]);
    registry.registerApp(JOURNAL_APP_CONTRIBUTION, []);

    expect(registry.requireView(JOURNAL_VIEW_DEFINITION_ID).definition).toBe(
      JOURNAL_VIEW_DEFINITION,
    );
    expect(registry.requireWidget(JOURNAL_VIEW_DEFINITION.defaultSlots[0].defaultWidgetTypeId))
      .toBeDefined();
    expect(JOURNAL_VIEW_DEFINITION.defaultSlots.map((slot) => slot.presence)).toEqual([
      "required",
      "default_on",
      "required",
    ]);
  });

  it("accepts a real Capture renderer intent and exposes exact text only by provider revision", async () => {
    const user = userEvent.setup();
    const provider = new InMemoryJournalProvider();
    const before = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const initialTimeline = before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items;
    const emitted: DashboardIntent[] = [];
    const input = toQuickTextCaptureInput(
      before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture],
    );
    const capturePresentation = presentation(JOURNAL_INSTANCE_IDS.capture);
    const view = render(
      <WidgetDraftTestScope
        definition={CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0]}
        presentation={capturePresentation}
        input={input}
      >
        <QuickTextCaptureWidget
          input={input}
          emit={collectIntent(emitted)}
          presentation={capturePresentation}
        />
      </WidgetDraftTestScope>,
    );

    await user.click(await screen.findByRole("button", { name: /Destination/ }));
    await user.click(await screen.findByRole("option", { name: /^Running notes/ }));
    await user.type(screen.getByRole("textbox", { name: "Capture text" }), "Meeting ran long");
    await user.click(screen.getByRole("button", { name: "Capture" }));

    await waitFor(() => expect(emitted).toHaveLength(1));
    expect(emitted[0]).toMatchObject({
      intent_type: "wb.capture.submit",
      view_id: JOURNAL_VIEW_DEFINITION_ID,
      instance_id: JOURNAL_INSTANCE_IDS.capture,
      payload: {
        target_id: "running_notes",
        mode: "smart",
        exact_text: "Meeting ran long",
      },
    });

    const result = await provider.dispatch(emitted[0]!);
    expect(result.status).toBe("accepted");
    const pending = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });
    expect(pending.revision).not.toBe(before.revision);
    expect(pending.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items).toEqual(
      initialTimeline,
    );
    const pendingNotes = pending.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items;
    expect(pendingNotes[pendingNotes.length - 1]?.markdown).toBe("Meeting ran long");

    const pendingInput = toQuickTextCaptureInput(
      pending.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture],
    );
    view.rerender(
      <WidgetDraftTestScope
        definition={CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0]}
        presentation={capturePresentation}
        input={pendingInput}
      >
        <QuickTextCaptureWidget
          input={pendingInput}
          emit={collectIntent(emitted)}
          presentation={capturePresentation}
        />
      </WidgetDraftTestScope>,
    );
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Capture text" })).toHaveValue(""),
    );

    expect(provider.advanceDemoProcessing()).toBe(true);
    const settled = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "reconcile" });
    const settledTimeline = settled.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items;
    for (const itemId of [...JULY11_PROTECTED_ITEM_IDS, ...JULY11_FIXED_ITEM_IDS]) {
      expect(itemById(settledTimeline, itemId)).toEqual(itemById(initialTimeline, itemId));
    }
  });

  it("preserves dumb Capture semantics through the real renderer", async () => {
    const user = userEvent.setup();
    const provider = new InMemoryJournalProvider();
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const emitted: DashboardIntent[] = [];
    const captureInput = toQuickTextCaptureInput(
      snapshot.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture],
    );
    const capturePresentation = presentation(JOURNAL_INSTANCE_IDS.capture);
    render(
      <WidgetDraftTestScope
        definition={CAPTURE_APP_CONTRIBUTION.widgetDefinitions[0]}
        presentation={capturePresentation}
        input={captureInput}
      >
        <QuickTextCaptureWidget
          input={captureInput}
          emit={collectIntent(emitted)}
          presentation={capturePresentation}
        />
      </WidgetDraftTestScope>,
    );

    const smart = await screen.findByRole("switch", { name: "Smart" });
    expect(smart).toBeChecked();
    await user.click(smart);
    expect(smart).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: /Destination/ }));
    await user.click(screen.getByRole("option", { name: /^Log/ }));
    await user.type(screen.getByRole("textbox", { name: "Capture text" }), "Coffee refill");
    await user.click(screen.getByRole("button", { name: "Capture" }));
    await waitFor(() => expect(emitted).toHaveLength(1));
    expect(await provider.dispatch(emitted[0]!)).toMatchObject({ status: "accepted" });

    const next = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });
    const submissions =
      next.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].recentSubmissions;
    const submission = submissions[submissions.length - 1];
    expect(submission).toMatchObject({
      exactText: "Coffee refill",
      processingStatus: "not_requested",
    });
    expect(submission?.annotation).toBeUndefined();
    expect(provider.advanceDemoProcessing()).toBe(false);
  });

  it("routes Timeline renderer intents through the provider without moving protected items", async () => {
    const user = userEvent.setup();
    const provider = new InMemoryJournalProvider();
    const before = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const initialTimeline = before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items;
    const emitted: DashboardIntent[] = [];
    render(
      <DayTimelineWidget
        input={toDayTimelineInput(
          before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline],
        )}
        emit={collectIntent(emitted)}
        presentation={presentation(JOURNAL_INSTANCE_IDS.timeline, "expanded")}
      />,
    );

    await user.click(screen.getByRole("radio", { name: "List" }));
    await user.click(screen.getByRole("button", { name: "Request replan" }));
    expect(emitted.map((intent) => intent.intent_type)).toEqual([
      "wb.timeline.render-mode-changed",
      "wb.timeline.replan-requested",
    ]);
    expect((await provider.dispatch(emitted[0]!)).status).toBe("accepted");
    expect((await provider.dispatch(emitted[1]!)).status).toBe("accepted");

    const next = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });
    expect(next.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].renderMode).toBe(
      "list",
    );
    const replanned = next.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items;
    for (const itemId of [...JULY11_PROTECTED_ITEM_IDS, ...JULY11_FIXED_ITEM_IDS]) {
      expect(itemById(replanned, itemId)).toEqual(itemById(initialTimeline, itemId));
    }
    expect(itemById(replanned, "timeline:prototype-mobile")).not.toEqual(
      itemById(initialTimeline, "timeline:prototype-mobile"),
    );
  });

  it("routes exact Markdown edits from the real Notes renderer through a new revision", async () => {
    const user = userEvent.setup();
    const provider = new InMemoryJournalProvider();
    const before = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const initialTimeline = before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items;
    const emitted: DashboardIntent[] = [];
    const notesInput = toRunningNotesInput(
      before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes],
    );
    const notesPresentation = presentation(JOURNAL_INSTANCE_IDS.runningNotes);
    render(
      <WidgetDraftTestScope
        definition={NOTES_APP_CONTRIBUTION.widgetDefinitions[0]}
        presentation={notesPresentation}
        input={notesInput}
      >
        <RunningNotesWidget
          input={notesInput}
          emit={collectIntent(emitted)}
          presentation={notesPresentation}
        />
      </WidgetDraftTestScope>,
    );

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const exactMarkdown = "  Revised **Markdown** stays exact.  ";
    fireEvent.change(screen.getByRole("textbox", { name: "Edit note" }), {
      target: { value: exactMarkdown },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(emitted).toHaveLength(1));
    expect(emitted[0]).toMatchObject({
      intent_type: "wb.notes.edit-requested",
      payload: { expected_version: 1, markdown: exactMarkdown },
    });
    expect((await provider.dispatch(emitted[0]!)).status).toBe("accepted");
    const next = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });
    const updated = next.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items[0];
    expect(updated?.markdown).toBe(exactMarkdown);
    expect(updated?.version).toBe(2);
    expect(next.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items).toEqual(
      initialTimeline,
    );
  });

  it("routes a confirmed Notes deletion through the App boundary as a tombstone", async () => {
    const user = userEvent.setup();
    const provider = new InMemoryJournalProvider();
    const before = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "mount",
    });
    const note = before.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
    ].items[0]!;
    const emitted: DashboardIntent[] = [];
    const notesInput = toRunningNotesInput(
      before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes],
    );
    const notesPresentation = presentation(JOURNAL_INSTANCE_IDS.runningNotes);
    render(
      <WidgetDraftTestScope
        definition={NOTES_APP_CONTRIBUTION.widgetDefinitions[0]}
        presentation={notesPresentation}
        input={notesInput}
      >
        <RunningNotesWidget
          input={notesInput}
          emit={collectIntent(emitted)}
          presentation={notesPresentation}
        />
      </WidgetDraftTestScope>,
    );

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete note" }));

    await waitFor(() => expect(emitted).toHaveLength(1));
    expect(emitted[0]).toMatchObject({
      intent_type: "wb.notes.delete-requested",
      client_mutation_id: expect.stringMatching(/^notes-delete:/),
      payload: { item_id: note.itemId, expected_version: note.version },
    });
    expect((await provider.dispatch(emitted[0]!)).status).toBe("accepted");
    const next = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "refresh",
    });
    expect(
      next.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items,
    ).toHaveLength(0);
    expect(provider.getRunningNoteTombstone(note.itemId)?.item).toEqual(note);
  });

  it("keeps the pre-05:00 instant bound to the prior Journal day in the Timeline renderer", async () => {
    const user = userEvent.setup();
    const input = toDayTimelineInput(
      JOURNAL_PRE_0500_BOUNDARY_FIXTURE.model.widgetInputs[
        JOURNAL_WIDGET_INSTANCE_IDS.timeline
      ],
    );
    render(
      <DayTimelineWidget
        input={input}
        emit={async (intent) => ({ intent_id: intent.intent_id, status: "accepted" })}
        presentation={presentation(JOURNAL_INSTANCE_IDS.timeline, "compact")}
      />,
    );

    expect(input.day.localDate).toBe("2026-07-11");
    expect(input.day.now).toBe("2026-07-12T04:30:00-04:00");
    await user.click(screen.getByRole("radio", { name: "List" }));
    const preBoundaryItem = await screen.findByRole("button", {
      name: /Captured before the Journal day boundary/i,
    });
    await user.click(preBoundaryItem);
    expect(
      await screen.findByRole("heading", {
        name: "Captured before the Journal day boundary",
      }),
    ).toBeInTheDocument();
    expect(within(await screen.findByRole("dialog")).getByText("4:25 AM")).toBeInTheDocument();
  });
});
