import { describe, expect, it } from "vitest";

import { ContributionRegistry } from "../../dashboard/contributions/registry";
import { validateAppContribution } from "../../dashboard/contributions/validate";
import { COWORK_VIEW_ID } from "./bindings";
import { COWORK_APP_CONTRIBUTION } from "./contribution";
import { COWORK_VIEW_MODULE } from "./viewModule";

describe("Co-work App contribution", () => {
  it("validates cleanly with no registry context", () => {
    expect(validateAppContribution(COWORK_APP_CONTRIBUTION)).toEqual([]);
  });

  it("registers with its view module at native trust", () => {
    const registry = new ContributionRegistry();
    const receipt = registry.registerApp(
      COWORK_APP_CONTRIBUTION,
      [],
      [COWORK_VIEW_MODULE],
      { trust: "native" },
    );
    expect(receipt.trust).toBe("native");
    expect(receipt.viewIds).toContain(COWORK_VIEW_ID);
    expect(receipt.widgetTypeIds).toEqual([]);
  });

  it("exposes the workspace as a single-surface view with three regions", () => {
    const registry = new ContributionRegistry();
    registry.registerApp(COWORK_APP_CONTRIBUTION, [], [COWORK_VIEW_MODULE], {
      trust: "native",
    });
    const view = registry.requireView(COWORK_VIEW_ID);
    expect(view.definition.layoutKind).toBe("single-surface");
    expect(view.definition.route).toBe("cowork");
    expect(view.definition.surface?.regions).toHaveLength(3);
  });

  it("loads a module exporting both the coarse runtime and the surface renderer", async () => {
    const registry = new ContributionRegistry();
    registry.registerApp(COWORK_APP_CONTRIBUTION, [], [COWORK_VIEW_MODULE], {
      trust: "native",
    });
    const loaded = await registry.loadViewModule(COWORK_VIEW_ID);
    expect(loaded.createRuntime).toBeTypeOf("function");
    expect(loaded.surface).toBeTypeOf("function");
  });
});
