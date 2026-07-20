import { useEffect, useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider } from "../../theme/ThemeProvider";
import { DashboardTestRuntime } from "../../test/DashboardTestRuntime";
import { DashboardAnnouncer } from "../accessibility/DashboardAnnouncer";
import { CustomizeModeProvider } from "../customize";
import { CustomizeViewToggle } from "../customize/CustomizeViewToggle";
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

const appId = asAppId("example.durable-provider");
const viewId = asViewId("example.durable-provider.main");
const roleId = asWidgetRoleId("example.durable-provider.widget-role.workspace@1");
const durableType = asWidgetTypeId("example.durable-provider.workspace");
const durableInstanceId = asWidgetInstanceId("default:workspace");

// A live durable renderer. It counts its own mounts, so any remount would push the count past
// one, and it holds React state in a controlled input, so any remount would reset the value.
let probeMountCount = 0;

function DurableProbe(_props: WidgetRendererProps) {
  const [typed, setTyped] = useState("");
  useEffect(() => {
    probeMountCount += 1;
  }, []);
  return (
    <input
      data-testid="durable-probe"
      aria-label="Durable probe field"
      value={typed}
      onChange={(event) => setTyped(event.target.value)}
    />
  );
}

const durableDefinition: WidgetDefinition = {
  typeId: durableType,
  definitionVersion: 1,
  publisherAppId: appId,
  displayName: "Durable Workspace",
  description: "A durable workspace for keep-alive tests",
  libraryPath: ["Workspaces", "Durable Workspace"],
  providesRoles: [roleId],
  settingsSchema: { schemaId: `${durableType}.settings`, version: 1 },
  inputSchema: { schemaId: `${durableType}.input`, version: 1 },
  outputIntentSchemas: [],
  sizeContract: {
    default: { w: 12, h: 10 },
    min: { w: 6, h: 4 },
    modes: ["compact", "standard"],
  },
  multiplicity: "single_per_view",
  durable: true,
  rendererModuleId: asWidgetModuleId(`${durableType}.renderer`),
  theme: {
    contractVersion: 1,
    conformance: "standard",
    supports: ["light", "dark", "forced-colors", "reduced-motion"],
    styling: "host-primitives",
  },
};

const contribution: AppContribution = {
  schemaVersion: 1,
  appId,
  definitionVersion: 1,
  displayName: "Durable Provider",
  widgetRoles: [
    {
      roleId,
      ownerAppId: appId,
      displayName: "Workspace",
      description: "A durable workspace role",
    },
  ],
  widgetDefinitions: [durableDefinition],
  views: [
    {
      viewId,
      definitionVersion: 1,
      ownerAppId: appId,
      displayName: "Durable Test",
      route: "durable-test",
      navigation: { label: "Durable Test", order: 1 },
      primaryJob: "Prove durable widgets survive a customize round-trip",
      grid: { columns: 24 },
      defaultSlots: [
        {
          slotId: asWidgetSlotId("workspace"),
          defaultInstanceId: durableInstanceId,
          requiredRole: roleId,
          defaultWidgetTypeId: durableType,
          presence: "required",
          help: {
            summary: "The durable workspace.",
            details: "Keeps its live state across grid remounts.",
          },
          defaultSettings: {},
          defaultLayout: { x: 0, y: 0, w: 12, h: 10 },
          lockedReason: "The test view requires its durable workspace",
        },
      ],
      readingOrder: [asWidgetSlotId("workspace")],
      mobileOrder: [asWidgetSlotId("workspace")],
    },
  ],
};

const workspaceModule: WidgetModule = {
  moduleId: durableDefinition.rendererModuleId,
  widgetTypeId: durableType,
  load: async () => ({ default: DurableProbe }),
};

const registry = new ContributionRegistry();
registry.registerApp(contribution, [workspaceModule]);

const provider = {
  appId,
  getAddableWidgetTypeIds: () => [],
  loadView: async () => ({
    viewId,
    revision: "r1",
    observedAt: "2026-07-20T12:00:00Z",
    status: "ready" as const,
    quality: { kind: "complete" as const },
    model: {},
    bindings: {},
    widgetInputs: {},
  }),
  loadWidget: async (widgetTypeId: WidgetTypeId, request: { instanceId: string }) =>
    ({
      widgetTypeId,
      instanceId: asWidgetInstanceId(request.instanceId),
      revision: "r1",
      observedAt: "2026-07-20T12:00:00Z",
      status: "ready" as const,
      quality: { kind: "complete" as const },
      input: { value: "durable" },
    }) satisfies WidgetSnapshot,
  dispatch: async (intent: DashboardIntent) => ({
    intent_id: intent.intent_id,
    status: "unavailable" as const,
  }),
  reconcile: async () => ({ changed: false }),
} satisfies ViewProvider;

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

