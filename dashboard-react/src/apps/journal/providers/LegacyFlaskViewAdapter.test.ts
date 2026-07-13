import { describe, expect, it, vi } from "vitest";

import {
  asAppId,
  asViewId,
} from "../../../dashboard/contributions/contracts";
import {
  JOURNAL_APP_ID,
  JOURNAL_INSTANCE_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_IDS,
} from "../bindings";
import {
  LEGACY_JOURNAL_UNSUPPORTED,
  LEGACY_TODAY_ENDPOINT,
  LegacyFlaskViewAdapter,
  type LegacyJournalViewModel,
} from "./LegacyFlaskViewAdapter";

const READY_PAYLOAD = {
  status: "ok",
  now: {
    iso: "2026-07-11T16:18:00.000Z",
    local_hhmm: "12:18",
    minutes_into_day: 12 * 60 + 18,
  },
  work_hours: [9, 17],
  current_contexts: ["@filesystem"],
  recommendations: [
    { task_id: "t-1", text: "A useful untimed recommendation", state: "focused" },
  ],
  plan: [
    {
      time_start: "10:00",
      time_end: "11:00",
      text: "Mapped plan row",
      checked: true,
    },
    {
      time_start: "12:20",
      time_end: "13:30",
      text: "Prototype mobile timeline",
      checked: false,
    },
    {
      time_start: "14:00",
      time_end: "14:45",
      text: "[Cal] Northwind project review",
      checked: false,
    },
  ],
  plan_status: "ok",
  focused_count: 2,
  calendar_event_count: 1,
  active_contracts: [{ contract_id: "c-1" }],
  contract_constraints: [{ kind: "wip" }],
  engage_count: 4,
  errors: [],
} as const;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function modelOrThrow(model: LegacyJournalViewModel | null): LegacyJournalViewModel {
  if (model === null) throw new Error("Expected a legacy Journal model");
  return model;
}

