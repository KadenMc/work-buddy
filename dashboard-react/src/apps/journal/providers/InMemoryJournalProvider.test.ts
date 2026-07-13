import { describe, expect, it } from "vitest";
import {
  asAppId,
  asViewId,
  type DefaultWidgetSlot,
  type WidgetSlotId,
} from "../../../dashboard/contributions/contracts";
import {
  JOURNAL_APP_ID,
  JOURNAL_BINDING_KEYS,
  JOURNAL_INSTANCE_IDS,
  JOURNAL_SLOT_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_IDS,
  toDashboardJournalIntent,
} from "../bindings";
import {
  JOURNAL_WIDGET_INSTANCE_IDS,
  type JournalTimelineItem,
} from "../contracts";
import {
  JULY11_DUMB_CAPTURE_INTENT,
  JULY11_FIXED_ITEM_IDS,
  JULY11_PROTECTED_ITEM_IDS,
  JULY11_SMART_CAPTURE_INTENT,
  JULY11_SMART_SETTLED_REVISION,
} from "../fixtures/july11";
import { JOURNAL_READ_ONLY_FIXTURE } from "../fixtures/states";
import { JOURNAL_APP_CONTRIBUTION } from "../contribution";
import { JOURNAL_VIEW_DEFINITION } from "../viewDefinition";
import { InMemoryJournalProvider } from "./InMemoryJournalProvider";

const itemById = (items: readonly JournalTimelineItem[], itemId: string) => {
  const item = items.find((candidate) => candidate.itemId === itemId);
  if (item === undefined) throw new Error(`Missing timeline item ${itemId}`);
  return item;
};

const slotById = (slotId: WidgetSlotId): DefaultWidgetSlot => {
  const slot = JOURNAL_VIEW_DEFINITION.defaultSlots.find(
    (candidate) => candidate.slotId === slotId,
  );
  if (slot === undefined) throw new Error(`Missing Journal slot ${slotId}`);
  return slot;
};

describe("Journal contribution policy", () => {
  it("declares external widget types with required Capture and Timeline purposes", () => {
    expect(JOURNAL_APP_CONTRIBUTION.appId).toBe(JOURNAL_APP_ID);
    expect(JOURNAL_APP_CONTRIBUTION.widgetDefinitions).toEqual([]);
    expect(JOURNAL_VIEW_DEFINITION.route).toBe("journal");
    expect(JOURNAL_VIEW_DEFINITION.grid.columns).toBe(24);

    const captureSlot = slotById(JOURNAL_SLOT_IDS.capture);
    const timelineSlot = slotById(JOURNAL_SLOT_IDS.timeline);
    const runningNotesSlot = slotById(JOURNAL_SLOT_IDS.runningNotes);
    expect(captureSlot).toMatchObject({
      presence: "required",
      defaultWidgetTypeId: JOURNAL_WIDGET_TYPE_IDS.capture,
      defaultLayout: { x: 0, y: 0, w: 8, h: 4 },
    });
    expect(captureSlot.lockedReason).toMatch(/cannot record/i);
    expect(timelineSlot).toMatchObject({
      presence: "required",
      defaultWidgetTypeId: JOURNAL_WIDGET_TYPE_IDS.timeline,
      defaultLayout: { x: 8, y: 0, w: 16, h: 12 },
    });
    expect(timelineSlot.lockedReason).toMatch(/cannot reconcile/i);
    expect(runningNotesSlot).toMatchObject({
      presence: "default_on",
      defaultWidgetTypeId: JOURNAL_WIDGET_TYPE_IDS.runningNotes,
      defaultLayout: { x: 0, y: 4, w: 8, h: 8 },
    });
    expect(runningNotesSlot.lockedReason).toBeUndefined();
    expect(JOURNAL_VIEW_DEFINITION.mobileOrder).toEqual([
      JOURNAL_SLOT_IDS.capture,
      JOURNAL_SLOT_IDS.timeline,
      JOURNAL_SLOT_IDS.runningNotes,
    ]);
  });
});