describe("ViewHost durable widget keep-alive", () => {
  it("keeps one live durable element, its DOM node, and its state across a customize round-trip", async () => {
    vi.stubGlobal("matchMedia", vi.fn(media));
    const user = userEvent.setup();

    render(
      <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
        <DashboardEventProvider>
          <DashboardAnnouncer>
            <DashboardTestRuntime>
              <CustomizeModeProvider>
                <CustomizeViewToggle />
                <ViewHost
                  registry={registry}
                  definition={contribution.views[0]!}
                  provider={provider}
                  personalizationRepository={new InMemoryPersonalizationRepository()}
                />
              </CustomizeModeProvider>
            </DashboardTestRuntime>
          </DashboardAnnouncer>
        </DashboardEventProvider>
      </ThemeProvider>,
    );

    // The live durable element mounts exactly once, inside the grid.
    const probe = (await screen.findByTestId("durable-probe", undefined, {
      timeout: 5_000,
    })) as HTMLInputElement & { __durableTag?: string };
    expect(probeMountCount).toBe(1);
    probe.__durableTag = "kept";
    await user.type(probe, "hello");
    expect(probe.value).toBe("hello");

    // Begin customize through the navbar control. The grid below remounts to clear its private
    // drag state, but the durable element is portaled above the grid and must not be torn down.
    const customizeToggle = screen.getByRole("button", { name: "Customize view" });
    await waitFor(() => expect(customizeToggle).toBeEnabled());
    await user.click(customizeToggle);
    expect(screen.getByText("Arranging layout")).toBeVisible();

    // The durable frame stays live during arrange. It is never frozen with the inert content and
    // shield the ordinary widgets get, because a durable widget owns interactive state by design.
    const frame = screen.getByTestId("durable-probe").closest(".wb-widget-frame");
    expect(frame).not.toBeNull();
    expect(frame).toHaveAttribute("data-widget-interaction-mode", "operate");
    expect(frame!.querySelector(".wb-widget-frame__content")).not.toHaveAttribute("inert");
    expect(frame!.querySelector(".wb-widget-frame__interaction-shield")).toBeNull();

    // Cancel customize. The grid remounts once more on the way out.
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.queryByText("Arranging layout")).not.toBeInTheDocument(),
    );

    // Same DOM node, same expando, same React state, and still a single mount throughout.
    const after = screen.getByTestId("durable-probe") as HTMLInputElement & {
      __durableTag?: string;
    };
    expect(after).toBe(probe);
    expect(after.__durableTag).toBe("kept");
    expect(after.value).toBe("hello");
    expect(probeMountCount).toBe(1);
  }, 15_000);

  it("offers Arrange only on an all-durable view, hiding Preview interactions", async () => {
    vi.stubGlobal("matchMedia", vi.fn(media));
    const user = userEvent.setup();

    render(
      <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
        <DashboardEventProvider>
          <DashboardAnnouncer>
            <DashboardTestRuntime>
              <CustomizeModeProvider>
                <CustomizeViewToggle />
                <ViewHost
                  registry={registry}
                  definition={contribution.views[0]!}
                  provider={provider}
                  personalizationRepository={new InMemoryPersonalizationRepository()}
                />
              </CustomizeModeProvider>
            </DashboardTestRuntime>
          </DashboardAnnouncer>
        </DashboardEventProvider>
      </ThemeProvider>,
    );

    await screen.findByTestId("durable-probe", undefined, { timeout: 5_000 });

    const customizeToggle = screen.getByRole("button", { name: "Customize view" });
    await waitFor(() => expect(customizeToggle).toBeEnabled());
    await user.click(customizeToggle);

    // The one visible widget is durable, so Preview has nothing to sandbox. The view is in
    // Arrange and offers no way into Preview, while the other arrange controls stay available.
    expect(screen.getByText("Arranging layout")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Preview interactions" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Widgets/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Mobile order/ })).toBeVisible();
  }, 15_000);
});
