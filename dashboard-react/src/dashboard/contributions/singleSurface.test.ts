import { describe, expect, it } from "vitest";

import type { AppContribution, ViewDefinition } from "./contracts";
import {
  asAppId,
  asViewId,
  asWidgetRoleId,
} from "./contracts";
import type { WidgetThemeDeclaration } from "./themeContract";
import { validateAppContribution } from "./validate";

const appId = asAppId("example.surface");
const editorRole = asWidgetRoleId("example.widget-role.editor@1");
const railRole = asWidgetRoleId("example.widget-role.rail@1");

const STANDARD_THEME: WidgetThemeDeclaration = {
  contractVersion: 1,
  conformance: "standard",
  supports: ["light", "dark", "forced-colors", "reduced-motion"],
  styling: "semantic-tokens",
};

const singleSurfaceView = (): ViewDefinition => ({
  viewId: asViewId("example.surface.workspace"),
  definitionVersion: 1,
  ownerAppId: appId,
  displayName: "Surface",
  route: "surface",
  navigation: { label: "Surface", order: 20 },
  primaryJob: "Compose one App-owned surface.",
  layoutKind: "single-surface",
  grid: { columns: 24 },
  defaultSlots: [],
  readingOrder: [],
  mobileOrder: [],
  surface: {
    regions: [
      {
        regionId: "editor",
        role: editorRole,
        presence: "required",
        help: { summary: "Edit the document.", details: "The editor region." },
        theme: STANDARD_THEME,
      },
      {
        regionId: "rail",
        role: railRole,
        presence: "required",
        help: { summary: "Review proposals.", details: "The review rail region." },
        theme: STANDARD_THEME,
      },
    ],
  },
});

const contribution = (view: ViewDefinition = singleSurfaceView()): AppContribution => ({
  schemaVersion: 1,
  appId,
  definitionVersion: 1,
  displayName: "Surface",
  widgetRoles: [
    {
      roleId: editorRole,
      ownerAppId: appId,
      displayName: "Editor",
      description: "Owns the document surface.",
    },
    {
      roleId: railRole,
      ownerAppId: appId,
      displayName: "Rail",
      description: "Owns the review rail.",
    },
  ],
  widgetDefinitions: [],
  views: [view],
});

const codes = (view: ViewDefinition): string[] =>
  validateAppContribution(contribution(view)).map((issue) => issue.code);

describe("single-surface view validation", () => {
  it("accepts a valid single-surface contribution", () => {
    expect(validateAppContribution(contribution())).toEqual([]);
  });

  it("keeps every identity, route, and theme safety invariant", () => {
    const view = {
      ...singleSurfaceView(),
      viewId: asViewId("NotNamespaced"),
      route: "bad/route",
      surface: {
        regions: [
          {
            regionId: "Editor",
            role: asWidgetRoleId("example.widget-role.editor@1"),
            presence: "required" as const,
            help: { summary: "", details: "" },
            theme: {
              contractVersion: 1,
              conformance: "standard",
              supports: ["light"],
              styling: "semantic-tokens",
            } as WidgetThemeDeclaration,
          },
        ],
      },
    } as ViewDefinition;
    const found = codes(view);
    expect(found).toEqual(
      expect.arrayContaining([
        "invalid_namespaced_id",
        "invalid_view_route",
        "invalid_region_id",
        "missing_help_summary",
        "missing_theme_support",
      ]),
    );
  });

  it("requires a surface composition", () => {
    const view = { ...singleSurfaceView(), surface: undefined } as ViewDefinition;
    expect(codes(view)).toContain("missing_surface_composition");
  });

  it("rejects grid slots or orders on a single-surface view", () => {
    const base = singleSurfaceView();
    const view = {
      ...base,
      readingOrder: ["editor"],
      mobileOrder: ["editor"],
    } as unknown as ViewDefinition;
    expect(codes(view)).toContain("single_surface_has_grid_order");
  });

  it("rejects a region role that is not registered", () => {
    const base = singleSurfaceView();
    const view: ViewDefinition = {
      ...base,
      surface: {
        regions: [
          {
            ...base.surface!.regions[0]!,
            role: asWidgetRoleId("example.widget-role.unknown@1"),
          },
          base.surface!.regions[1]!,
        ],
      },
    };
    expect(codes(view)).toContain("unknown_region_role");
  });

  it("rejects two regions filling the same role", () => {
    const base = singleSurfaceView();
    const view: ViewDefinition = {
      ...base,
      surface: {
        regions: [
          base.surface!.regions[0]!,
          { ...base.surface!.regions[1]!, role: editorRole },
        ],
      },
    };
    expect(codes(view)).toContain("duplicate_region_role");
  });

  it("rejects an unknown layout kind", () => {
    const view = {
      ...singleSurfaceView(),
      layoutKind: "free-form",
    } as unknown as ViewDefinition;
    expect(codes(view)).toContain("invalid_layout_kind");
  });
});

describe("standard-grid conformance is untouched", () => {
  const standardView = (): ViewDefinition => ({
    viewId: asViewId("example.surface.grid"),
    definitionVersion: 1,
    ownerAppId: appId,
    displayName: "Grid",
    route: "grid",
    navigation: { label: "Grid", order: 21 },
    primaryJob: "A plain grid view.",
    grid: { columns: 24 },
    defaultSlots: [],
    readingOrder: [],
    mobileOrder: [],
  });

  it("still validates a standard grid view with no layoutKind", () => {
    expect(validateAppContribution(contribution(standardView()))).toEqual([]);
  });

  it("rejects a surface composition on a standard grid view", () => {
    const view: ViewDefinition = {
      ...standardView(),
      surface: { regions: [] },
    };
    expect(codes(view)).toContain("surface_on_standard_grid");
  });

  it("still enforces the 24-column grid on standard views", () => {
    const view: ViewDefinition = { ...standardView(), grid: { columns: 12 } };
    expect(codes(view)).toContain("invalid_grid_columns");
  });
});