describe("InMemoryJournalProvider", () => {
  it("loads a demo-marked view with typed day/access/quality/source bindings", async () => {
    const provider = new InMemoryJournalProvider();
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "mount",
    });

    expect(snapshot.status).toBe("ready");
    expect(snapshot.quality.kind).toBe("demo");
    expect(snapshot.model.source.kind).toBe("in_memory");
    expect(snapshot.bindings[JOURNAL_BINDING_KEYS.day]).toBe(snapshot.model.day);
    expect(snapshot.bindings[JOURNAL_BINDING_KEYS.access]).toBe(snapshot.model.access);
  });

  it("binds an external widget type only to its matching Journal instance", async () => {
    const provider = new InMemoryJournalProvider();
    const capture = await provider.loadWidget(JOURNAL_WIDGET_TYPE_IDS.capture, {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      instanceId: JOURNAL_INSTANCE_IDS.capture,
    });
    const wrongType = await provider.loadWidget(JOURNAL_WIDGET_TYPE_IDS.timeline, {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      instanceId: JOURNAL_INSTANCE_IDS.capture,
    });

    expect(capture.status).toBe("ready");
    expect(capture.input?.instanceId).toBe(JOURNAL_WIDGET_INSTANCE_IDS.capture);
    expect(wrongType.status).toBe("unavailable");
    expect(wrongType.input).toBeNull();
  });

  it("moves smart capture through pending and settled provider revisions", async () => {
    const provider = new InMemoryJournalProvider();
    const before = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const initialTimeline = before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items;

    const result = await provider.dispatch(toDashboardJournalIntent(JULY11_SMART_CAPTURE_INTENT));
    const acceptedReconcile = await provider.reconcile({
      id: "journal-demo-capture-accepted",
      appId: JOURNAL_APP_ID,
      viewIds: [JOURNAL_VIEW_DEFINITION_ID],
      revision: result.revision,
      reason: "intent accepted",
      observedAt: "2026-07-11T12:18:01-04:00",
    });
    const pending = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "refresh",
      knownRevision: before.revision,
    });
    const pendingCapture =
      pending.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].recentSubmissions;
    const pendingNotes = pending.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items;

    expect(result).toMatchObject({ status: "accepted", revision: pending.revision });
    expect(acceptedReconcile.snapshot?.revision).toBe(pending.revision);
    expect(pendingCapture[pendingCapture.length - 1]?.exactText).toBe("Meeting ran long");
    expect(pendingCapture[pendingCapture.length - 1]?.processingStatus).toBe("pending");
    expect(pendingNotes[pendingNotes.length - 1]?.markdown).toBe("Meeting ran long");
    expect(pending.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items).toEqual(
      initialTimeline,
    );

    const duplicate = await provider.dispatch(
      toDashboardJournalIntent(JULY11_SMART_CAPTURE_INTENT),
    );
    expect(duplicate).toEqual(result);

    const reconciled = await provider.reconcile({
      id: "journal-demo-processing-settled",
      appId: JOURNAL_APP_ID,
      viewIds: [JOURNAL_VIEW_DEFINITION_ID],
      revision: JULY11_SMART_SETTLED_REVISION,
      reason: "demo smart processing settled",
      observedAt: "2026-07-11T12:18:06-04:00",
    });
    expect(reconciled.changed).toBe(true);

    const settled = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "reconcile",
      knownRevision: pending.revision,
    });
    const settledTimeline =
      settled.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items;
    for (const itemId of [...JULY11_PROTECTED_ITEM_IDS, ...JULY11_FIXED_ITEM_IDS]) {
      expect(itemById(settledTimeline, itemId)).toEqual(itemById(initialTimeline, itemId));
    }
    expect(itemById(settledTimeline, "timeline:prototype-mobile")).not.toEqual(
      itemById(initialTimeline, "timeline:prototype-mobile"),
    );
    const settledNotes =
      settled.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items;
    const settledNote = settledNotes[settledNotes.length - 1];
    expect(settledNote?.markdown).toBe("Meeting ran long");
    expect(settledNote?.processing.state).toBe("succeeded");
  });

  it("persists dumb Log capture without scheduling per-entry processing", async () => {
    const provider = new InMemoryJournalProvider();
    const before = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const beforeNotes = before.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items;

    const result = await provider.dispatch(toDashboardJournalIntent(JULY11_DUMB_CAPTURE_INTENT));
    const after = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });
    const captures = after.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.capture
    ].recentSubmissions;

    expect(result.status).toBe("accepted");
    expect(captures[captures.length - 1]?.processingStatus).toBe("not_requested");
    expect(after.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items).toEqual(
      beforeNotes,
    );
    expect(provider.advanceDemoProcessing()).toBe(false);
  });

  it("rejects mutations while preserving a readable read-only snapshot", async () => {
    const provider = new InMemoryJournalProvider(JOURNAL_READ_ONLY_FIXTURE);
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const result = await provider.dispatch(toDashboardJournalIntent(JULY11_DUMB_CAPTURE_INTENT));

    expect(snapshot.status).toBe("read-only");
    expect(snapshot.model.access.mode).toBe("read_only");
    expect(result.status).toBe("unavailable");
    expect(provider.revision).toBe(snapshot.revision);
  });

  it("advances an injected clock without wall-clock waits", async () => {
    const provider = new InMemoryJournalProvider();
    const revision = provider.advanceClock("2026-07-11T12:19:00-04:00");
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });

    expect(snapshot.revision).toBe(revision);
    expect(snapshot.model.day.now).toBe("2026-07-11T12:19:00-04:00");
    expect(
      Object.values(snapshot.model.widgetInputs).every(
        (input) => input.revision === snapshot.revision,
      ),
    ).toBe(true);
    expect(
      snapshot.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].day.now,
    ).toBe("2026-07-11T12:19:00-04:00");
  });

  it("ignores invalidations for another App or view", async () => {
    const provider = new InMemoryJournalProvider();
    const appMismatch = await provider.reconcile({
      id: "other-app-event",
      appId: asAppId("example.other"),
      reason: "unrelated",
      observedAt: "2026-07-11T12:18:01-04:00",
    });
    const viewMismatch = await provider.reconcile({
      id: "other-view-event",
      appId: JOURNAL_APP_ID,
      viewIds: [asViewId("wb.journal.other")],
      reason: "unrelated",
      observedAt: "2026-07-11T12:18:02-04:00",
    });

    expect(appMismatch.changed).toBe(false);
    expect(viewMismatch.changed).toBe(false);
  });
});
