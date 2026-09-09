import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardAnnouncer } from "../../dashboard/accessibility/DashboardAnnouncer";
import type { DashboardIntent, WidgetModule, WidgetRendererProps } from "../../dashboard/contributions/contracts";
import { ContributionRegistry } from "../../dashboard/contributions/registry";
import { CustomizeModeProvider } from "../../dashboard/customize";
import { CustomizeViewToggle } from "../../dashboard/customize/CustomizeViewToggle";
import { DashboardEventProvider } from "../../dashboard/events/DashboardEventProvider";
import { LocalStoragePersonalizationRepository } from "../../dashboard/personalization/repository";
import type { ViewProvider } from "../../dashboard/providers/ViewProvider";
import { ViewHost } from "../../dashboard/views/ViewHost";
import { DashboardTestRuntime } from "../../test/DashboardTestRuntime";
import { ThemeProvider } from "../../theme/ThemeProvider";
import { TASKS_APP_ID, TASKS_INSTANCE_IDS, TASKS_SLOT_IDS, TASKS_VIEW_ID } from "./bindings";
import { TASKS_APP_CONTRIBUTION } from "./contribution";
import { TASKS_VIEW_DEFINITION } from "./viewDefinition";

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

function WorkspaceProbe({ presentation }: WidgetRendererProps) {
  const [confirming, setConfirming] = useState(false);
  return <><output aria-label="Task Workspace mode">{presentation.interactionMode}</output>
    <button onClick={() => setConfirming(true)}>Review task action</button>
    {confirming ? <div role="alertdialog" aria-label="Review task action"><button onClick={() => setConfirming(false)}>Cancel task action</button></div> : null}
  </>;
}

