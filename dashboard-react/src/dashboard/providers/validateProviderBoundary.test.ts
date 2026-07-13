import { describe, expect, it } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  asWidgetTypeId,
  type DashboardIntent,
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
const snapshot = (): ViewSnapshot => ({
  viewId,
  revision: "r1",
  observedAt: "2026-07-12T12:00:00Z",
  status: "ready",
  quality: { kind: "complete" },
  model: { title: "Valid" },
  bindings: {},
  widgetInputs: { "default:capture": { instanceId: "default:capture" } },
});
const intent: DashboardIntent = {
  intent_type: "example.validation.update",
  schema_version: 1,
  intent_id: "intent-1",
  client_mutation_id: "mutation-1",
  view_id: viewId,
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

  it("rejects mismatched or non-JSON widget snapshots", () => {
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
  });

  it("rejects non-JSON provider models and invalid instance keys", () => {
    const invalid = {
      ...snapshot(),
      model: { callback: () => undefined },
      widgetInputs: { "not an instance": {} },
    };
    expect(() => assertViewSnapshot(invalid, viewId)).toThrow(ProviderContractError);
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
