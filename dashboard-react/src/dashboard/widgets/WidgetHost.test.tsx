import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentType } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { ThemeProvider } from "../../theme/ThemeProvider";
import {
  asAppId,
  asViewId,
  asWidgetInstanceId,
  asWidgetModuleId,
  asWidgetRoleId,
  asWidgetTypeId,
  type WidgetDefinition,
  type WidgetIntent,
  type WidgetModule,
  type WidgetRendererProps,
} from "../contributions/contracts";
import { WidgetHost } from "./WidgetHost";

const typeId = asWidgetTypeId("wb.test.summary");
const moduleId = asWidgetModuleId("wb.test.summary.renderer");
const instanceId = asWidgetInstanceId("instance-test-summary");
const viewId = asViewId("wb.test.main");

const definition: WidgetDefinition = {
  typeId,
  definitionVersion: 1,
  publisherAppId: asAppId("wb.test"),
  displayName: "Test summary",
  description: "A generic host test widget",
  libraryPath: ["Test", "Summary"],
  providesRoles: [asWidgetRoleId("wb.role.summary")],
  settingsSchema: { schemaId: "wb.test.settings", version: 1 },
  inputSchema: { schemaId: "wb.test.input", version: 1 },
  outputIntentSchemas: [],
  sizeContract: {
    default: { w: 4, h: 3 },
    min: { w: 2, h: 2 },
    modes: ["compact", "standard", "expanded"],
  },
  multiplicity: "multiple_per_view",
  rendererModuleId: moduleId,
  theme: {
    contractVersion: 1,
    conformance: "standard",
    supports: ["light", "dark", "forced-colors", "reduced-motion"],
    styling: "semantic-tokens",
  },
};

const intent = {
  intent_type: "wb.test.activated",
  schema_version: 1,
  intent_id: "intent-1",
  view_id: viewId,
  instance_id: instanceId,
  payload: {},
} satisfies WidgetIntent;

function createModule(
  renderer: ComponentType<WidgetRendererProps<unknown, WidgetIntent>> | unknown,
  load = vi.fn(),
): WidgetModule {
  load.mockResolvedValue({ default: renderer });
  return { moduleId, widgetTypeId: typeId, load };
}

function renderHost(
  module: WidgetModule,
  overrides: Partial<React.ComponentProps<typeof WidgetHost>> = {},
) {
  const emit = vi.fn();
  const result = render(
    <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
      <WidgetHost
        definition={definition}
        module={module}
        instanceId={instanceId}
        viewId={viewId}
        input={{ label: "Bound input" }}
        status="ready"
        width={480}
        height={320}
        sizeMode="standard"
        editing={false}
        emit={emit}
        {...overrides}
      />
    </ThemeProvider>,
  );
  return { ...result, emit };
}

describe("WidgetHost", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        media: query,
        matches: false,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
  });

  it("lazy-loads a renderer with bound input and host presentation", async () => {
    const Renderer = ({ input, emit, presentation }: WidgetRendererProps) => (
      <button type="button" onClick={() => emit(intent)}>
        {`${(input as { label: string }).label}:${presentation.sizeMode}:${presentation.theme.resolvedScheme}:${presentation.getCanvasTheme().dataSeries.length}`}
      </button>
    );
    const load = vi.fn();
    const module = createModule(Renderer, load);
    const { container, emit } = renderHost(module);

    const button = await screen.findByRole("button", {
      name: "Bound input:standard:light:8",
    });
    await userEvent.click(button);

    expect(load).toHaveBeenCalledTimes(1);
    expect(emit).toHaveBeenCalledWith(intent);
    await expectNoAccessibilityViolations(container);
  });

  it("does not load a renderer while showing a blocking host state", () => {
    const load = vi.fn();
    renderHost(createModule(() => null, load), { status: "loading" });

    expect(
      screen.getByRole("heading", { name: "Loading widget" }),
    ).toBeInTheDocument();
    expect(load).not.toHaveBeenCalled();
  });

  it("keeps stale content visible with a truthful host banner", async () => {
    renderHost(createModule(() => <p>Last known content</p>), {
      status: "stale",
    });

    expect(await screen.findByText("Last known content")).toBeInTheDocument();
    expect(screen.getByText(/May be out of date/)).toBeInTheDocument();
  });

  it("prevents required slots from being hidden or removed", async () => {
    const onHide = vi.fn();
    const onRemove = vi.fn();
    renderHost(createModule(() => null), {
      status: "empty",
      presence: "required",
      lockedReason: "Capture is required to preserve the view's primary job.",
      onHide,
      onRemove,
    });

    await userEvent.click(
      screen.getByText("Actions for Test summary", { selector: "span" }),
    );
    expect(screen.getByRole("button", { name: "Hide" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove" })).toBeDisabled();
    expect(
      screen.getByText("Capture is required to preserve the view's primary job."),
    ).toBeInTheDocument();
    expect(onHide).not.toHaveBeenCalled();
    expect(onRemove).not.toHaveBeenCalled();
  });

  it("isolates a throwing renderer without removing its frame", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const ThrowingRenderer = () => {
      throw new Error("renderer failed");
    };
    const onRetry = vi.fn();
    renderHost(createModule(ThrowingRenderer), { onRetry });

    expect(await screen.findByText("Widget could not load")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Test summary" })).toBeInTheDocument();
    await userEvent.click(
      within(screen.getByRole("alert")).getByRole("button", { name: "Retry" }),
    );
    await waitFor(() => expect(onRetry).toHaveBeenCalledTimes(1));
    consoleError.mockRestore();
  });
});
