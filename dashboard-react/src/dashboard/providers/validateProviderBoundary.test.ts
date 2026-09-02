import { describe, expect, it } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
  type DashboardIntent,
  type EffectiveViewComposition,
  type ViewSnapshot,
} from "../contributions/contracts";
import {
  assertDashboardIntent,
  assertIntentResult,
  assertReconcileResult,
  assertViewSnapshot,
  assertWidgetSnapshot,
  ProviderContractError,
} from "./validateProviderBoundary";

const viewId = asViewId("example.validation.main");
const widgetTypeId = asWidgetTypeId("example.validation.summary");
const widgetInstanceId = asWidgetInstanceId("personal:summary");
const composition = (): EffectiveViewComposition => ({
  compositionId: "example-composition",
  revision: "composition-r1",
  defaultSlots: [{
    slotId: asWidgetSlotId("capture"),
    defaultInstanceId: asWidgetInstanceId("default:capture"),
    requiredRole: asWidgetRoleId("example.widget-role.capture@1"),
    defaultWidgetTypeId: widgetTypeId,
    presence: "required",
    help: { summary: "Capture an item.", details: "Provides the primary capture surface." },
    defaultSettings: {},
    defaultLayout: { x: 0, y: 0, w: 8, h: 4 },
    lockedReason: "The view requires its capture surface.",
  }],
  readingOrder: [asWidgetSlotId("capture")],
  mobileOrder: [asWidgetSlotId("capture")],
});
const snapshot = (): ViewSnapshot => ({
  viewId,
  revision: "r1",
  observedAt: "2026-07-12T12:00:00Z",
  status: "ready",
  quality: { kind: "complete" },
  model: { title: "Valid" },
  bindings: {},
  widgetInputs: {
    "default:capture": { instanceId: "default:capture" },
    "simple.capture": { instanceId: "simple.capture" },
    "profile/check-in": { instanceId: "profile/check-in" },
  },
});
const intent: DashboardIntent = {
  intent_type: "example.validation.update",
  schema_version: 1,
  intent_id: "intent-1",
  client_mutation_id: "mutation-1",
  view_id: viewId,
  instance_id: asWidgetInstanceId("custom.check-in"),
  payload: { title: "After" },
};

