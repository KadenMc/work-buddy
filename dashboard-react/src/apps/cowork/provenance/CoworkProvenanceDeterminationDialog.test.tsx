import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkProvenanceDeterminationDialog } from "./CoworkProvenanceDeterminationDialog";
import {
  defaultCoworkProvenanceDetermination,
  type CoworkProvenanceDetermination,
} from "./contracts";

const ACTOR = {
  kind: "human",
  ref: "dashboard-user",
  identity_status: "local_actor_ref",
} as const;

function ControlledDialog({
  onClose = vi.fn(),
  onConfirm = vi.fn(),
}: {
  readonly onClose?: () => void;
  readonly onConfirm?: (value: CoworkProvenanceDetermination) => void;
}) {
  const [value, setValue] = useState<CoworkProvenanceDetermination>(
    () => defaultCoworkProvenanceDetermination(ACTOR),
  );
  return (
    <CoworkProvenanceDeterminationDialog
      value={value}
      currentUserIdentity={ACTOR}
      onChange={setValue}
      onClose={onClose}
      onConfirm={onConfirm}
    />
  );
}

describe("CoworkProvenanceDeterminationDialog", () => {
  it("submits the typed determination and offers a non-destructive deferral", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onConfirm = vi.fn();
    render(<ControlledDialog onClose={onClose} onConfirm={onConfirm} />);

    expect(
      screen.getByRole("dialog", { name: "Where did this text come from?" }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(onConfirm).toHaveBeenCalledWith(
      defaultCoworkProvenanceDetermination(ACTOR),
    );

    await user.click(screen.getByRole("button", { name: "Decide later" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("keeps confirmation unavailable until a named person is identified", async () => {
    const user = userEvent.setup();
    render(<ControlledDialog />);

    await user.click(screen.getByRole("button", { name: /^Me Author$/i }));
    await user.click(screen.getByRole("option", { name: "Someone else" }));

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.queryByText("Enter the author’s name.")).toBeNull();
    await user.type(
      screen.getByRole("textbox", { name: "Author’s name" }),
      "Avery",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("has no automated accessibility violations", async () => {
    const { container } = render(<ControlledDialog />);
    await expectNoAccessibilityViolations(container);
  });
});
