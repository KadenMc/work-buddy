import { describe, expect, it } from "vitest";

import { createContributionRegistry } from "../dashboard/contributions/registry";
import {
  CAPTURE_APP_CONTRIBUTION,
  QUICK_TEXT_CAPTURE_MODULE,
  QUICK_TEXT_CAPTURE_TYPE_ID,
} from "./capture";
import {
  NOTES_APP_CONTRIBUTION,
  RUNNING_NOTES_MODULE,
  RUNNING_NOTES_TYPE_ID,
} from "./notes";
import {
  DAY_TIMELINE_MODULE,
  DAY_TIMELINE_TYPE_ID,
  TIMELINE_APP_CONTRIBUTION,
} from "./timeline";

describe("widget-library publisher contributions", () => {
  it("registers the three independent publisher Apps and lazy modules", async () => {
    const registry = createContributionRegistry();
    registry.registerApp(CAPTURE_APP_CONTRIBUTION, [QUICK_TEXT_CAPTURE_MODULE]);
    registry.registerApp(TIMELINE_APP_CONTRIBUTION, [DAY_TIMELINE_MODULE]);
    registry.registerApp(NOTES_APP_CONTRIBUTION, [RUNNING_NOTES_MODULE]);

    expect(registry.listApps()).toHaveLength(3);
    expect(registry.requireWidget(QUICK_TEXT_CAPTURE_TYPE_ID).definition.sizeContract).toMatchObject(
      { default: { w: 8, h: 4 }, min: { w: 6, h: 3 } },
    );
    expect(registry.requireWidget(DAY_TIMELINE_TYPE_ID).definition.sizeContract).toMatchObject(
      { default: { w: 16, h: 12 }, min: { w: 12, h: 8 } },
    );
    expect(registry.requireWidget(RUNNING_NOTES_TYPE_ID).definition.sizeContract).toMatchObject(
      { default: { w: 8, h: 8 }, min: { w: 6, h: 6 } },
    );

    for (const typeId of [
      QUICK_TEXT_CAPTURE_TYPE_ID,
      DAY_TIMELINE_TYPE_ID,
      RUNNING_NOTES_TYPE_ID,
    ]) {
      const widget = registry.requireWidget(typeId).definition;
      expect(widget.theme).toMatchObject({
        contractVersion: 1,
        conformance: "standard",
        styling: "semantic-tokens",
      });
      expect(widget.theme.supports).toEqual([
        "light",
        "dark",
        "forced-colors",
        "reduced-motion",
      ]);
      const loaded = await registry.loadWidgetModule(typeId);
      expect(typeof loaded.default).toBe("function");
    }
  });
});