describe("Dashboard View API boundary validation", () => {
  it("accepts JSON-compatible snapshots, intents, results, and reconciliations", () => {
    expect(() => assertViewSnapshot(snapshot(), viewId)).not.toThrow();
    expect(() => assertDashboardIntent(intent, viewId)).not.toThrow();
    expect(() =>
      assertWidgetSnapshot(
        {
          widgetTypeId,
          instanceId: widgetInstanceId,
          revision: "r1",
          observedAt: "2026-07-12T12:00:00Z",
          status: "ready",
          quality: { kind: "complete" },
          input: { title: "Hydrated" },
        },
        widgetTypeId,
        widgetInstanceId,
      ),
    ).not.toThrow();
    expect(() =>
      assertIntentResult(
        {
          intent_id: intent.intent_id,
          client_mutation_id: intent.client_mutation_id,
          status: "accepted",
          revision: "r2",
        },
        intent,
      ),
    ).not.toThrow();
    expect(() =>
      assertReconcileResult(
        { changed: true, revision: "r1", snapshot: snapshot() },
        viewId,
      ),
    ).not.toThrow();
  });

  it("rejects mismatched, invalid-identity, or non-JSON widget snapshots", () => {
    expect(() =>
      assertWidgetSnapshot(
        {
          widgetTypeId: asWidgetTypeId("example.validation.other"),
          instanceId: widgetInstanceId,
          observedAt: "2026-07-12T12:00:00Z",
          status: "ready",
          quality: { kind: "complete" },
          input: { callback: () => undefined },
        },
        widgetTypeId,
        widgetInstanceId,
      ),
    ).toThrow(ProviderContractError);
    expect(() =>
      assertWidgetSnapshot(
        {
          widgetTypeId,
          instanceId: widgetInstanceId,
          revision: "r2",
          observedAt: "2026-07-12T12:00:00Z",
          status: "ready",
          quality: { kind: "complete" },
          input: {},
        },
        widgetTypeId,
        widgetInstanceId,
        "r1",
      ),
    ).toThrow(/does not match view revision/i);

    const invalidInstanceId = asWidgetInstanceId("not an instance");
    expect(() =>
      assertWidgetSnapshot(
        {
          widgetTypeId,
          instanceId: invalidInstanceId,
          observedAt: "2026-07-12T12:00:00Z",
          status: "ready",
          quality: { kind: "complete" },
          input: {},
        },
        widgetTypeId,
        invalidInstanceId,
      ),
    ).toThrow(/opaque instance ID/i);
  });

  it("accepts a structurally valid provider-authored composition", () => {
    const value = { ...snapshot(), effectiveComposition: composition() };
    expect(() => assertViewSnapshot(value, viewId)).not.toThrow();
  });

  it("rejects duplicate composition identities and missing widget inputs", () => {
    const valid = composition();
    const duplicateSlot = {
      ...valid,
      defaultSlots: [valid.defaultSlots[0], valid.defaultSlots[0]],
    };
    expect(() =>
      assertViewSnapshot({ ...snapshot(), effectiveComposition: duplicateSlot }, viewId),
    ).toThrow(/duplicate slot capture/i);

    const duplicateInstance = {
      ...valid,
      defaultSlots: [
        valid.defaultSlots[0],
        { ...valid.defaultSlots[0]!, slotId: asWidgetSlotId("secondary") },
      ],
      readingOrder: [asWidgetSlotId("capture"), asWidgetSlotId("secondary")],
      mobileOrder: [asWidgetSlotId("capture"), asWidgetSlotId("secondary")],
    };
    expect(() =>
      assertViewSnapshot({ ...snapshot(), effectiveComposition: duplicateInstance }, viewId),
    ).toThrow(/duplicate instance default:capture/i);

    const missingInput = snapshot();
    expect(() =>
      assertViewSnapshot({
        ...missingInput,
        effectiveComposition: composition(),
        widgetInputs: {},
      }, viewId),
    ).toThrow(/must contain composed instance default:capture/i);
  });

  it("rejects malformed or incomplete composition order arrays", () => {
    const valid = composition();
    expect(() =>
      assertViewSnapshot({ ...snapshot(), effectiveComposition: [] }, viewId),
    ).toThrow(/effectiveComposition must be an object/i);
    expect(() =>
      assertViewSnapshot({
        ...snapshot(),
        effectiveComposition: { ...valid, readingOrder: [] },
      }, viewId),
    ).toThrow(/readingOrder must include slot capture/i);
    expect(() =>
      assertViewSnapshot({
        ...snapshot(),
        effectiveComposition: {
          ...valid,
          mobileOrder: [asWidgetSlotId("capture"), asWidgetSlotId("capture")],
        },
      }, viewId),
    ).toThrow(/mobileOrder must not contain duplicate slot capture/i);
  });

  it("rejects non-JSON provider models and invalid instance keys", () => {
    const invalid = {
      ...snapshot(),
      model: { callback: () => undefined },
      widgetInputs: { "not an instance": {} },
    };
    expect(() => assertViewSnapshot(invalid, viewId)).toThrow(ProviderContractError);
  });

  it("rejects non-string, whitespace, control, and overlong instance identities", () => {
    expect(() =>
      assertDashboardIntent({ ...intent, instance_id: 123 } as unknown, viewId),
    ).toThrow(/opaque instance ID/i);
    for (const instance_id of ["module capture", "module\ncapture", `m${"x".repeat(128)}`]) {
      expect(() =>
        assertDashboardIntent({ ...intent, instance_id } as unknown, viewId),
      ).toThrow(/opaque instance ID/i);
    }
  });

  it("rejects mismatched intent and revision envelopes", () => {
    expect(() =>
      assertIntentResult({ intent_id: "other", status: "accepted" }, intent),
    ).toThrow(/echo intent_id/i);
    expect(() =>
      assertReconcileResult(
        { changed: true, revision: "r2", snapshot: snapshot() },
        viewId,
      ),
    ).toThrow(/must match/i);
  });
});
