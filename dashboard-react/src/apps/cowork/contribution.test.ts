import { describe, expect, it } from "vitest";

import { ContributionRegistry } from "../../dashboard/contributions/registry";
import { validateAppContribution } from "../../dashboard/contributions/validate";
import {
  COWORK_VIEW_ID,
  COWORK_WORKSPACE_MODULE_ID,
  COWORK_WORKSPACE_TYPE_ID,
} from "./bindings";
import { COWORK_APP_CONTRIBUTION } from "./contribution";
import { COWORK_VIEW_MODULE } from "./viewModule";
import { COWORK_WORKSPACE_WIDGET_MODULE } from "./widgetModule";

const register = (registry: ContributionRegistry) =>
  registry.registerApp(
    COWORK_APP_CONTRIBUTION,
    [COWORK_WORKSPACE_WIDGET_MODULE],
    [COWORK_VIEW_MODULE],
    { trust: "native" },
  );

describe("Co-work App contribution", () => {
  it("validates cleanly once its renderer module is in context", () => {
    expect(
      validateAppContribution(COWORK_APP_CONTRIBUTION, {
        widgetModules: new Map([
          [COWORK_WORKSPACE_MODULE_ID, COWORK_WORKSPACE_WIDGET_MODULE],
        ]),
      }),
    ).toEqual([]);
  });

  it("registers its composite widget and view module at native trust", () => {
    const registry = new ContributionRegistry();
    const receipt = register(registry);
    expect(receipt.trust).toBe("native");
    expect(receipt.viewIds).toContain(COWORK_VIEW_ID);
    expect(receipt.widgetTypeIds).toEqual([COWORK_WORKSPACE_TYPE_ID]);
  });

  it("exposes the workspace as a standard-grid view placing one durable widget", () => {
    const registry = new ContributionRegistry();
    register(registry);

    const view = registry.requireView(COWORK_VIEW_ID);
    expect(view.definition.layoutKind).toBeUndefined();
    expect(view.definition.surface).toBeUndefined();
    expect(view.definition.route).toBe("cowork");
    expect(view.definition.defaultSlots).toHaveLength(1);

    const widget = registry.requireWidget(COWORK_WORKSPACE_TYPE_ID);
    expect(widget.definition.durable).toBe(true);
    expect(widget.definition.multiplicity).toBe("single_per_view");
    expect(widget.definition.drafts ?? []).toEqual([]);
    expect(widget.definition.outputIntentSchemas).toEqual([]);
  });

  it("loads the view module's coarse runtime and the widget module's renderer", async () => {
    const registry = new ContributionRegistry();
    register(registry);

    const loadedView = await registry.loadViewModule(COWORK_VIEW_ID);
    expect(loadedView.createRuntime).toBeTypeOf("function");
    // The view no longer exports a single-surface renderer. The durable widget does.
    expect(loadedView.surface).toBeUndefined();

    const loadedWidget = await registry.loadWidgetModule(COWORK_WORKSPACE_TYPE_ID);
    expect(loadedWidget.default).toBeTypeOf("function");
  });
});
