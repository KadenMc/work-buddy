import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import {
  InteractionSurfaceProvider,
  useInteractionSurfaces,
} from "./InteractionSurfaceProvider";

function Harness() {
  const { notify, confirm } = useInteractionSurfaces();
  const [result, setResult] = useState("none");
  return (
    <>
      <button
        type="button"
        onClick={() =>
          notify({
            message: "Placement unchanged",
            tone: "warning",
            dedupeKey: "layout",
          })
        }
      >
        Notify
      </button>
      <button
        type="button"
        onClick={() => {
          void confirm({
            title: "Clear this draft?",
            description: "Saved items and settings are unaffected.",
            confirmLabel: "Clear draft",
            cancelLabel: "Keep draft",
            tone: "danger",
          }).then((confirmed) => setResult(confirmed ? "cleared" : "kept"));
        }}
      >
        Confirm
      </button>
      <output>{result}</output>
    </>
  );
}

describe("InteractionSurfaceProvider", () => {
  it("deduplicates reusable transient feedback", async () => {
    render(
      <InteractionSurfaceProvider>
        <Harness />
      </InteractionSurfaceProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Notify" }));
    await userEvent.click(screen.getByRole("button", { name: "Notify" }));
    expect(screen.getAllByText("Placement unchanged")).toHaveLength(1);
    expect(screen.getByText("Placement unchanged").closest(".wb-transient-notice")).toHaveAttribute(
      "role",
      "status",
    );
  });

  it("serializes an accessible confirmation without treating cancellation as consent", async () => {
    render(
      <InteractionSurfaceProvider>
        <Harness />
      </InteractionSurfaceProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(screen.getByRole("alertdialog", { name: "Clear this draft?" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Keep draft" }));
    expect(screen.getByText("kept")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});
