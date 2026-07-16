import { describe, expect, it } from "vitest";
import {
  JOURNAL_WIDGET_INSTANCE_IDS,
  type JournalFixtureState,
  type JournalTimelineItem,
} from "../contracts";
import {
  JULY11_DAY,
  JULY11_DUMB_CAPTURE_INTENT,
  JULY11_DUMB_CAPTURE_TRANSITION,
  JULY11_DUMB_PERSISTED_FIXTURE,
  JULY11_FIXED_ITEM_IDS,
  JULY11_INITIAL_REVISION,
  JULY11_INITIAL_TIMELINE_ITEMS,
  JULY11_PROTECTED_ITEM_IDS,
  JULY11_READY_FIXTURE,
  JULY11_REVISED_TIMELINE_ITEMS,
  JULY11_SMART_CAPTURE_INTENT,
  JULY11_SMART_CAPTURE_TRANSITION,
  JULY11_SMART_PENDING_FIXTURE,
  JULY11_SMART_SETTLED_FIXTURE,
} from "./july11";

function populatedModel(fixture: JournalFixtureState) {
  if (fixture.model === null) {
    throw new Error(`Expected populated fixture: ${fixture.fixtureId}`);
  }
  return fixture.model;
}

function itemById(items: readonly JournalTimelineItem[], itemId: string) {
  const item = items.find((candidate) => candidate.itemId === itemId);
  if (!item) throw new Error(`Missing timeline item: ${itemId}`);
  return item;
}

describe("July 11 Journal fixture", () => {
  it("keeps the configured day boundary distinct from actual openedAt", () => {
    expect(JULY11_DAY.dayBoundaryStart).toBe("05:00");
    expect(JULY11_DAY.windowStart).toBe("2026-07-11T05:00:00-04:00");
    expect(JULY11_DAY.openedAt).toBe("2026-07-11T08:42:00-04:00");
    expect(JULY11_DAY.openedAt).not.toBe(JULY11_DAY.windowStart);
  });

  it("models record, calendar, and plan items with explicit mutability", () => {
    const items = populatedModel(JULY11_READY_FIXTURE).widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.timeline
    ].items;

    expect(new Set(items.map((item) => item.kind))).toEqual(
      new Set(["record", "calendar", "plan"]),
    );
    expect(items.filter((item) => item.mutability === "past_protected")).toHaveLength(3);
    expect(items.filter((item) => item.mutability === "fixed")).toHaveLength(1);
    expect(items.filter((item) => item.mutability === "editable")).toHaveLength(2);
  });

  it("publishes smart capture through provider-owned accepted and settled revisions", () => {
    const [accepted, settled] = JULY11_SMART_CAPTURE_TRANSITION.phases;

    expect(JULY11_SMART_CAPTURE_TRANSITION.fromRevision).toBe(JULY11_INITIAL_REVISION);
    expect(accepted?.phase).toBe("accepted");
    expect(settled?.phase).toBe("settled");
    expect(accepted?.snapshot.model.revision).toBe("journal:july11:r2-smart-pending");
    expect(settled?.snapshot.model.revision).toBe("journal:july11:r3-smart-settled");
    expect(accepted?.changedInstanceIds).toEqual([
      JOURNAL_WIDGET_INSTANCE_IDS.capture,
      JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
    ]);
    expect(settled?.changedInstanceIds).toContain(JOURNAL_WIDGET_INSTANCE_IDS.timeline);
  });

  it("persists exact smart-capture text before async processing completes", () => {
    const pending = populatedModel(JULY11_SMART_PENDING_FIXTURE);
    const capture = pending.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture];
    const notes = pending.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes];
    const submission = capture.recentSubmissions[capture.recentSubmissions.length - 1];
    const note = notes.items[notes.items.length - 1];

    expect(submission?.exactText).toBe(JULY11_SMART_CAPTURE_INTENT.payload.exact_text);
    expect(submission?.persistenceStatus).toBe("persisted");
    expect(submission?.processingStatus).toBe("pending");
    expect(note?.markdown).toBe(JULY11_SMART_CAPTURE_INTENT.payload.exact_text);
    expect(note?.processing.state).toBe("pending");
    expect(pending.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items).toEqual(
      JULY11_INITIAL_TIMELINE_ITEMS,
    );
  });

  it("settles smart annotation without rewriting the original text", () => {
    const settled = populatedModel(JULY11_SMART_SETTLED_FIXTURE);
    const submissions =
      settled.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].recentSubmissions;
    const submission = submissions[submissions.length - 1];
    const notes = settled.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items;
    const note = notes[notes.length - 1];

    expect(submission?.exactText).toBe(JULY11_SMART_CAPTURE_INTENT.payload.exact_text);
    expect(submission?.processingStatus).toBe("succeeded");
    expect(submission?.annotation?.effects).toContain("Replanned editable future blocks");
    expect(note?.markdown).toBe(JULY11_SMART_CAPTURE_INTENT.payload.exact_text);
    expect(note?.processing.state).toBe("succeeded");
  });

  it("replans only editable future items and preserves past and fixed items", () => {
    for (const itemId of [...JULY11_PROTECTED_ITEM_IDS, ...JULY11_FIXED_ITEM_IDS]) {
      const initial = itemById(JULY11_INITIAL_TIMELINE_ITEMS, itemId);
      const settled = itemById(JULY11_REVISED_TIMELINE_ITEMS, itemId);
      expect(settled).toEqual(initial);
    }

    expect(itemById(JULY11_REVISED_TIMELINE_ITEMS, "timeline:prototype-mobile")).not.toEqual(
      itemById(JULY11_INITIAL_TIMELINE_ITEMS, "timeline:prototype-mobile"),
    );
    expect(
      itemById(JULY11_REVISED_TIMELINE_ITEMS, "timeline:review-tracker-schema"),
    ).not.toEqual(itemById(JULY11_INITIAL_TIMELINE_ITEMS, "timeline:review-tracker-schema"));
  });

  it("never enters per-entry processing for a dumb capture", () => {
    const model = populatedModel(JULY11_DUMB_PERSISTED_FIXTURE);
    const submission = model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.capture
    ].recentSubmissions;
    const lastSubmission = submission[submission.length - 1];
    const timeline = model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline];
    const lastTimelineItem = timeline.items[timeline.items.length - 1];

    expect(lastSubmission?.exactText).toBe(JULY11_DUMB_CAPTURE_INTENT.payload.exact_text);
    expect(lastSubmission?.processingStatus).toBe("not_requested");
    expect(lastSubmission?.annotation).toBeUndefined();
    expect(lastTimelineItem?.title).toBe(JULY11_DUMB_CAPTURE_INTENT.payload.exact_text);
    expect(JULY11_DUMB_CAPTURE_TRANSITION.phases).toHaveLength(1);
    expect(JULY11_DUMB_CAPTURE_TRANSITION.phases[0]?.invariants).toContain(
      "no_per_entry_compute",
    );
  });

  it.each([
    JULY11_READY_FIXTURE,
    JULY11_SMART_PENDING_FIXTURE,
    JULY11_SMART_SETTLED_FIXTURE,
    JULY11_DUMB_PERSISTED_FIXTURE,
  ])("keeps all widget inputs on the provider's %s revision", (fixture) => {
    const model = populatedModel(fixture);
    expect(
      Object.values(model.widgetInputs).every((input) => input.revision === model.revision),
    ).toBe(true);
  });
});
