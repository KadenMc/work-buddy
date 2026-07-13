import { fireEvent, render, screen } from "@testing-library/react";
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

const theme: ResolvedThemeSummary = {
  contractVersion: 1,
  preference: { scheme: "dark", skinId: "wb.default" },
  resolvedScheme: "dark",
  skin: { id: "wb.default", version: 1, publisherAppId: "wb.core" },
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
    const view = render(
      <QuickTextCaptureWidget
        input={input}
        emit={(intent) => emitted.push(intent)}
        presentation={presentation(JOURNAL_INSTANCE_IDS.capture)}
      />,
    );

    await user.selectOptions(screen.getByRole("combobox", { name: "Destination" }), "running_notes");
    await user.type(screen.getByRole("textbox", { name: "Capture text" }), "Meeting ran long");
    await user.click(screen.getByRole("button", { name: "Capture" }));

    expect(emitted).toHaveLength(1);
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

    view.rerender(
      <QuickTextCaptureWidget
        input={toQuickTextCaptureInput(
          pending.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture],
        )}
        emit={(intent) => emitted.push(intent)}
        presentation={presentation(JOURNAL_INSTANCE_IDS.capture)}
      />,
    );
    expect(screen.getByRole("textbox", { name: "Capture text" })).toHaveValue("");

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
    render(
      <QuickTextCaptureWidget
        input={toQuickTextCaptureInput(
          snapshot.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture],
        )}
        emit={(intent) => emitted.push(intent)}
        presentation={presentation(JOURNAL_INSTANCE_IDS.capture)}
      />,
    );

    await user.click(screen.getByRole("radio", { name: "Save only" }));
    await user.type(screen.getByRole("textbox", { name: "Capture text" }), "Coffee refill");
    await user.click(screen.getByRole("button", { name: "Capture" }));
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
        emit={(intent) => emitted.push(intent)}
        presentation={presentation(JOURNAL_INSTANCE_IDS.timeline, "expanded")}
      />,
    );

    await user.click(screen.getByRole("button", { name: "List" }));
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
    render(
      <RunningNotesWidget
        input={toRunningNotesInput(
          before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes],
        )}
        emit={(intent) => emitted.push(intent)}
        presentation={presentation(JOURNAL_INSTANCE_IDS.runningNotes)}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Edit" }));
    const exactMarkdown = "  Revised **Markdown** stays exact.  ";
    fireEvent.change(screen.getByRole("textbox", { name: "Edit note" }), {
      target: { value: exactMarkdown },
    });
    await user.click(screen.getByRole("button", { name: "Save" }));

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

  it("keeps the pre-05:00 instant bound to the prior Journal day in the Timeline renderer", () => {
    const input = toDayTimelineInput(
      JOURNAL_PRE_0500_BOUNDARY_FIXTURE.model.widgetInputs[
        JOURNAL_WIDGET_INSTANCE_IDS.timeline
      ],
    );
    render(
      <DayTimelineWidget
        input={input}
        emit={() => undefined}
        presentation={presentation(JOURNAL_INSTANCE_IDS.timeline, "compact")}
      />,
    );

    expect(input.day.localDate).toBe("2026-07-11");
    expect(input.day.now).toBe("2026-07-12T04:30:00-04:00");
    expect(
      screen.getByRole("button", { name: /Captured before the Journal day boundary/i }),
    ).toBeInTheDocument();
  });
});
