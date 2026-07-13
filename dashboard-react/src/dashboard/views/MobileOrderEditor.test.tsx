import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { dashboardRegistry } from "../../app/dashboardRegistry";
import { JOURNAL_VIEW_DEFINITION } from "../../apps/journal/viewDefinition";
import { resolveViewPersonalization } from "../personalization/reducer";
import { MobileOrderEditor } from "./MobileOrderEditor";

describe("MobileOrderEditor", () => {
  it("changes the canonical order with explicit non-drag controls", async () => {
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

    await userEvent.click(
      screen.getByRole("button", { name: "Move Day Timeline earlier on mobile" }),
    );
    expect(onChange).toHaveBeenCalledWith([
      "default:timeline",
      "default:capture",
      "default:running-notes",
    ]);
  });
});
