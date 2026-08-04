import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { CoworkLabSection } from "./coworkLabCases";

describe("Co-work Widget Lab section", () => {
  it("renders every card type", () => {
    render(<CoworkLabSection />);
    expect(
      within(screen.getByTestId("cowork-lab-card-insertion")).getByText(
        "Insertion",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByTestId("cowork-lab-card-deletion")).getByText(
        "Deletion",
      ),
    ).toBeVisible();
    expect(
      within(screen.getByTestId("cowork-lab-card-flag")).getByText("Flag"),
    ).toBeVisible();
    expect(
      within(screen.getByTestId("cowork-lab-card-claim")).getByText("Claim"),
    ).toBeVisible();
  });

  it("renders every verb group", () => {
    render(<CoworkLabSection />);
    const edit = within(screen.getByTestId("cowork-lab-markbar-edit"));
    for (const label of [
      "Accept",
      "Amend",
      "Reject",
      "Reject as false",
      "Reject as preference",
      "Redirect",
      "Defer",
    ]) {
      expect(edit.getByRole("button", { name: label })).toBeVisible();
    }

    const flag = within(screen.getByTestId("cowork-lab-markbar-flag"));
    for (const label of ["Endorse", "Dismiss", "Redirect"]) {
      expect(flag.getByRole("button", { name: label })).toBeVisible();
    }

    const claim = within(screen.getByTestId("cowork-lab-markbar-claim"));
    for (const label of [
      "Confirm",
      "Reject",
      "Challenge",
      "Supersede",
      "Redact",
      "Propose",
    ]) {
      expect(claim.getByRole("button", { name: label })).toBeVisible();
    }
  });

  it("disables text mutation when the original target is missing", () => {
    render(<CoworkLabSection />);
    const stale = within(screen.getByTestId("cowork-lab-stale"));
    // The card badge and mark-bar note both state the target problem in text.
    expect(
      stale.getByText("Original passage is no longer present"),
    ).toBeVisible();
    expect(
      stale.getByText(
        "The original passage cannot be placed safely. Accept and Amend are unavailable; other review decisions still work.",
      ),
    ).toBeVisible();
    const markbar = within(screen.getByTestId("cowork-lab-markbar-stale"));
    expect(markbar.getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(markbar.getByRole("button", { name: "Amend" })).toBeDisabled();
    expect(markbar.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(markbar.getByRole("button", { name: "Defer" })).toBeEnabled();
  });

  it("renders the document-order stream", () => {
    render(<CoworkLabSection />);
    const stream = within(screen.getByTestId("cowork-lab-stream"));
    expect(stream.getAllByRole("listitem")).toHaveLength(5);
  });

  it("stages a verb through the live mark bar", async () => {
    render(<CoworkLabSection />);
    const edit = within(screen.getByTestId("cowork-lab-markbar-edit"));
    const accept = edit.getByRole("button", { name: "Accept" });
    expect(accept).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(accept);
    expect(accept).toHaveAttribute("aria-pressed", "true");
  });

  it("clears axe on the card and stream panels", async () => {
    // Per-component axe lives in the conformance suite. The whole section mounts
    // several mark bars at once, which collide only as a lab-composition artifact
    // (production shows one mark bar), so axe is scoped to panels here.
    render(<CoworkLabSection />);
    await expectNoAccessibilityViolations(
      screen.getByTestId("cowork-lab-card-insertion"),
    );
    await expectNoAccessibilityViolations(
      screen.getByTestId("cowork-lab-stream"),
    );
  });
});
