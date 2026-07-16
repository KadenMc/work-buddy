import { describe, expect, it, vi } from "vitest";

import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetTypeId,
  type ViewSnapshot,
  type WidgetSnapshot,
} from "../contributions/contracts";
import { FixtureNotFoundError, FixtureViewProvider } from "./FixtureViewProvider";

const appId = asAppId("example.fixtures");
const viewId = asViewId("example.fixtures.main");
const widgetTypeId = asWidgetTypeId("example.fixtures.card");
const instanceId = asWidgetInstanceId("default:card");

const viewSnapshot: ViewSnapshot<{ readonly title: string }> = {
  viewId,
  revision: 1,
  observedAt: "2026-07-12T12:00:00Z",
  status: "ready",
  quality: { kind: "demo", message: "Demo data" },
  model: { title: "Fixture" },
  bindings: {},
  widgetInputs: { [instanceId]: { title: "Card" } },
};

const widgetSnapshot: WidgetSnapshot<{ readonly title: string }> = {
  widgetTypeId,
  instanceId,
  revision: 1,
  observedAt: viewSnapshot.observedAt,
  status: "ready",
  quality: viewSnapshot.quality,
  input: { title: "Card" },
};

describe("FixtureViewProvider", () => {
  it("returns isolated deterministic view and widget snapshots", async () => {
    const provider = new FixtureViewProvider({
      appId,
      viewSnapshots: [viewSnapshot],
      widgetSnapshots: [widgetSnapshot],
    });

    const first = await provider.loadView(viewId, { reason: "mount" });
    const second = await provider.loadView(viewId, { reason: "refresh" });
    expect(first).toEqual(viewSnapshot);
    expect(first).not.toBe(second);
    expect(
      await provider.loadWidget(widgetTypeId, { viewId, instanceId }),
    ).toEqual(widgetSnapshot);
  });

  it("fails truthfully for unknown fixture data and undeclared intents", async () => {
    const provider = new FixtureViewProvider({ appId, viewSnapshots: [viewSnapshot] });

    await expect(
      provider.loadView(asViewId("example.fixtures.missing"), { reason: "mount" }),
    ).rejects.toBeInstanceOf(FixtureNotFoundError);
    await expect(
      provider.loadWidget(widgetTypeId, { viewId, instanceId }),
    ).rejects.toBeInstanceOf(FixtureNotFoundError);
    await expect(
      provider.dispatch({
        intent_type: "example.fixtures.unknown",
        schema_version: 1,
        intent_id: "intent-1",
        view_id: viewId,
        payload: {},
      }),
    ).resolves.toMatchObject({ intent_id: "intent-1", status: "unavailable" });
  });

  it("delegates declared intents/reconciliation without leaking mutable fixtures", async () => {
    const intentHandler = vi.fn(() => ({
      intent_id: "handler-overridden",
      status: "accepted" as const,
      revision: 2,
    }));
    const reconcile = vi.fn(() => ({
      changed: true,
      revision: 2,
      snapshot: { ...viewSnapshot, revision: 2 },
    }));
    const provider = new FixtureViewProvider({
      appId,
      viewSnapshots: [viewSnapshot],
      intentHandlers: { "example.fixtures.refresh": intentHandler },
      reconcile,
    });

    await expect(
      provider.dispatch({
        intent_type: "example.fixtures.refresh",
        schema_version: 1,
        intent_id: "intent-2",
        view_id: viewId,
        payload: {},
      }),
    ).resolves.toMatchObject({ intent_id: "intent-2", status: "accepted", revision: 2 });
    await expect(
      provider.reconcile({
        id: "event-2",
        appId,
        viewIds: [viewId],
        revision: 2,
        reason: "fixture changed",
        observedAt: viewSnapshot.observedAt,
      }),
    ).resolves.toMatchObject({ changed: true, revision: 2 });
    expect(intentHandler).toHaveBeenCalledOnce();
    expect(reconcile).toHaveBeenCalledOnce();
  });
});