describe("LegacyFlaskViewAdapter", () => {
  it("projects only genuine Today time and plan fields into the generic Timeline", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(READY_PAYLOAD));
    const provider = new LegacyFlaskViewAdapter({
      fetchImpl,
      timezone: "America/New_York",
    });

    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "mount",
    });
    const model = modelOrThrow(snapshot.model);
    const timeline = snapshot.widgetInputs[JOURNAL_INSTANCE_IDS.timeline];

    expect(fetchImpl).toHaveBeenCalledWith(LEGACY_TODAY_ENDPOINT, {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    expect(snapshot).toMatchObject({
      status: "read-only",
      quality: { kind: "partial" },
    });
    expect(snapshot.quality.message).toMatch(/partial read-only legacy today/i);
    expect(model.source).toEqual({ kind: "live" });
    expect(model.legacy).toMatchObject({
      endpoint: LEGACY_TODAY_ENDPOINT,
      sourceStatus: "ok",
      recommendations: READY_PAYLOAD.recommendations,
      calendarEventCount: 1,
      timelineWindowSource: "legacy_work_hours",
      revisionSource: "adapter_projection_hash",
    });
    expect(model.legacy.limitations).toEqual(LEGACY_JOURNAL_UNSUPPORTED);
    expect(model.day).toMatchObject({
      localDate: "2026-07-11",
      timezone: "America/New_York",
      dayBoundaryStart: "unknown",
      windowStart: "2026-07-11T13:00:00.000Z",
      windowEnd: "2026-07-11T21:00:00.000Z",
      now: READY_PAYLOAD.now.iso,
    });
    expect(timeline).toBeDefined();
    expect(timeline?.access).toMatchObject({ mode: "read_only" });
    expect(timeline?.items).toHaveLength(3);
    expect(timeline?.items.map((item) => item.kind)).toEqual([
      "plan",
      "plan",
      "plan",
    ]);
    expect(timeline?.items[0]).toMatchObject({
      title: "Mapped plan row",
      status: "completed",
      mutability: "past_protected",
      precision: "derived",
      provenance: { source: "planner", label: "Legacy Today plan" },
      startAt: "2026-07-11T14:00:00.000Z",
      endAt: "2026-07-11T15:00:00.000Z",
    });
    expect(timeline?.items[1]?.mutability).toBe("editable");
    // A text prefix and aggregate count are not trustworthy calendar provenance.
    expect(timeline?.items[2]).toMatchObject({
      title: "[Cal] Northwind project review",
      kind: "plan",
      provenance: { source: "planner" },
    });
    expect(timeline?.items.some((item) => item.kind === "calendar")).toBe(false);
    expect(timeline?.items.some((item) => item.kind === "record")).toBe(false);
    expect(timeline?.items.some((item) => item.title.includes("recommendation"))).toBe(
      false,
    );
  });

  it("preserves the Journal slots while omitting unsupported renderer inputs", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(READY_PAYLOAD));
    const provider = new LegacyFlaskViewAdapter({ fetchImpl, timezone: "UTC" });
    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "mount",
    });

    expect(Object.keys(snapshot.widgetInputs)).toEqual([JOURNAL_INSTANCE_IDS.timeline]);
    expect(snapshot.widgetInputs[JOURNAL_INSTANCE_IDS.capture]).toBeUndefined();
    expect(snapshot.widgetInputs[JOURNAL_INSTANCE_IDS.runningNotes]).toBeUndefined();

    const capture = await provider.loadWidget(JOURNAL_WIDGET_TYPE_IDS.capture, {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      instanceId: JOURNAL_INSTANCE_IDS.capture,
    });
    const runningNotes = await provider.loadWidget(
      JOURNAL_WIDGET_TYPE_IDS.runningNotes,
      {
        viewId: JOURNAL_VIEW_DEFINITION_ID,
        instanceId: JOURNAL_INSTANCE_IDS.runningNotes,
      },
    );
    expect(capture).toMatchObject({ status: "unavailable", input: null });
    expect(capture.quality.message).toMatch(/no capture persistence contract/i);
    expect(runningNotes).toMatchObject({ status: "unavailable", input: null });
    expect(runningNotes.quality.message).toMatch(/native Running Notes records/i);
    // Unsupported widget reads use the known partial snapshot and never call another API.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("rejects every intent without issuing a write request", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(READY_PAYLOAD));
    const provider = new LegacyFlaskViewAdapter({ fetchImpl, timezone: "UTC" });
    const before = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "mount",
    });
    const result = await provider.dispatch({
      intent_type: "wb.capture.submit",
      schema_version: 1,
      intent_id: "legacy-write-attempt",
      client_mutation_id: "mutation-1",
      view_id: JOURNAL_VIEW_DEFINITION_ID,
      instance_id: JOURNAL_INSTANCE_IDS.capture,
      payload: { exact_text: "must not be sent" },
    });

    expect(result).toMatchObject({
      intent_id: "legacy-write-attempt",
      client_mutation_id: "mutation-1",
      status: "unavailable",
      revision: before.revision,
    });
    expect(result.message).toMatch(/read-only projection/i);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("marks a degraded payload explicitly while retaining genuine readable plan data", async () => {
    const degraded = {
      ...READY_PAYLOAD,
      status: "degraded",
      errors: ["Calendar source unavailable"],
    };
    const provider = new LegacyFlaskViewAdapter({
      fetchImpl: vi.fn(async () => jsonResponse(degraded)),
      timezone: "America/New_York",
    });

    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "mount",
    });
    const model = modelOrThrow(snapshot.model);

    expect(snapshot.status).toBe("read-only");
    expect(snapshot.quality).toMatchObject({ kind: "partial" });
    expect(snapshot.quality.message).toMatch(/partial\/degraded/i);
    expect(model.legacy.sourceStatus).toBe("degraded");
    expect(model.legacy.errors).toEqual(["Calendar source unavailable"]);
    expect(model.quality.issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ message: "Calendar source unavailable" }),
      ]),
    );
    expect(snapshot.widgetInputs[JOURNAL_INSTANCE_IDS.timeline]?.items).toHaveLength(3);
  });

  it("keeps a source-reported error distinct from transport unavailability", async () => {
    const provider = new LegacyFlaskViewAdapter({
      fetchImpl: vi.fn(async () =>
        jsonResponse({ ...READY_PAYLOAD, status: "error", errors: ["Planner failed"] }),
      ),
      timezone: "America/New_York",
    });

    const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "mount",
    });

    expect(snapshot.status).toBe("error");
    expect(snapshot.quality.kind).toBe("partial");
    expect(modelOrThrow(snapshot.model).legacy.sourceStatus).toBe("error");
  });

  it("returns an unmistakable unavailable snapshot on HTTP, network, or schema failure", async () => {
    const cases = [
      vi.fn(async () => jsonResponse({ status: "error", error: "failed" }, 500)),
      vi.fn(async () => {
        throw new Error("network down");
      }),
      vi.fn(async () => jsonResponse({ status: "ok", now: {} })),
    ];

    for (const fetchImpl of cases) {
      const provider = new LegacyFlaskViewAdapter({
        fetchImpl,
        timezone: "UTC",
        clock: () => "2026-07-11T16:19:00.000Z",
      });
      const snapshot = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
        reason: "mount",
      });
      expect(snapshot).toMatchObject({
        observedAt: "2026-07-11T16:19:00.000Z",
        status: "unavailable",
        quality: { kind: "partial" },
        model: null,
        bindings: {},
        widgetInputs: {},
      });
      expect(snapshot.quality.message).toMatch(/endpoint unavailable/i);
    }
  });

  it("re-fetches queryable truth on matching invalidations and hashes projected changes", async () => {
    const changed = {
      ...READY_PAYLOAD,
      plan: [
        ...READY_PAYLOAD.plan,
        {
          time_start: "15:00",
          time_end: "15:30",
          text: "New plan row",
          checked: false,
        },
      ],
    };
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(READY_PAYLOAD))
      .mockResolvedValueOnce(jsonResponse(READY_PAYLOAD))
      .mockResolvedValueOnce(jsonResponse(changed));
    const provider = new LegacyFlaskViewAdapter({
      fetchImpl,
      timezone: "America/New_York",
    });
    const initial = await provider.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "mount",
    });
    const unchanged = await provider.reconcile({
      id: "today-same",
      appId: JOURNAL_APP_ID,
      viewIds: [JOURNAL_VIEW_DEFINITION_ID],
      reason: "refresh",
      observedAt: READY_PAYLOAD.now.iso,
    });
    const updated = await provider.reconcile({
      id: "today-changed",
      appId: JOURNAL_APP_ID,
      viewIds: [JOURNAL_VIEW_DEFINITION_ID],
      reason: "refresh",
      observedAt: READY_PAYLOAD.now.iso,
    });

    expect(unchanged).toEqual({ changed: false, revision: initial.revision });
    expect(updated.changed).toBe(true);
    expect(updated.revision).not.toBe(initial.revision);
    expect(updated.snapshot?.widgetInputs[JOURNAL_INSTANCE_IDS.timeline]).toMatchObject({
      items: expect.arrayContaining([expect.objectContaining({ title: "New plan row" })]),
    });

    const appMismatch = await provider.reconcile({
      id: "other-app",
      appId: asAppId("example.other"),
      reason: "unrelated",
      observedAt: READY_PAYLOAD.now.iso,
    });
    const viewMismatch = await provider.reconcile({
      id: "other-view",
      appId: JOURNAL_APP_ID,
      viewIds: [asViewId("wb.journal.other")],
      reason: "unrelated",
      observedAt: READY_PAYLOAD.now.iso,
    });
    expect(appMismatch.changed).toBe(false);
    expect(viewMismatch.changed).toBe(false);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it("refuses a non-Journal view and a mismatched widget binding", async () => {
    const provider = new LegacyFlaskViewAdapter({
      fetchImpl: vi.fn(async () => jsonResponse(READY_PAYLOAD)),
      timezone: "UTC",
    });
    await expect(
      provider.loadView(asViewId("wb.journal.other"), { reason: "mount" }),
    ).rejects.toThrow(/cannot load view/i);

    const mismatched = await provider.loadWidget(JOURNAL_WIDGET_TYPE_IDS.capture, {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      instanceId: JOURNAL_INSTANCE_IDS.timeline,
    });
    expect(mismatched).toMatchObject({ status: "unavailable", input: null });
  });
});
