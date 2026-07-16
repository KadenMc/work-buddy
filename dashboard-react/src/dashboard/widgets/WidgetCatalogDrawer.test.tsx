import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import type {
  AppContribution,
  WidgetDefinition,
  WidgetModule,
  WidgetRoleId,
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
import type { EffectiveWidgetInstance } from "../personalization/contracts";
import { WidgetCatalogDrawer } from "./WidgetCatalogDrawer";

const appId = asAppId("example.drawer");
const summaryRole = asWidgetRoleId("example.widget-role.summary@1");
const notesRole = asWidgetRoleId("example.widget-role.notes@1");
const currentType = asWidgetTypeId("example.drawer.current");
const notesType = asWidgetTypeId("example.drawer.notes");
const compatibleType = asWidgetTypeId("example.drawer.quick-summary");
const incompatibleType = asWidgetTypeId("example.drawer.unrelated");

const makeDefinition = (
  typeId: WidgetTypeId,
  role: WidgetRoleId,
  path: readonly string[],
  multiplicity: WidgetDefinition["multiplicity"] = "single_per_view",
): WidgetDefinition => ({
  typeId,
  definitionVersion: 1,
  publisherAppId: appId,
  displayName: path[path.length - 1]!,
  description: `A ${path.join(" ")} widget`,
  libraryPath: path,
  providesRoles: [role],
  settingsSchema: { schemaId: `${typeId}.settings`, version: 1 },
  inputSchema: { schemaId: `${typeId}.input`, version: 1 },
  outputIntentSchemas: [],
  sizeContract: {
    default: { w: 8, h: 4 },
    min: { w: 6, h: 3 },
    max: { w: 12, h: 8 },
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

const setup = () => {
  const definitions = [
    makeDefinition(currentType, summaryRole, ["Summary", "Current"]),
    makeDefinition(notesType, notesRole, ["Notes", "Running Notes"]),
    makeDefinition(
      compatibleType,
      summaryRole,
      ["Capture", "Quick Summary"],
      "multiple_per_view",
    ),
    makeDefinition(incompatibleType, notesRole, ["Other", "Unrelated"]),
  ];
  const contribution: AppContribution = {
    schemaVersion: 1,
    appId,
    definitionVersion: 1,
    displayName: "Drawer Publisher",
    widgetRoles: [
      {
        roleId: summaryRole,
        ownerAppId: appId,
        displayName: "Summary",
        description: "Summary role",
      },
      {
        roleId: notesRole,
        ownerAppId: appId,
        displayName: "Notes",
        description: "Notes role",
      },
    ],
    widgetDefinitions: definitions,
    views: [
      {
        viewId: asViewId("example.drawer.main"),
        definitionVersion: 1,
        ownerAppId: appId,
        displayName: "Drawer",
        route: "drawer",
        navigation: { label: "Drawer", order: 1 },
        primaryJob: "Test widget discovery",
        grid: { columns: 24 },
        defaultSlots: [
          {
            slotId: asWidgetSlotId("summary"),
            defaultInstanceId: asWidgetInstanceId("default:summary"),
            requiredRole: summaryRole,
            defaultWidgetTypeId: currentType,
            presence: "required",
            defaultSettings: {},
            defaultLayout: { x: 0, y: 0, w: 8, h: 4 },
            lockedReason: "Summary is required",
          },
          {
            slotId: asWidgetSlotId("notes"),
            defaultInstanceId: asWidgetInstanceId("default:notes"),
            requiredRole: notesRole,
            defaultWidgetTypeId: notesType,
            presence: "default_on",
            defaultSettings: {},
            defaultLayout: { x: 8, y: 0, w: 8, h: 4 },
          },
        ],
        readingOrder: [asWidgetSlotId("summary"), asWidgetSlotId("notes")],
        mobileOrder: [asWidgetSlotId("summary"), asWidgetSlotId("notes")],
      },
    ],
  };
  const modules: WidgetModule[] = definitions.map((widget) => ({
    moduleId: widget.rendererModuleId,
    widgetTypeId: widget.typeId,
    load: async () => ({ default: () => null }),
  }));
  const registry = new ContributionRegistry();
  registry.registerApp(contribution, modules);
  const instance = (
    instanceId: string,
    typeId: WidgetTypeId,
    visibility: "shown" | "hidden",
    options: Partial<EffectiveWidgetInstance> = {},
  ): EffectiveWidgetInstance => ({
    instanceId: asWidgetInstanceId(instanceId),
    widgetTypeId: typeId,
    widgetDefinitionVersion: 1,
    settings: {},
    settingsSchemaVersion: 1,
    bindings: {},
    bindingVersion: 1,
    visibility,
    presence: "personal",
    layout: { instanceId: asWidgetInstanceId(instanceId), x: 0, y: 0, w: 8, h: 4 },
    ...options,
  });
  const instances = [
    instance("default:summary", currentType, "shown", {
      slotId: asWidgetSlotId("summary"),
      presence: "required",
      roleCompatibilityVersion: summaryRole,
    }),
    instance("default:notes", notesType, "hidden", {
      slotId: asWidgetSlotId("notes"),
      presence: "default_on",
      roleCompatibilityVersion: notesRole,
    }),
    instance("wi_orphan", asWidgetTypeId("missing.publisher.summary"), "hidden", {
      roleCompatibilityVersion: summaryRole,
      unavailableReason: "Publisher App was uninstalled",
    }),
  ];
  return { registry, view: contribution.views[0]!, instances };
};

describe("WidgetCatalogDrawer", () => {
  it("renders accessible shown, hidden, unavailable, and hierarchical available sections", async () => {
    const { registry, view, instances } = setup();
    const rendered = render(
      <WidgetCatalogDrawer
        registry={registry}
        view={view}
        instances={instances}
        addableWidgetTypeIds={[compatibleType]}
        getPublisherPresentation={(widget) => ({
          label: widget.app.displayName,
          appId: widget.app.appId,
          trust: "native",
        })}
        onAction={vi.fn()}
        onAddRequested={vi.fn()}
        onReplaceRequested={vi.fn()}
        onRecoverRequested={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Shown (1)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Hidden (1)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Unavailable (1)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Capture" })).toBeInTheDocument();
    expect(screen.getAllByText(/Drawer Publisher/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Trust: native/).length).toBeGreaterThan(0);
    await expectNoAccessibilityViolations(rendered.container);
  });

  it("emits undo-friendly actions and only role-compatible replacement choices", async () => {
    const user = userEvent.setup();
    const { registry, view, instances } = setup();
    const onAction = vi.fn();
    const onAdd = vi.fn();
    const onReplace = vi.fn();
    const onRecover = vi.fn();
    const onClose = vi.fn();
    const { container } = render(
      <WidgetCatalogDrawer
        registry={registry}
        view={view}
        instances={instances}
        addableWidgetTypeIds={[compatibleType]}
        onAction={onAction}
        onAddRequested={onAdd}
        onReplaceRequested={onReplace}
        onRecoverRequested={onRecover}
        onClose={onClose}
      />,
    );

    expect(screen.getByRole("button", { name: "Close Widgets drawer" })).toHaveFocus();
    const dialog = screen.getByRole("dialog");
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog).toHaveAttribute("open");

    const required = container.querySelector(
      '[data-instance-id="default:summary"]',
    ) as HTMLElement;
    expect(within(required).getByRole("button", { name: "Hide" })).toBeDisabled();
    expect(
      within(required).getByRole("button", { name: "Replace with Quick Summary" }),
    ).toBeInTheDocument();
    expect(within(required).queryByText(/Unrelated/)).not.toBeInTheDocument();

    const hidden = container.querySelector(
      '[data-instance-id="default:notes"]',
    ) as HTMLElement;
    await user.click(within(hidden).getByRole("button", { name: "Show" }));
    expect(onAction).toHaveBeenCalledWith({
      type: "show",
      instanceId: asWidgetInstanceId("default:notes"),
    });

    await user.click(within(required).getByRole("button", { name: "Replace with Quick Summary" }));
    expect(onReplace).toHaveBeenCalledWith(
      instances[0],
      expect.objectContaining({ definition: expect.objectContaining({ typeId: compatibleType }) }),
    );

    await user.click(screen.getByRole("button", { name: "Add Quick Summary" }));
    expect(onAdd).toHaveBeenCalledWith(
      expect.objectContaining({ typeId: compatibleType }),
    );

    const orphan = container.querySelector(
      '[data-instance-id="wi_orphan"]',
    ) as HTMLElement;
    await user.click(within(orphan).getByRole("button", { name: "Find replacement" }));
    expect(onRecover).toHaveBeenCalledWith(instances[2]);

    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not advertise installed renderers the provider did not declare addable", () => {
    const { registry, view, instances } = setup();
    render(
      <WidgetCatalogDrawer
        registry={registry}
        view={view}
        instances={instances}
        addableWidgetTypeIds={[]}
        onAction={vi.fn()}
        onAddRequested={vi.fn()}
        onReplaceRequested={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Available (0)" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add Quick Summary" })).not.toBeInTheDocument();
  });

  it("returns focus to the opener when the native modal is dismissed", async () => {
    const user = userEvent.setup();
    const { registry, view, instances } = setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open widget catalog</button>
          {open ? (
            <WidgetCatalogDrawer
              registry={registry}
              view={view}
              instances={instances}
              addableWidgetTypeIds={[compatibleType]}
              onAction={vi.fn()}
              onAddRequested={vi.fn()}
              onReplaceRequested={vi.fn()}
              onClose={() => setOpen(false)}
            />
          ) : null}
        </>
      );
    }
    render(<Harness />);

    const opener = screen.getByRole("button", { name: "Open widget catalog" });
    await user.click(opener);
    const dialog = screen.getByRole("dialog");
    expect(screen.getByRole("button", { name: "Close Widgets drawer" })).toHaveFocus();
    fireEvent(dialog, new Event("cancel", { cancelable: true }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
