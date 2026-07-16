import { describe, expect, it, vi } from "vitest";
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
  type JournalCaptureSubmitIntent,
  type JournalRunningNoteDeleteIntent,
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
      defaultLayout: { x: 0, y: 0, w: 8, h: 14 },
    });
    expect(captureSlot.lockedReason).toMatch(/cannot record/i);
    expect(timelineSlot).toMatchObject({
      presence: "required",
      defaultWidgetTypeId: JOURNAL_WIDGET_TYPE_IDS.timeline,
      defaultLayout: { x: 8, y: 0, w: 16, h: 16 },
    });
    expect(timelineSlot.lockedReason).toMatch(/cannot reconcile/i);
    expect(runningNotesSlot).toMatchObject({
      presence: "default_on",
      defaultWidgetTypeId: JOURNAL_WIDGET_TYPE_IDS.runningNotes,
      defaultLayout: { x: 0, y: 14, w: 8, h: 6 },
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

  it("publishes a local invalidation when mounted demo smart processing settles", async () => {
    vi.useFakeTimers();
    try {
      const provider = new InMemoryJournalProvider(undefined, { settlementDelayMs: 25 });
      const listener = vi.fn();
      const unsubscribe = provider.subscribeInvalidations(listener);

      const result = await provider.dispatch(
        toDashboardJournalIntent(JULY11_SMART_CAPTURE_INTENT),
      );
      expect(result.status).toBe("accepted");
      expect(listener).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(25);
      expect(listener).toHaveBeenCalledOnce();
      expect(listener).toHaveBeenCalledWith(
        expect.objectContaining({
          appId: JOURNAL_APP_ID,
          viewIds: [JOURNAL_VIEW_DEFINITION_ID],
          revision: JULY11_SMART_SETTLED_REVISION,
          reason: "demo-smart-processing-settled",
        }),
      );

      const settled = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
        reason: "reconcile",
        knownRevision: result.revision,
      });
      expect(settled.revision).toBe(JULY11_SMART_SETTLED_REVISION);
      unsubscribe();
    } finally {
      vi.useRealTimers();
    }
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

  it("routes Smart Auto capture through the App boundary and rejects dumb Auto", async () => {
    const provider = new InMemoryJournalProvider();
    const autoIntent = {
      intent_type: "wb.capture.submit",
      schema_version: 1,
      intent_id: "intent:auto-route",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.capture,
      client_mutation_id: "capture:auto-route",
      payload: {
        day_id: "journal-day:2026-07-11:America/New_York:05:00",
        target_id: "auto",
        mode: "smart",
        exact_text: "Meeting ran long",
      },
    } as const satisfies JournalCaptureSubmitIntent;

    const result = await provider.dispatch(toDashboardJournalIntent(autoIntent));
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "refresh",
    });
    const submissions = snapshot.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.capture
    ].recentSubmissions;
    const notes = snapshot.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
    ].items;

    expect(result).toMatchObject({
      status: "accepted",
      message: "Smart routed exact text to Running notes",
    });
    expect(submissions[submissions.length - 1]).toMatchObject({
      targetId: "running_notes",
      mode: "smart",
      exactText: "Meeting ran long",
    });
    expect(notes[notes.length - 1]?.markdown).toBe("Meeting ran long");

    const rejected = await provider.dispatch(
      toDashboardJournalIntent({
        ...autoIntent,
        intent_id: "intent:auto-route-dumb",
        client_mutation_id: "capture:auto-route-dumb",
        payload: { ...autoIntent.payload, mode: "dumb" },
      }),
    );
    expect(rejected).toMatchObject({
      status: "rejected",
      message: "Auto capture requires Smart mode",
    });
  });

  it("turns arbitrary Log text into an exact point record and deduplicates retries", async () => {
    const provider = new InMemoryJournalProvider();
    const intent = {
      intent_type: "wb.capture.submit",
      schema_version: 1,
      intent_id: "intent:arbitrary-log",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.capture,
      client_mutation_id: "capture:arbitrary-log",
      payload: {
        day_id: "journal-day:2026-07-11:America/New_York:05:00",
        target_id: "log",
        mode: "dumb",
        exact_text: "Shipped the Journal calendar integration",
      },
    } as const satisfies JournalCaptureSubmitIntent;

    const result = await provider.dispatch(toDashboardJournalIntent(intent));
    const duplicate = await provider.dispatch(toDashboardJournalIntent(intent));
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "refresh",
    });
    const timeline = snapshot.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.timeline
    ].items;
    const records = timeline.filter(
      (item) => item.itemId === "timeline:capture:arbitrary-log",
    );
    const submissions = snapshot.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.capture
    ].recentSubmissions;
    const capture = submissions[submissions.length - 1];

    expect(result).toMatchObject({
      status: "accepted",
      message: "Exact text persisted as a point record",
    });
    expect(duplicate).toEqual(result);
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({
      kind: "record",
      shape: "point",
      at: snapshot.model.day.now,
      title: intent.payload.exact_text,
    });
    expect(capture).toMatchObject({
      clientMutationId: intent.client_mutation_id,
      exactText: intent.payload.exact_text,
      persistenceStatus: "persisted",
      processingStatus: "not_requested",
    });
  });

  it("removes a Running note from the active collection while retaining a tombstone", async () => {
    const provider = new InMemoryJournalProvider();
    const before = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "mount" });
    const note = before.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
    ].items[0]!;
    const intent = {
      intent_type: "wb.notes.delete-requested",
      schema_version: 1,
      intent_id: "intent:delete-running-note",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
      client_mutation_id: "notes-delete:running-note-1",
      payload: {
        item_id: note.itemId,
        expected_version: note.version,
      },
    } as const satisfies JournalRunningNoteDeleteIntent;

    const result = await provider.dispatch(toDashboardJournalIntent(intent));
    const duplicate = await provider.dispatch(toDashboardJournalIntent(intent));
    const after = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "refresh",
    });
    const tombstone = provider.getRunningNoteTombstone(note.itemId);

    expect(result).toMatchObject({
      status: "accepted",
      message: "Running note deleted and tombstoned",
    });
    expect(duplicate).toEqual(result);
    expect(
      after.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items,
    ).not.toContainEqual(expect.objectContaining({ itemId: note.itemId }));
    expect(tombstone).toEqual({
      item: note,
      deletedAt: before.model.day.now,
      deletedVersion: note.version + 1,
      deletedBy: { source: "user", label: "you" },
      reason: "user_deleted",
    });
  });

  it("does not resurrect a tombstoned Running note when pending Smart work settles", async () => {
    const provider = new InMemoryJournalProvider();
    const captureIntent = {
      intent_type: "wb.capture.submit",
      schema_version: 1,
      intent_id: "intent:pending-note",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.capture,
      client_mutation_id: "capture:pending-note",
      payload: {
        day_id: "journal-day:2026-07-11:America/New_York:05:00",
        target_id: "running_notes",
        mode: "smart",
        exact_text: "Pending note that I no longer need",
      },
    } as const satisfies JournalCaptureSubmitIntent;
    expect(
      (await provider.dispatch(toDashboardJournalIntent(captureIntent))).status,
    ).toBe("accepted");
    const pending = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "refresh",
    });
    const note = pending.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
    ].items.find((item) => item.itemId === "running-note:capture:pending-note")!;
    const deleteIntent = {
      intent_type: "wb.notes.delete-requested",
      schema_version: 1,
      intent_id: "intent:delete-pending-note",
      view_id: "wb.journal.main",
      instance_id: JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
      client_mutation_id: "notes-delete:pending-note",
      payload: { item_id: note.itemId, expected_version: note.version },
    } as const satisfies JournalRunningNoteDeleteIntent;
    expect(
      (await provider.dispatch(toDashboardJournalIntent(deleteIntent))).status,
    ).toBe("accepted");

    expect(provider.advanceDemoProcessing()).toBe(true);
    const settled = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "reconcile",
    });
    expect(
      settled.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items,
    ).not.toContainEqual(expect.objectContaining({ itemId: note.itemId }));
    expect(provider.getRunningNoteTombstone(note.itemId)?.item.markdown).toBe(
      captureIntent.payload.exact_text,
    );
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
