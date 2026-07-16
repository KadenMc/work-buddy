import { describe, expect, it } from "vitest";

import type { ViewDefinition, WidgetDefinition } from "../contributions/contracts";
import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
} from "../contributions/contracts";
import type { EffectiveWidgetInstance, ViewEditSessionState } from "./contracts";
import {
  beginViewEditSession,
  createPersonalizationPatch,
  resolveViewPersonalization,
  viewEditSessionReducer,
} from "./reducer";

const appId = asAppId("example.layout");
const roleId = asWidgetRoleId("example.widget-role.card@1");
const primaryType = asWidgetTypeId("example.layout.card");
const replacementType = asWidgetTypeId("example.layout.alternate-card");

const widget = (typeId: typeof primaryType | typeof replacementType): WidgetDefinition => ({
  typeId,
  definitionVersion: 1,
  publisherAppId: appId,
  displayName: "Card",
  description: "Test card",
  libraryPath: ["Test", "Card"],
  providesRoles: [roleId],
  settingsSchema: { schemaId: `${typeId}.settings`, version: 1 },
  inputSchema: { schemaId: `${typeId}.input`, version: 1 },
  outputIntentSchemas: [],
  sizeContract: {
    default: { w: 6, h: 4 },
    min: { w: 4, h: 3 },
    max: { w: 12, h: 10 },
    modes: ["compact", "standard", "expanded"],
  },
  multiplicity: "multiple_per_view",
  rendererModuleId: asWidgetModuleId(`${typeId}.renderer`),
  theme: {
    contractVersion: 1,
    conformance: "standard",
    supports: ["light", "dark", "forced-colors", "reduced-motion"],
    styling: "semantic-tokens",
  },
});

const definitions = new Map([
  [primaryType, widget(primaryType)],
  [replacementType, widget(replacementType)],
]);

const view = (definitionVersion = 1, density = "comfortable"): ViewDefinition => ({
  viewId: asViewId("example.layout.main"),
  definitionVersion,
  ownerAppId: appId,
  displayName: "Layout",
  route: "layout",
  navigation: { label: "Layout", order: 1 },
  primaryJob: "Exercise personalization",
  grid: { columns: 24 },
  defaultSlots: [
    {
      slotId: asWidgetSlotId("primary"),
      defaultInstanceId: asWidgetInstanceId("default:primary"),
      requiredRole: roleId,
      defaultWidgetTypeId: primaryType,
      presence: "required",
      help: { summary: "Show the primary item.", details: "Keeps the test view's primary purpose available." },
      defaultSettings: { density },
      defaultLayout: { x: 0, y: 0, w: 6, h: 4 },
      lockedReason: "The primary purpose must remain present.",
    },
    {
      slotId: asWidgetSlotId("notes"),
      defaultInstanceId: asWidgetInstanceId("default:notes"),
      requiredRole: roleId,
      defaultWidgetTypeId: primaryType,
      presence: "default_on",
      help: { summary: "Show supporting notes.", details: "Adds optional notes to this test view." },
      defaultSettings: { density, filter: "all" },
      defaultLayout: { x: 8, y: 0, w: 6, h: 4 },
    },
  ],
  readingOrder: [asWidgetSlotId("primary"), asWidgetSlotId("notes")],
  mobileOrder: [asWidgetSlotId("primary"), asWidgetSlotId("notes")],
});

const reduce = (state: ViewEditSessionState, ...actions: Parameters<typeof viewEditSessionReducer>[1][]) =>
  actions.reduce(viewEditSessionReducer, state);

const personalInstance = (): EffectiveWidgetInstance => ({
  instanceId: asWidgetInstanceId("wi_personal"),
  widgetTypeId: primaryType,
  widgetDefinitionVersion: 1,
  roleCompatibilityVersion: roleId,
  settings: {},
  settingsSchemaVersion: 1,
  bindings: {},
  bindingVersion: 1,
  visibility: "shown",
  presence: "personal",
  layout: {
    instanceId: asWidgetInstanceId("wi_personal"),
    x: 8,
    y: 0,
    w: 6,
    h: 4,
    minW: 4,
    minH: 3,
    maxW: 12,
    maxH: 10,
  },
});

