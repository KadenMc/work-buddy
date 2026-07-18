import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { DashboardEventProvider } from "../../../dashboard/events/DashboardEventProvider";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { InMemoryCoworkProvider } from "../providers/InMemoryCoworkProvider";
import { COWORK_VIEW_DEFINITION } from "../viewDefinition";
import { CoworkWorkspaceSurface } from "./CoworkWorkspaceSurface";

const renderSurface = () =>
  render(
    <DashboardEventProvider>
      <CoworkWorkspaceSurface
        definition={COWORK_VIEW_DEFINITION}
        provider={new InMemoryCoworkProvider()}
      />
    </DashboardEventProvider>,
  );

describe("CoworkWorkspaceSurface", () => {
  it("renders the health strip, editor pane, and review rail regions", async () => {
    const { container } = renderSurface();

    // Health strip reflects the coarse document session.
    await waitFor(
      () => expect(screen.getByText("Co-work demo document")).toBeVisible(),
      { timeout: 10_000 },
    );
    expect(screen.getByText("In sync")).toBeVisible();
    expect(screen.getByText("0 open proposals")).toBeVisible();

    // Rail tabs.
    expect(screen.getByRole("tab", { name: "Review" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /Chat/ })).toBeVisible();

    // Editor pane mounts a live ProseMirror editor with its seeded content.
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    expect(screen.getByText(/This is the editor pane/)).toBeVisible();
  }, 15_000);

  it("switches to the Chat tab", async () => {
    renderSurface();
    await waitFor(
      () => expect(screen.getByText("Co-work demo document")).toBeVisible(),
      { timeout: 10_000 },
    );

    await userEvent.click(screen.getByRole("tab", { name: /Chat/ }));
    expect(screen.getByRole("tab", { name: /Chat/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    // The Chat tab now mounts the house chat panel seeded with the document agent's
    // opening message, not the rail placeholder stub.
    await waitFor(
      () =>
        expect(
          screen.getByText(/I proposed a few tracked edits/),
        ).toBeVisible(),
      { timeout: 10_000 },
    );
  }, 15_000);

  it("has no accessibility violations in its resting state", async () => {
    const { container } = renderSurface();
    await waitFor(
      () => expect(container.querySelector(".ProseMirror")).not.toBeNull(),
      { timeout: 10_000 },
    );
    await expectNoAccessibilityViolations(container);
  }, 15_000);
});
