import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../../theme/ThemeProvider";
import { DashboardTestRuntime } from "../../test/DashboardTestRuntime";
import { DashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import type {
  AppContribution,
  DashboardIntent,
  WidgetDefinition,
  WidgetModule,
  WidgetRendererProps,
  WidgetSnapshot,
  WidgetTypeId,
} from "../contributions/contracts";
import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetSlotId,
  asWidgetTypeId,
} from "../contributions/contracts";
import { ContributionRegistry } from "../contributions/registry";
import { DashboardEventProvider } from "../events/DashboardEventProvider";
import { InMemoryPersonalizationRepository } from "../personalization/repository";
import type { ViewProvider } from "../providers/ViewProvider";
import { ViewHost } from "./ViewHost";

const appId = asAppId("example.catalog-provider");
const viewId = asViewId("example.catalog-provider.main");
const roleId = asWidgetRoleId("example.catalog-provider.widget-role.input@1");
const defaultType = asWidgetTypeId("example.catalog-provider.default-input");
const addableType = asWidgetTypeId("example.catalog-provider.multiple-input");
const unavailableType = asWidgetTypeId("example.catalog-provider.unavailable-input");
const unsupportedType = asWidgetTypeId("example.catalog-provider.unsupported-input");
const defaultInstanceId = asWidgetInstanceId("default:input");

const definition = (
  typeId: WidgetTypeId,
  displayName: string,
  multiplicity: WidgetDefinition["multiplicity"],
): WidgetDefinition => ({
  typeId,
  definitionVersion: 1,
  publisherAppId: appId,
  displayName,
  description: `${displayName} for catalog tests`,
  libraryPath: ["Inputs", displayName],
  providesRoles: [roleId],
  settingsSchema: { schemaId: `${typeId}.settings`, version: 1 },
  inputSchema: { schemaId: `${typeId}.input`, version: 1 },
  outputIntentSchemas: [],
  sizeContract: {
    default: { w: 8, h: 4 },
    min: { w: 6, h: 3 },
    modes: ["compact", "standard"],
  },
  multiplicity,
  rendererModuleId: asWidgetModuleId(`${typeId}.renderer`),
  theme: {
    contractVersion: 1,
    conformance: "standard",
    supports: ["light", "dark", "forced-colors", "reduced-motion"],
    styling: "host-primitives",
  },
});

const definitions = [
  definition(defaultType, "Default Input", "single_per_view"),
  definition(addableType, "Multiple Input", "multiple_per_view"),
  definition(unavailableType, "Unavailable Input", "multiple_per_view"),
  definition(unsupportedType, "Unsupported Input", "multiple_per_view"),
];

const contribution: AppContribution = {
  schemaVersion: 1,
  appId,
  definitionVersion: 1,
  displayName: "Catalog Provider",
  widgetRoles: [
    {
      roleId,
      ownerAppId: appId,
      displayName: "Input",
      description: "A provider-bound input",
    },
  ],
  widgetDefinitions: definitions,
  views: [
    {
      viewId,
      definitionVersion: 1,
      ownerAppId: appId,
      displayName: "Catalog Test",
      route: "catalog-test",
      navigation: { label: "Catalog Test", order: 1 },
      primaryJob: "Prove provider-bound catalog additions",
      grid: { columns: 24 },
      defaultSlots: [
        {
          slotId: asWidgetSlotId("input"),
          defaultInstanceId,
          requiredRole: roleId,
          defaultWidgetTypeId: defaultType,
          presence: "required",
          help: { summary: "Provide test input.", details: "Keeps the test view usable." },
          defaultSettings: {},
          defaultLayout: { x: 0, y: 0, w: 8, h: 4 },
          lockedReason: "The test view requires one input to remain usable",
        },
      ],
      readingOrder: [asWidgetSlotId("input")],
      mobileOrder: [asWidgetSlotId("input")],
    },
  ],
};