describe("Tasks view customization", () => {
  it("restores saved placements and preserves Arrange, Preview, cancel, and saved resize across remount", async () => {
    let mobile = false;
    const listeners = new Set<() => void>();
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
      get matches() { return query === "(max-width: 767px)" && mobile; }, media: query, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: (_type: string, listener: () => void) => { listeners.add(listener); },
      removeEventListener: (_type: string, listener: () => void) => { listeners.delete(listener); }, dispatchEvent: vi.fn(),
    })));
    const registry = new ContributionRegistry();
    // Use the real Tasks contribution and stable identities, with small renderer
    // probes so this test isolates the host's layout and mode contract.
    const modules: WidgetModule[] = TASKS_APP_CONTRIBUTION.widgetDefinitions.map((widget) => ({
      moduleId: widget.rendererModuleId,
      widgetTypeId: widget.typeId,
      load: async () => ({
        default: widget.displayName === "Task Workspace" ? WorkspaceProbe : ({ presentation }: WidgetRendererProps) => (
          <output aria-label={`${widget.displayName} mode`}>{presentation.interactionMode}</output>
        ),
      }),
    }));
    registry.registerApp(TASKS_APP_CONTRIBUTION, modules);
    const provider: ViewProvider = {
      appId: TASKS_APP_ID,
      loadView: async () => ({
        viewId: TASKS_VIEW_ID, revision: "1", observedAt: "2026-09-09T12:00:00Z",
        status: "ready", quality: { kind: "complete" }, model: {}, bindings: {}, widgetInputs: {},
      }),
      loadWidget: async (widgetTypeId, request) => ({
        widgetTypeId, instanceId: request.instanceId, revision: "1",
        observedAt: "2026-09-09T12:00:00Z", status: "ready", quality: { kind: "complete" }, input: {},
      }),
      dispatch: vi.fn(async (intent: DashboardIntent) => ({ intent_id: intent.intent_id, status: "accepted" as const })),
      reconcile: async () => ({ changed: false }),
    };
    const repository = new LocalStoragePersonalizationRepository(window.localStorage);
    await repository.save({
      schemaVersion: 1, viewId: TASKS_VIEW_ID, baseDefinitionVersion: 1,
      defaultSlotOverrides: {
        [TASKS_SLOT_IDS.quickAdd]: {
          slotId: TASKS_SLOT_IDS.quickAdd, instanceId: TASKS_INSTANCE_IDS.quickAdd,
          layout: { instanceId: TASKS_INSTANCE_IDS.quickAdd, x: 0, y: 0, w: 12, h: 4 },
        },
        [TASKS_SLOT_IDS.workspace]: {
          slotId: TASKS_SLOT_IDS.workspace, instanceId: TASKS_INSTANCE_IDS.workspace,
          layout: { instanceId: TASKS_INSTANCE_IDS.workspace, x: 12, y: 0, w: 12, h: 18 },
        },
      },
      addedInstances: [], orphanedInstances: [], mobileOrderOverride: null,
    });
    const save = vi.spyOn(repository, "save");
    const tree = () => (
      <MemoryRouter>
        <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
          <DashboardEventProvider>
            <DashboardAnnouncer>
              <DashboardTestRuntime>
                <CustomizeModeProvider>
                  <CustomizeViewToggle />
                  <ViewHost registry={registry} definition={TASKS_VIEW_DEFINITION}
                    provider={provider} personalizationRepository={repository} />
                </CustomizeModeProvider>
              </DashboardTestRuntime>
            </DashboardAnnouncer>
          </DashboardEventProvider>
        </ThemeProvider>
      </MemoryRouter>
    );
    const user = userEvent.setup();
    const rendered = render(tree());
    await screen.findByLabelText("Task Workspace mode");
    expect(rendered.container.querySelector(".react-grid-layout")).not.toBeNull();
    const workspace = () => screen.getByRole("region", { name: "Task Workspace" });
    const workspaceHandle = () => within(
      rendered.container.querySelector<HTMLElement>(
        `[data-widget-instance-id="${TASKS_INSTANCE_IDS.workspace}"].wb-dashboard-grid-item`,
      )!,
    ).getByRole("button", { name: /Move or resize widget/ });

    await user.click(screen.getByRole("button", { name: "Review task action" }));
    const review = screen.getByRole("alertdialog", { name: "Review task action" });
    act(() => { mobile = true; listeners.forEach((listener) => listener()); });
    expect(rendered.container.querySelector(".wb-dashboard-mobile-stack")).not.toBeNull();
    expect(screen.getByRole("alertdialog", { name: "Review task action" })).toBe(review);
    act(() => { mobile = false; listeners.forEach((listener) => listener()); });
    expect(rendered.container.querySelector(".react-grid-layout")).not.toBeNull();
    expect(screen.getByRole("alertdialog", { name: "Review task action" })).toBe(review);
    await user.click(screen.getByRole("button", { name: "Cancel task action" }));

    await user.click(screen.getByRole("button", { name: "Customize view" }));
    expect(within(workspace()).getByText("12 × 18 grid units")).toBeVisible();
    expect(await screen.findByLabelText("Task Workspace mode")).toHaveTextContent("arrange");
    expect(screen.getByRole("button", { name: "Widgets" })).toBeEnabled();
    fireEvent.keyDown(workspaceHandle(), { key: "ArrowDown", shiftKey: true });
    expect(within(workspace()).getByText("12 × 19 grid units")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Preview interactions" }));
    expect(await screen.findByLabelText("Task Workspace mode")).toHaveTextContent("preview");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByLabelText("Task Workspace mode")).toHaveTextContent("operate");
    expect(save).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Customize view" }));
    expect(within(workspace()).getByText("12 × 18 grid units")).toBeVisible();
    fireEvent.keyDown(workspaceHandle(), { key: "ArrowDown", shiftKey: true });
    fireEvent.keyDown(workspaceHandle(), { key: "ArrowDown" });
    await user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.getByLabelText("Task Workspace mode")).toHaveTextContent("operate"));
    expect(save).toHaveBeenCalledTimes(1);
    expect((await repository.load(TASKS_VIEW_ID))?.defaultSlotOverrides[TASKS_SLOT_IDS.workspace]?.layout)
      .toMatchObject({ x: 12, y: 1, w: 12, h: 19 });
    expect(provider.dispatch).not.toHaveBeenCalled();

    rendered.unmount();
    render(tree());
    await screen.findByLabelText("Task Workspace mode");
    await user.click(screen.getByRole("button", { name: "Customize view" }));
    expect(within(workspace()).getByText("12 × 19 grid units")).toBeVisible();
  });
});
