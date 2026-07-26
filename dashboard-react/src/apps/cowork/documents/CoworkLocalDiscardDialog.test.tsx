import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CoworkLocalDiscardDialog } from "./CoworkLocalDiscardDialog";

describe("CoworkLocalDiscardDialog", () => {
  it("requires an explicit confirmation before discarding the on-device copy", async () => {
    const user = userEvent.setup();
    const onDiscard = vi.fn(async () => undefined);
    render(
      <CoworkLocalDiscardDialog
        title="Untitled"
        onClose={vi.fn()}
        onDiscard={onDiscard}
      />,
    );

    expect(onDiscard).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Discard this document?" })).toHaveTextContent(
      "saved only on this device",
    );

    await user.click(screen.getByRole("button", { name: "Discard document" }));
    expect(onDiscard).toHaveBeenCalledTimes(1);
  });
});
