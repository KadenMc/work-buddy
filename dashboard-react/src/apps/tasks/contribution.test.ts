import { describe, expect, it } from "vitest";

import { createContributionRegistry } from "../../dashboard/contributions/registry";
import { TASKS_VIEW_ID } from "./bindings";
import { TASKS_APP_CONTRIBUTION } from "./contribution";
import { TASK_INTENTS } from "./contracts";
import { TASKS_VIEW_DEFINITION } from "./viewDefinition";
import { TASKS_VIEW_MODULE } from "./viewModule";
import { TASKS_WIDGET_MODULES } from "./widgetModule";

describe("Tasks contribution", () => {
  it("registers the stable Tasks view and both required widgets", () => {
    const registry = createContributionRegistry();
    registry.registerApp(
      TASKS_APP_CONTRIBUTION,
      TASKS_WIDGET_MODULES,
      [TASKS_VIEW_MODULE],
      { trust: "native" },
    );

    expect(registry.requireView(TASKS_VIEW_ID).definition).toBe(TASKS_VIEW_DEFINITION);
    expect(TASKS_VIEW_DEFINITION.route).toBe("tasks");
    expect(TASKS_VIEW_DEFINITION.navigation.order).toBe(20);
    expect(TASKS_VIEW_DEFINITION.defaultSlots.map((slot) => slot.presence)).toEqual([
      "required",
      "required",
    ]);
    expect(TASKS_VIEW_DEFINITION.readingOrder).toEqual(
      TASKS_VIEW_DEFINITION.mobileOrder,
    );
  });

  it("declares device drafts and full Theme Contract support", () => {
    const [quickAdd, workspace] = TASKS_APP_CONTRIBUTION.widgetDefinitions;
    expect(quickAdd.drafts?.[0]).toMatchObject({
      draftName: "task-create",
      persistence: "device",
      clearPolicy: "confirm",
      retentionDays: 30,
    });
    expect(workspace.drafts?.[0]).toMatchObject({
      draftName: "task-edit",
      persistence: "device",
      scope: { kind: "input-field", path: ["selectedTask", "task_id"] },
    });
    for (const widget of [quickAdd, workspace]) {
      expect(widget.theme).toMatchObject({
        contractVersion: 1,
        styling: "semantic-tokens",
        supports: ["light", "dark", "forced-colors", "reduced-motion"],
      });
      expect(widget.multiplicity).toBe("single_per_view");
      expect(widget.durable).not.toBe(true);
    }
  });

  it("declares server batch preview as a Quick Add read intent", () => {
    const [quickAdd, workspace] = TASKS_APP_CONTRIBUTION.widgetDefinitions;

    expect(quickAdd.outputIntentSchemas).toContainEqual({
      schemaId: TASK_INTENTS.batchPreview,
      version: 1,
    });
    expect(quickAdd.outputIntentEffects).toContainEqual({
      schema: { schemaId: TASK_INTENTS.batchPreview, version: 1 },
      effect: "read",
      preview: "block",
    });
    expect(workspace.outputIntentSchemas).not.toContainEqual({
      schemaId: TASK_INTENTS.batchPreview,
      version: 1,
    });
  });
});
