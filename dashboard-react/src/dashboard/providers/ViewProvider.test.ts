import { describe, expect, it } from "vitest";

import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetTypeId,
  type ViewSnapshot,
} from "../contributions/contracts";
import type { ViewProvider } from "./ViewProvider";

const viewId = asViewId("example.provider.main");
const widgetTypeId = asWidgetTypeId("example.provider.card");
const instanceId = asWidgetInstanceId("default:card");

describe("ViewProvider", () => {
  it("keeps snapshot loading, widget loading, intent dispatch, and reconciliation behind one boundary", async () => {
    const snapshot: ViewSnapshot<{ readonly title: string }> = {
      viewId,
      revision: 2,
      observedAt: "2026-07-12T12:00:00Z",
      status: "ready",
      quality: { kind: "demo" },
      model: { title: "Provider fixture" },
      bindings: {},
      widgetInputs: { [instanceId]: { title: "Card" } },
    };
    const provider: ViewProvider = {
      appId: asAppId("example.provider"),
      loadView: async () => snapshot,
      loadWidget: async () => ({
        widgetTypeId,
        instanceId,
        revision: 2,
        observedAt: snapshot.observedAt,
        status: "ready",
        quality: snapshot.quality,
        input: { title: "Card" },
      }),
      dispatch: async (intent) => ({
        intent_id: intent.intent_id,
        status: "accepted",
        revision: 3,
      }),
      reconcile: async () => ({ changed: true, revision: 3, snapshot }),
    };

    expect((await provider.loadView(viewId, { reason: "mount" })).model).toEqual({
      title: "Provider fixture",
    });
    expect(
      (
        await provider.loadWidget(widgetTypeId, {
          viewId,
          instanceId,
        })
      ).instanceId,
    ).toBe(instanceId);
    expect(
      await provider.dispatch({
        intent_type: "example.provider.refresh-requested",
        schema_version: 1,
        intent_id: "intent-1",
        view_id: viewId,
        payload: {},
      }),
    ).toMatchObject({ status: "accepted", revision: 3 });
    expect(
      await provider.reconcile({
        id: "event-1",
        appId: provider.appId,
        viewIds: [viewId],
        revision: 3,
        reason: "fixture changed",
        observedAt: snapshot.observedAt,
      }),
    ).toMatchObject({ changed: true, revision: 3 });
  });
});