const moduleFor = (widget: WidgetDefinition): WidgetModule => ({
  moduleId: widget.rendererModuleId,
  widgetTypeId: widget.typeId,
  load: async () => ({
    default: ({ input }: WidgetRendererProps) => (
      <input
        aria-label={`${widget.displayName} value`}
        readOnly
        value={(input as { value: string }).value}
      />
    ),
  }),
});

const registry = new ContributionRegistry();
registry.registerApp(contribution, definitions.map(moduleFor));

const media = (): MediaQueryList =>
  ({
    matches: false,
    media: "",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  }) as unknown as MediaQueryList;

afterEach(() => vi.unstubAllGlobals());

describe("ViewHost provider-bound catalog additions", () => {
  it("preflights, renders, and refreshes a real personal instance without exposing unsupported types", async () => {
    vi.stubGlobal("matchMedia", vi.fn(media));
    const loadWidget = vi.fn(
      async (widgetTypeId: WidgetTypeId, request: { instanceId: string }) => {
        const base = {
          widgetTypeId,
          instanceId: asWidgetInstanceId(request.instanceId),
          revision: "r1",
          observedAt: "2026-07-12T12:00:00Z",
          quality: { kind: "complete" as const },
        };
        if (widgetTypeId === defaultType) {
          return {
            ...base,
            status: "stale" as const,
            quality: { kind: "partial" as const, message: "Default input is behind" },
            input: { value: "stale provider value" },
          } satisfies WidgetSnapshot;
        }
        if (widgetTypeId === unavailableType) {
          return {
            ...base,
            status: "unavailable" as const,
            quality: { kind: "partial" as const, message: "No binding can be created" },
            input: null,
          } satisfies WidgetSnapshot;
        }
        return {
          ...base,
          status: "ready" as const,
          input: { value: `provider-bound:${request.instanceId}` },
        } satisfies WidgetSnapshot;
      },
    );
    const provider = {
      appId,
      getAddableWidgetTypeIds: () => [addableType, unavailableType],
      loadView: async () => ({
        viewId,
        revision: "r1",
        observedAt: "2026-07-12T12:00:00Z",
        status: "ready" as const,
        quality: { kind: "complete" as const },
        model: {},
        bindings: {},
        widgetInputs: {},
      }),
      loadWidget,
      dispatch: async (intent: DashboardIntent) => ({
        intent_id: intent.intent_id,
        status: "unavailable" as const,
      }),
      reconcile: async () => ({ changed: false }),
    } satisfies ViewProvider;
    const user = userEvent.setup();

    render(
      <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
        <DashboardEventProvider>
          <DashboardAnnouncer>
            <DashboardTestRuntime>
              <ViewHost
                registry={registry}
                definition={contribution.views[0]!}
                provider={provider}
                personalizationRepository={new InMemoryPersonalizationRepository()}
              />
            </DashboardTestRuntime>
          </DashboardAnnouncer>
        </DashboardEventProvider>
      </ThemeProvider>,
    );

    expect(
      await screen.findByDisplayValue("stale provider value", undefined, { timeout: 5_000 }),
    ).toBeVisible();
    expect(screen.getByText("Default input is behind")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Widgets" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Customize view" }));
    await user.click(screen.getByRole("button", { name: "Widgets" }));

    expect(screen.queryByRole("button", { name: "Add Unsupported Input" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Shown (1)" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add Unavailable Input" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("No binding can be created"),
    );
    expect(screen.getByRole("heading", { name: "Shown (1)" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Add Multiple Input" }));
    expect(await screen.findByRole("heading", { name: "Shown (2)" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Close Widgets drawer" }));
    const added = await screen.findByRole("textbox", { name: "Multiple Input value" });
    expect((added as HTMLInputElement).value).toMatch(/^provider-bound:personal:/);

    const personalCalls = loadWidget.mock.calls.filter(
      ([widgetTypeId, request]) =>
        widgetTypeId === addableType && request.instanceId.startsWith("personal:"),
    );
    expect(personalCalls.length).toBeGreaterThan(0);
    expect(personalCalls[0]![1].instanceId).toContain("personal:");
  });
});
