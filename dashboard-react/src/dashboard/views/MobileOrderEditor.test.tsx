import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { dashboardRegistry } from "../../app/dashboardRegistry";
import { JOURNAL_VIEW_DEFINITION } from "../../apps/journal/viewDefinition";
import { asWidgetInstanceId } from "../contributions/contracts";
import { resolveViewPersonalization } from "../personalization/reducer";
import { MobileOrderEditor, reorderMobileWidgets } from "./MobileOrderEditor";

describe("MobileOrderEditor", () => {
  it("renders accessible drag handles instead of stepwise ordering buttons", () => {
    const definitions = new Map(
      dashboardRegistry.listWidgets().map(({ definition }) => [definition.typeId, definition]),
    );
    const resolved = resolveViewPersonalization(JOURNAL_VIEW_DEFINITION, definitions);
    const onChange = vi.fn();
    render(
      <MobileOrderEditor
        registry={dashboardRegistry}
        instances={resolved.instances}
        order={resolved.mobileOrder}
        onChange={onChange}
        onClose={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Drag Day Timeline to reorder on mobile" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Earlier")).not.toBeInTheDocument();
    expect(screen.queryByText("Later")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("reorders one or more widget identities at a drop target", () => {
    const capture = asWidgetInstanceId("default:capture");
    const timeline = asWidgetInstanceId("default:timeline");
    const notes = asWidgetInstanceId("default:running-notes");
    expect(
      reorderMobileWidgets(
        [capture, timeline, notes],
        new Set([timeline]),
        { key: capture, dropPosition: "before" },
      ),
    ).toEqual([timeline, capture, notes]);
  });
});
