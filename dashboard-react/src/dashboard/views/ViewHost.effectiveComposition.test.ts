import { describe, expect, it } from "vitest";

import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
  type ViewDefinition,
} from "../contributions/contracts";
import { definitionWithEffectiveComposition } from "./ViewHost";

const slot = (name: string) => ({
  slotId: asWidgetSlotId(name),
  defaultInstanceId: asWidgetInstanceId(`instance:${name}`),
  requiredRole: asWidgetRoleId("role:test"),
  defaultWidgetTypeId: asWidgetTypeId("widget:test"),
  presence: "default_on" as const,
  help: { summary: name, details: name },
  defaultSettings: {},
  defaultLayout: { x: 0, y: 0, w: 24, h: 8 },
});

const definition: ViewDefinition = {
  viewId: asViewId("view:test"),
  definitionVersion: 1,
  ownerAppId: asAppId("app:test"),
  displayName: "Test",
  route: "test",
  navigation: { label: "Test", order: 1 },
  primaryJob: "Test compositions",
  grid: { columns: 24 },
  defaultSlots: [slot("legacy")],
  readingOrder: [asWidgetSlotId("legacy")],
  mobileOrder: [asWidgetSlotId("legacy")],
};

describe("provider-resolved view composition", () => {
  it("replaces only slot composition and preserves the registered view identity", () => {
    const current = slot("current");
    const resolved = definitionWithEffectiveComposition(definition, {
      effectiveComposition: {
        compositionId: "composition-1",
        revision: 4,
        defaultSlots: [current],
        readingOrder: [current.slotId],
        mobileOrder: [current.slotId],
      },
    });

    expect(resolved).toMatchObject({
      viewId: definition.viewId,
      route: definition.route,
      defaultSlots: [current],
      readingOrder: [current.slotId],
    });
    expect(definition.defaultSlots[0]?.slotId).toBe("legacy");
  });
});