describe("view edit session reducer", () => {
  it("previews pointer frames without history and checkpoints once on interaction end", () => {
    const opening = resolveViewPersonalization(view(), definitions);
    const preview = opening.instances.map((instance) =>
      instance.instanceId === "default:primary"
        ? { ...instance.layout, x: 0, y: 5 }
        : instance.layout,
    );
    let state = reduce(
      beginViewEditSession(opening),
      { type: "begin-interaction" },
      { type: "preview-layout", layout: preview },
    );
    expect(state.past).toEqual([]);
    expect(state.present.instances[0]?.layout.y).toBe(5);

    state = viewEditSessionReducer(state, { type: "commit-interaction" });
    expect(state.past).toHaveLength(1);
    state = viewEditSessionReducer(state, { type: "undo" });
    expect(state.present).toEqual(opening);
    state = viewEditSessionReducer(state, { type: "redo" });
    expect(state.present.instances[0]?.layout.y).toBe(5);
  });

  it("protects required presence and restores optional widgets without moving siblings", () => {
    const opening = resolveViewPersonalization(view(), definitions);
    let state = viewEditSessionReducer(beginViewEditSession(opening), {
      type: "hide",
      instanceId: asWidgetInstanceId("default:primary"),
    });
    expect(state.lastFailure).toMatch(/required/);

    state = reduce(
      beginViewEditSession(opening),
      { type: "hide", instanceId: asWidgetInstanceId("default:notes") },
      { type: "add", instance: personalInstance() },
      { type: "show", instanceId: asWidgetInstanceId("default:notes") },
    );
    const primary = state.present.instances.find((instance) => instance.instanceId === "default:primary")!;
    const notes = state.present.instances.find((instance) => instance.instanceId === "default:notes")!;
    expect(primary.layout).toEqual(opening.instances[0]?.layout);
    expect(notes).toMatchObject({ visibility: "shown", layout: { x: 14, y: 0 } });
  });

  it("replaces a compatible type atomically while preserving slot, instance, and layout", () => {
    const opening = resolveViewPersonalization(view(), definitions);
    const action = {
      type: "replace-widget" as const,
      instanceId: asWidgetInstanceId("default:notes"),
      replacement: {
        widgetTypeId: replacementType,
        widgetDefinitionVersion: 1,
        roleCompatibilityVersion: roleId,
      },
      settings: { appearance: "alternate" },
      settingsSchemaVersion: 1,
      bindings: {},
      bindingVersion: 1,
      roleCompatible: false,
    };
    let state = viewEditSessionReducer(beginViewEditSession(opening), action);
    expect(state.present).toEqual(opening);

    state = viewEditSessionReducer(beginViewEditSession(opening), {
      ...action,
      roleCompatible: true,
    });
    const replaced = state.present.instances[1]!;
    expect(replaced).toMatchObject({
      instanceId: "default:notes",
      slotId: "notes",
      widgetTypeId: replacementType,
      layout: opening.instances[1]?.layout,
    });
  });

  it("Cancel restores the opening state while Reset is undoable and Done commits", () => {
    const opening = resolveViewPersonalization(view(), definitions);
    let state = viewEditSessionReducer(beginViewEditSession(opening), {
      type: "hide",
      instanceId: asWidgetInstanceId("default:notes"),
    });
    expect(state.dirty).toBe(true);
    expect(viewEditSessionReducer(state, { type: "cancel" })).toMatchObject({
      present: opening,
      status: "cancelled",
      dirty: false,
    });

    state = viewEditSessionReducer(state, { type: "reset", defaults: opening });
    expect(state.present).toEqual(opening);
    state = viewEditSessionReducer(state, { type: "undo" });
    expect(state.present.instances[1]?.visibility).toBe("hidden");
    expect(viewEditSessionReducer(state, { type: "done" })).toMatchObject({
      status: "done",
      dirty: false,
    });
  });
});

describe("portable personalization patch", () => {
  it("stores only deltas and merges them over upgraded App defaults", () => {
    const original = resolveViewPersonalization(view(1, "comfortable"), definitions);
    let state = viewEditSessionReducer(beginViewEditSession(original), {
      type: "configure",
      instanceId: asWidgetInstanceId("default:notes"),
      settings: { density: "comfortable", filter: "open" },
      settingsSchemaVersion: 1,
    });
    state = viewEditSessionReducer(state, {
      type: "layout-command",
      command: {
        kind: "move",
        instanceId: asWidgetInstanceId("default:notes"),
        direction: "down",
        amount: 5,
      },
    });
    const patch = createPersonalizationPatch(view(1, "comfortable"), definitions, state.present);
    expect(patch.defaultSlotOverrides.notes).toMatchObject({
      slotId: "notes",
      settingsPatch: { filter: "open" },
      layout: { y: 5 },
    });
    expect(JSON.stringify(patch)).not.toContain('"i"');

    const upgraded = resolveViewPersonalization(view(2, "compact"), definitions, patch);
    expect(upgraded.instances[1]).toMatchObject({
      settings: { density: "compact", filter: "open" },
      layout: { y: 5 },
    });
  });

  it("preserves opaque overrides for App slots removed by a later definition", () => {
    const original = resolveViewPersonalization(view(1), definitions);
    const customized = viewEditSessionReducer(beginViewEditSession(original), {
      type: "configure",
      instanceId: asWidgetInstanceId("default:notes"),
      settings: { density: "comfortable", filter: "private" },
      settingsSchemaVersion: 1,
    });
    const prior = createPersonalizationPatch(view(1), definitions, customized.present);
    const upgraded = {
      ...view(2),
      defaultSlots: view(2).defaultSlots.slice(0, 1),
      readingOrder: [asWidgetSlotId("primary")],
      mobileOrder: [asWidgetSlotId("primary")],
    } satisfies ViewDefinition;

    const resolved = resolveViewPersonalization(upgraded, definitions, prior);
    const resaved = createPersonalizationPatch(upgraded, definitions, resolved, prior);

    expect(resolved.instances).toHaveLength(1);
    expect(resaved.defaultSlotOverrides.notes).toEqual(prior.defaultSlotOverrides.notes);
  });
});
